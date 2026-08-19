from __future__ import annotations

import json
from threading import Event

import pytest
from app.models import Command, ImportStatus, KnowledgeDocument, Manual
from app.planning.runtime import PlanningCancelled, append_event
from app.planning.service import (
    cancel_config_task,
    create_config_task,
    create_topology,
    generate_config_commands,
)
from app.schemas import ConfigTaskCreate, TopologyDraft
from app.template_service import (
    create_template_from_task,
    delete_template,
    template_snapshot,
    update_template,
)


def _command(session, manual: Manual, name: str, syntax: list[str]) -> None:  # type: ignore[no-untyped-def]
    document = KnowledgeDocument(
        manual_id=manual.id,
        source_path=f"{name}.html",
        title=name,
        text_content=f"{name} command reference",
    )
    session.add(document)
    session.flush()
    session.add(
        Command(
            manual_id=manual.id,
            document_id=document.id,
            canonical_name=name,
            syntax_json=json.dumps(syntax),
            views_json="[]",
            preconditions_json="[]",
            constraints_json="[]",
        )
    )


def _topology(session, name: str, port: str):  # type: ignore[no-untyped-def]
    return create_topology(
        session,
        TopologyDraft.model_validate(
            {
                "name": name,
                "nodes": [
                    {"id": f"{name}-sw", "kind": "switch", "name": "SW", "x": 0, "y": 0},
                    {"id": f"{name}-pc", "kind": "pc", "name": "PC", "x": 200, "y": 0},
                ],
                "links": [
                    {
                        "id": f"{name}-link",
                        "source": f"{name}-sw",
                        "source_port": port,
                        "target": f"{name}-pc",
                        "target_port": "Ethernet0/0/1",
                    }
                ],
            }
        ),
    )


def test_template_is_a_snapshot_and_is_only_a_reference_for_new_planning(session) -> None:  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="huawei-vlan.html",
        stored_path="huawei-vlan.html",
        source_sha256="3" * 64,
        brand="Huawei",
        release=None,
        file_format="html",
        status=ImportStatus.completed,
    )
    session.add(manual)
    session.flush()
    _command(session, manual, "vlan batch", ["vlan batch { vlan-id }"])
    _command(session, manual, "port link-type", ["port link-type access"])
    _command(session, manual, "port default vlan", ["port default vlan vlan-id"])
    session.commit()

    original_topology = _topology(session, "original", "GE0/0/1")
    original_task = create_config_task(
        session,
        ConfigTaskCreate(
            topology_revision_id=original_topology.id,
            manual_id=manual.id,
            requirement_text="创建 VLAN 10，并将接入口配置为 Access。",
        ),
    )
    original_task = generate_config_commands(session, original_task.id)
    frozen = template_snapshot(original_task)
    template = create_template_from_task(
        session,
        task_id=original_task.id,
        title="单交换机 VLAN 10",
        description="接入端口配置方式参考",
    )
    assert json.loads(template.snapshot_json) == frozen
    assert frozen["topology"]["links"][0]["source_port"] == "GE0/0/1"
    assert "port default vlan 10" in frozen["device_plans"][0]["commands"]

    updated = update_template(
        session,
        template_id=template.id,
        title="更新后的模板标题",
        description="仅修改元数据，不改快照",
    )
    assert updated.title == "更新后的模板标题"
    assert json.loads(updated.snapshot_json) == frozen

    new_topology = _topology(session, "new", "GE0/0/5")
    referenced_task = create_config_task(
        session,
        ConfigTaskCreate(
            topology_revision_id=new_topology.id,
            manual_id=manual.id,
            template_id=template.id,
            requirement_text="创建 VLAN 30，并将当前接入口配置为 Access。",
        ),
    )
    intent = json.loads(referenced_task.intent_json)
    reference = intent["template_reference"]
    assert reference["title"] == "更新后的模板标题"
    assert reference["reference_device_commands"][0]["commands"] == frozen["device_plans"][0]["commands"]
    assert intent["vlan_ids"] == [30]

    delete_template(session, template.id)
    assert session.get(type(template), template.id) is None


def test_planning_events_are_persisted_and_cancel_token_stops_new_run(session) -> None:  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="stream.html",
        stored_path="stream.html",
        source_sha256="4" * 64,
        brand=None,
        release=None,
        file_format="html",
        status=ImportStatus.completed,
    )
    session.add(manual)
    session.flush()
    for name, syntax in [
        ("vlan batch", ["vlan batch { vlan-id }"]),
        ("port link-type", ["port link-type access"]),
        ("port default vlan", ["port default vlan vlan-id"]),
    ]:
        _command(session, manual, name, syntax)
    session.commit()

    topology = _topology(session, "stream", "GE0/0/1")
    captured: list[tuple[str, str, str]] = []
    task = create_config_task(
        session,
        ConfigTaskCreate(
            task_id="streaming-event-task-0001",
            topology_revision_id=topology.id,
            manual_id=manual.id,
            requirement_text="创建 VLAN 10，并将接入口配置为 Access。",
        ),
        event_sink=lambda stage, event_type, content: captured.append((stage, event_type, content)),
    )
    assert task.status.value == "idea_ready"
    assert [event_type for _, event_type, _ in captured] == ["stage", "stage", "output", "done"]

    persisted = append_event(session, task.id, "手册检索", "stage", "正在检索手册证据。")
    assert persisted.sequence == 1
    assert append_event(session, task.id, "手册检索", "output", "找到 3 条证据。").sequence == 2

    cancellation = Event()
    cancellation.set()
    cancelled_topology = _topology(session, "cancel", "GE0/0/2")
    with pytest.raises(PlanningCancelled):
        create_config_task(
            session,
            ConfigTaskCreate(
                task_id="streaming-cancel-task-0001",
                topology_revision_id=cancelled_topology.id,
                manual_id=manual.id,
                requirement_text="创建 VLAN 20，并将接入口配置为 Access。",
            ),
            event_sink=lambda _stage, _event_type, _content: None,
            cancel_event=cancellation,
        )
    stopped = cancel_config_task(session, "streaming-cancel-task-0001")
    assert stopped.status.value == "cancelled"
    assert stopped.cancel_requested is True
