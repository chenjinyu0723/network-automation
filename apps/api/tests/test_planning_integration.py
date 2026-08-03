from __future__ import annotations

import json

from app.models import Command, DeviceModel, ImportStatus, KnowledgeDocument, Manual, ModelLevel, ReviewStatus
from app.planning.service import _pc_facing_ports_from_topology, create_config_task, create_topology
from app.schemas import ConfigTaskCreate, TopologyDraft


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


def test_planning_keeps_ge_cli_text_but_blocks_equivalent_protected_long_port(session) -> None:  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="test.html",
        stored_path="test.html",
        source_sha256="0" * 64,
        brand="Huawei",
        release="test",
        file_format="html",
        status=ImportStatus.completed,
    )
    session.add(manual)
    session.flush()
    series = DeviceModel(
        brand="Huawei",
        canonical_name="S5700",
        level=ModelLevel.series,
        review_status=ReviewStatus.published,
        source_manual_id=manual.id,
    )
    session.add(series)
    session.flush()
    _command(session, manual, "vlan batch", ["vlan batch { vlan-id }"])
    _command(session, manual, "port link-type", ["port link-type access"])
    _command(session, manual, "port default vlan", ["port default vlan vlan-id"])
    session.commit()

    allowed = create_topology(
        session,
        TopologyDraft.model_validate(
            {
                "name": "GE 保留写法",
                "nodes": [
                    {
                        "id": "sw1",
                        "kind": "switch",
                        "name": "SW1",
                        "x": 0,
                        "y": 0,
                        "model_id": series.id,
                        "protected_ports": ["GigabitEthernet0/0/2"],
                    },
                    {"id": "pc1", "kind": "pc", "name": "PC1", "x": 1, "y": 1},
                ],
                "links": [
                    {
                        "id": "l1",
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
            topology_revision_id=allowed.id,
            manual_id=manual.id,
            requirement_text="创建 VLAN 10，并将端口配置为 Access。",
        ),
    )
    assert task.status.value == "needs_review"
    assert "interface GE0/0/1" in json.loads(task.device_plans[0].commands_json)

    blocked = create_topology(
        session,
        TopologyDraft.model_validate(
            {
                "name": "GE 等价保护端口",
                "nodes": [
                    {
                        "id": "sw2",
                        "kind": "switch",
                        "name": "SW2",
                        "x": 0,
                        "y": 0,
                        "model_id": series.id,
                        "protected_ports": ["GigabitEthernet0/0/2"],
                    },
                    {"id": "pc2", "kind": "pc", "name": "PC2", "x": 1, "y": 1},
                ],
                "links": [
                    {
                        "id": "l2",
                        "source": "sw2",
                        "source_port": "GE0/0/2",
                        "target": "pc2",
                        "target_port": "Ethernet0/0/1",
                    }
                ],
            }
        ),
    )
    blocked_task = create_config_task(
        session,
        ConfigTaskCreate(
            topology_revision_id=blocked.id,
            manual_id=manual.id,
            requirement_text="创建 VLAN 10，并将端口配置为 Access。",
        ),
    )
    assert blocked_task.status.value == "blocked"
    assert json.loads(blocked_task.device_plans[0].commands_json) == []


def test_vlan_access_scope_only_contains_switch_ports_facing_pcs() -> None:
    graph = {
        "nodes": [
            {"id": "sw1", "kind": "switch"},
            {"id": "sw2", "kind": "switch"},
            {"id": "pc1", "kind": "pc"},
        ],
        "links": [
            {
                "id": "pc-link",
                "source": "sw1",
                "source_port": "GE0/0/1",
                "target": "pc1",
                "target_port": "Ethernet0/0/1",
            },
            {
                "id": "uplink",
                "source": "sw1",
                "source_port": "GE0/0/2",
                "target": "sw2",
                "target_port": "GE0/0/1",
            },
        ],
    }
    assert _pc_facing_ports_from_topology(graph, "sw1") == ["GE0/0/1"]
    assert _pc_facing_ports_from_topology(graph, "sw2") == []
