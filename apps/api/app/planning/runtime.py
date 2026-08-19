from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PlanningEvent


class PlanningCancelled(RuntimeError):
    """Raised inside a planning worker when the operator stops the run."""


EventSink = Callable[[str, str, str], None]


@dataclass
class PlanningRun:
    task_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)


_runs: dict[str, PlanningRun] = {}
_runs_lock = threading.Lock()


def start_run(task_id: str) -> PlanningRun:
    run = PlanningRun(task_id=task_id)
    with _runs_lock:
        _runs[task_id] = run
    return run


def get_run(task_id: str) -> PlanningRun | None:
    with _runs_lock:
        return _runs.get(task_id)


def finish_run(task_id: str) -> None:
    with _runs_lock:
        _runs.pop(task_id, None)


def request_cancel(task_id: str) -> bool:
    run = get_run(task_id)
    if not run:
        return False
    run.cancel_event.set()
    return True


def check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check and cancel_check():
        raise PlanningCancelled("用户已停止配置规划")


def append_event(session: Session, task_id: str, stage: str, event_type: str, content: str) -> PlanningEvent:
    latest = session.scalar(
        select(func.max(PlanningEvent.sequence)).where(PlanningEvent.task_id == task_id)
    )
    event = PlanningEvent(
        task_id=task_id,
        sequence=int(latest or 0) + 1,
        stage=stage,
        event_type=event_type,
        content=content,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def make_event_sink(
    session: Session,
    task_id: str,
    cancel_check: Callable[[], bool] | None = None,
) -> EventSink:
    # Token-by-token thinking is useful to the UI but too chatty for SQLite.
    # Coalesce short chunks while preserving stage boundaries and cancellation.
    pending: dict[tuple[str, str], str] = {}
    last_flush: dict[tuple[str, str], float] = {}
    stream_event_types = {"thinking", "output"}
    flush_interval = 0.12
    flush_chars = 512

    def flush_pending() -> None:
        for (pending_stage, pending_type), pending_content in list(pending.items()):
            if pending_content:
                append_event(session, task_id, pending_stage, pending_type, pending_content)
            pending.pop((pending_stage, pending_type), None)
            last_flush.pop((pending_stage, pending_type), None)

    def emit(stage: str, event_type: str, content: str) -> None:
        check_cancel(cancel_check)
        if event_type in stream_event_types:
            key = (stage, event_type)
            pending[key] = f"{pending.get(key, '')}{content}"
            now = time.monotonic()
            if len(pending[key]) < flush_chars and now - last_flush.get(key, 0.0) < flush_interval:
                return
            content = pending.pop(key)
            last_flush[key] = now
        else:
            flush_pending()
        append_event(session, task_id, stage, event_type, content)

    return emit
