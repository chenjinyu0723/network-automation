from __future__ import annotations

import threading
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


def finish_run(task_id: str, expected_run: PlanningRun | None = None) -> None:
    """Forget a run without letting an older worker erase its replacement.

    A stop-and-restart can create a new cancellation token for the same task
    while the old provider request is still unwinding.  The old worker must
    only remove its own registry entry; otherwise the replacement loses its
    cancel/progress ownership and the UI can appear to need a second click.
    """

    with _runs_lock:
        current = _runs.get(task_id)
        if expected_run is None or current is expected_run:
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
    def emit(stage: str, event_type: str, content: str) -> None:
        check_cancel(cancel_check)
        # The operator asked for workflow status, not raw model thought or
        # partial JSON. Dropping token chunks here also keeps SQLite available
        # for the stop/restart button while a local LLM is streaming.
        if event_type in {"thinking", "output"}:
            return
        append_event(session, task_id, stage, event_type, content)

    return emit
