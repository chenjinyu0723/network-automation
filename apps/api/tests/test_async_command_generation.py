from __future__ import annotations

import json
import time

from app.api import routes
from app.db import Base
from app.models import ConfigTask, ImportStatus, Manual, TaskStatus, Topology, TopologyRevision
from app.planning.runtime import finish_run
from app.schemas import PlanningIdeaUpdateRequest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def test_command_generation_route_returns_before_background_worker_finishes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A slow provider must not keep the browser POST open or suppress SSE progress."""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(routes, "SessionLocal", maker)

    with maker() as session:
        manual = Manual(
            original_filename="async-route-test.html",
            stored_path="async-route-test.html",
            source_sha256="a" * 64,
            file_format="html",
            status=ImportStatus.completed,
        )
        topology = Topology(name="async-route-test")
        session.add_all([manual, topology])
        session.flush()
        revision = TopologyRevision(
            topology_id=topology.id,
            revision=1,
            graph_json=json.dumps({"name": topology.name, "nodes": [], "links": []}),
        )
        session.add(revision)
        session.flush()
        task = ConfigTask(
            topology_revision_id=revision.id,
            manual_id=manual.id,
            requirement_text="验证后台命令生成。",
            status=TaskStatus.idea_ready,
            planning_idea="确认后交给后台生成命令。",
        )
        session.add(task)
        session.commit()
        task_id = task.id

    def slow_generate(session, task_id, *, event_sink=None, cancel_event=None):  # type: ignore[no-untyped-def]
        time.sleep(0.15)
        task = session.get(ConfigTask, task_id)
        assert task is not None
        task.status = TaskStatus.needs_review
        session.commit()
        if event_sink:
            event_sink("完成", "done", "后台测试命令已生成。")
        return task

    monkeypatch.setattr(routes, "generate_config_commands", slow_generate)
    started = time.monotonic()
    with maker() as session:
        response = routes.post_generate_config_commands(
            task_id,
            PlanningIdeaUpdateRequest(planning_idea="确认后交给后台生成命令。"),
            session,
        )
    assert response.status == TaskStatus.planning.value
    assert time.monotonic() - started < 0.1

    deadline = time.monotonic() + 2
    completed = False
    while time.monotonic() < deadline:
        with maker() as session:
            task = session.get(ConfigTask, task_id)
            completed = bool(task and task.status == TaskStatus.needs_review)
        if completed:
            break
        time.sleep(0.02)

    finish_run(task_id)
    assert completed
