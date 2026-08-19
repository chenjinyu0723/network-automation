from __future__ import annotations

import json

from app.execution import service as execution_service
from app.execution.service import (
    execute_huawei_device_plan,
    execute_huawei_undo_plan,
    queue_huawei_device_plan,
    queue_huawei_undo_plan,
)
from app.models import Command, ImportStatus, KnowledgeDocument, Manual
from app.planning.service import (
    approve_device_plan,
    create_config_task,
    create_topology,
    generate_config_commands,
)
from app.schemas import ConfigTaskCreate, TopologyDraft


class FakeHuaweiConnection:
    def __init__(self, **_kwargs: object) -> None:
        self.disconnected = False

    def send_command(self, command: str, read_timeout: int = 30) -> str:
        del read_timeout
        if command == "display version":
            return "Huawei VRP Version 5.110 (S5700 V200R001C00)"
        if command.startswith("display vlan"):
            return "VLAN 10 GE0/0/1"
        if command.startswith("display port vlan"):
            return "GE0/0/1 PVID: 10"
        return command

    def send_command_timing(self, command: str, read_timeout: int = 30) -> str:
        del read_timeout
        if command == "save":
            return "Save the configuration successfully"
        return command

    def disconnect(self) -> None:
        self.disconnected = True


def _add_command(session, manual: Manual, name: str, syntax: list[str]) -> None:  # type: ignore[no-untyped-def]
    document = KnowledgeDocument(
        manual_id=manual.id,
        source_path=f"{name}.html",
        title=name,
        text_content=f"{name} reference",
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


def test_apply_and_undo_record_command_level_echo_without_uplink(
    session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(execution_service, "ConnectHandler", FakeHuaweiConnection)
    manual = Manual(
        original_filename="huawei-vlan.html",
        stored_path="huawei-vlan.html",
        source_sha256="9" * 64,
        brand="Huawei",
        release=None,
        file_format="html",
        status=ImportStatus.completed,
    )
    session.add(manual)
    session.flush()
    _add_command(session, manual, "vlan batch", ["vlan batch { vlan-id }"])
    _add_command(session, manual, "port link-type", ["port link-type access"])
    _add_command(session, manual, "port default vlan", ["port default vlan vlan-id"])
    session.commit()
    topology = create_topology(
        session,
        TopologyDraft.model_validate(
            {
                "name": "stream-execution",
                "nodes": [
                    {
                        "id": "sw1",
                        "kind": "switch",
                        "name": "SW1",
                        "x": 0,
                        "y": 0,
                        "protected_ports": ["GE0/0/2"],
                    },
                    {"id": "pc1", "kind": "pc", "name": "PC1", "x": 200, "y": 0},
                ],
                "links": [
                    {
                        "id": "pc-link",
                        "source": "sw1",
                        "source_port": "GE0/0/1",
                        "target": "pc1",
                        "target_port": "Ethernet0/0/1",
                    }
                ],
            }
        ),
    )
    task = create_config_task(
        session,
        ConfigTaskCreate(
            topology_revision_id=topology.id,
            manual_id=manual.id,
            requirement_text="创建 VLAN 10，并将 PC 接入口配置为 Access。",
        ),
    )
    task = generate_config_commands(session, task.id)
    plan = task.device_plans[0]
    approve_device_plan(session, plan.id, plan.approval_revision, None)

    queued = queue_huawei_device_plan(
        session,
        task_id=task.id,
        plan_id=plan.id,
        host="192.0.2.1",
        port=22,
        execution_id="apply-stream-echo-0001",
    )
    applied = execute_huawei_device_plan(
        session,
        task_id=task.id,
        plan_id=plan.id,
        host="192.0.2.1",
        port=22,
        username="user",
        password="placeholder",
        execution_id=queued.id,
    )
    assert applied.operation == "apply"
    assert applied.status.value == "completed"
    assert [entry.sequence for entry in applied.commands] == list(range(1, len(applied.commands) + 1))
    assert applied.commands[0].phase == "connect"
    assert any(entry.phase == "configure" for entry in applied.commands)

    undo_queued = queue_huawei_undo_plan(
        session,
        task_id=task.id,
        plan_id=plan.id,
        host="192.0.2.1",
        port=22,
        execution_id="undo-stream-echo-0001",
    )
    undone = execute_huawei_undo_plan(
        session,
        task_id=task.id,
        plan_id=plan.id,
        host="192.0.2.1",
        port=22,
        username="user",
        password="placeholder",
        execution_id=undo_queued.id,
    )
    assert undone.operation == "undo"
    assert undone.status.value == "completed"
    assert all("GE0/0/2" not in entry.command for entry in undone.commands)
