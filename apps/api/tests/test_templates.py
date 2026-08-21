from __future__ import annotations

import json
from threading import Event

import pytest
from app.models import Command, ImportStatus, KnowledgeDocument, Manual
from app.planning.runtime import PlanningCancelled, append_event
from app.planning.service import (
    cancel_config_task,
    create_config_task,
    create_config_task_record,
    create_topology,
    generate_config_commands,
)
from app.schemas import ConfigTaskCreate, TopologyDraft
from app.template_service import (
    create_template,
    create_template_from_task,
    delete_template,
    sanitize_template_snapshot,
    template_snapshot,
    update_template,
    validate_template_snapshot,
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


def test_template_is_a_snapshot_of_topology_requirements_idea_and_commands(session) -> None:  # type: ignore[no-untyped-def]
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
    assert set(frozen["device_plans"][0]) == {"display_name", "device_node_id", "commands"}
    assert "intent" not in frozen["device_plans"][0]
    assert "validation" not in frozen["device_plans"][0]
    assert sanitize_template_snapshot(
        {
            "device_plans": [
                {
                    "display_name": "SW1",
                    "device_node_id": "sw1",
                    "intent": {"internal": True},
                    "commands": ["system-view"],
                    "validation": {"internal": True},
                }
            ]
        }
    )["device_plans"] == [
        {"display_name": "SW1", "device_node_id": "sw1", "commands": ["system-view"]}
    ]

    updated = update_template(
        session,
        template_id=template.id,
        title="更新后的模板标题",
        description="仅修改元数据，不改快照",
    )
    assert updated.title == "更新后的模板标题"
    assert json.loads(updated.snapshot_json) == frozen

    delete_template(session, template.id)
    assert session.get(type(template), template.id) is None


def test_user_created_template_keeps_per_switch_commands_and_validates_membership(session) -> None:  # type: ignore[no-untyped-def]
    snapshot = {
        "topology_id": "topology-a",
        "topology": {
            "name": "two-switch",
            "nodes": [
                {"id": "sw-a", "kind": "switch", "name": "SW-A", "x": 0, "y": 0},
                {"id": "sw-b", "kind": "switch", "name": "SW-B", "x": 300, "y": 0},
                {"id": "pc-a", "kind": "pc", "name": "PC-A", "x": 0, "y": 200},
            ],
            "links": [],
        },
        "requirement_text": "两台交换机建立三层互通",
        "planning_idea": "先配置接口地址，再配置路由协议。",
        "device_plans": [
            {"display_name": "SW-A", "device_node_id": "sw-a", "commands": ["interface GE0/0/1"]},
            {"display_name": "SW-B", "device_node_id": "sw-b", "commands": ["interface GE0/0/1"]},
        ],
    }
    template = create_template(
        session,
        title="双交换机三层模板",
        description="用户独立创建",
        snapshot=snapshot,
    )
    saved = json.loads(template.snapshot_json)
    assert saved["topology_id"] == "topology-a"
    assert [item["device_node_id"] for item in saved["device_plans"]] == ["sw-a", "sw-b"]

    changed = {**snapshot, "planning_idea": "改为先规划地址，再验证邻居。"}
    updated = update_template(
        session,
        template_id=template.id,
        title="双交换机三层模板（更新）",
        description="已更新思路",
        snapshot=changed,
    )
    assert json.loads(updated.snapshot_json)["planning_idea"] == changed["planning_idea"]

    invalid = {
        **snapshot,
        "device_plans": [
            {"display_name": "PC-A", "device_node_id": "pc-a", "commands": ["x"]}
        ],
    }
    with pytest.raises(ValueError, match="交换机"):
        validate_template_snapshot(invalid)


def test_reference_template_is_kept_in_task_intent_for_llm_stages(session) -> None:  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="generic.html",
        stored_path="generic.html",
        source_sha256="6" * 64,
        brand="Huawei",
        release=None,
        file_format="html",
        status=ImportStatus.completed,
    )
    session.add(manual)
    session.flush()
    topology = _topology(session, "template-reference", "GE0/0/8")
    template = create_template(
        session,
        title="路由参考模板",
        description="仅用于业务组织",
        snapshot={
            "topology": _load_topology_graph(topology),
            "requirement_text": "参考需求",
            "planning_idea": "参考思路",
            "device_plans": [
                {
                    "display_name": "SW",
                    "device_node_id": "template-reference-sw",
                    "commands": ["ospf 1"],
                }
            ],
        },
    )
    task = create_config_task_record(
        session,
        ConfigTaskCreate(
            topology_revision_id=topology.id,
            manual_id=manual.id,
            requirement_text="在链路上配置动态路由。",
            template_id=template.id,
        ),
    )
    reference = json.loads(task.intent_json)["template_reference"]
    assert reference["title"] == "路由参考模板"
    assert reference["reference_mode"] == "soft_reference"
    assert reference["reference_device_commands"][0]["commands"] == ["ospf 1"]
    assert "ignored_by_generation" not in reference


def _load_topology_graph(revision):  # type: ignore[no-untyped-def]
    return json.loads(revision.graph_json)


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
    assert [event_type for _, event_type, _ in captured] == ["stage", "stage", "stage", "done"]

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
