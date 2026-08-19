from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app.llm.client import LlmTextResult
from app.models import Command, ImportStatus, KnowledgeDocument, Manual
from app.planning import llm_command_plan, llm_refinement
from app.planning import service as planning_service
from app.planning.dialect import HUAWEI_VRP
from app.planning.llm_command_plan import compile_command_plan
from app.planning.service import (
    _candidate_commands,
    _derive_intent,
    _intent_for_device,
    _operator_edited_planning_idea,
    _pc_facing_ports_from_topology,
    _planning_idea_text,
    _render_command_plan_or_fallback,
    _renderer_mode_for_intent,
    _topology_context_for_llm,
    create_config_task,
    create_topology,
    generate_config_commands,
    get_topology_revision,
    update_planning_idea,
    update_topology,
)
from app.schemas import ConfigTaskCreate, LlmCommandPlan, TopologyDraft


def _command(session, manual: Manual, name: str, syntax: list[str], views: list[str] | None = None) -> None:  # type: ignore[no-untyped-def]
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
            views_json=json.dumps(views or []),
            preconditions_json="[]",
            constraints_json="[]",
        )
    )


def test_only_an_operator_edit_extends_generated_planning_scope() -> None:
    generated = "一、目标\n仅启用已请求的协议。"

    assert _operator_edited_planning_idea({"generated_planning_idea": generated}, generated) is False
    assert _operator_edited_planning_idea(
        {"generated_planning_idea": generated},
        f"{generated}\n补充：同时配置用户明确要求的监控。",
    ) is True
    assert _operator_edited_planning_idea({}, generated) is True


def test_chinese_vlan_requirement_builds_a_concrete_editable_plan() -> None:
    graph = {
        "nodes": [
            {"id": "sw1", "kind": "switch", "name": "SW1"},
            {"id": "sw2", "kind": "switch", "name": "SW2"},
            {"id": "sw3", "kind": "switch", "name": "SW3"},
            {"id": "pc1", "kind": "pc", "name": "PC1"},
            {"id": "pc2", "kind": "pc", "name": "PC2"},
            {"id": "pc3", "kind": "pc", "name": "PC3"},
            {"id": "pc4", "kind": "pc", "name": "PC4"},
        ],
        "links": [
            {"source": "sw1", "source_port": "GE0/0/1", "target": "pc1", "target_port": "Ethernet0/0/1"},
            {"source": "sw1", "source_port": "GE0/0/3", "target": "pc2", "target_port": "Ethernet0/0/1"},
            {"source": "sw1", "source_port": "GE0/0/4", "target": "sw3", "target_port": "GE0/0/1"},
            {"source": "sw2", "source_port": "GE0/0/1", "target": "pc3", "target_port": "Ethernet0/0/1"},
            {"source": "sw2", "source_port": "GE0/0/2", "target": "pc4", "target_port": "Ethernet0/0/1"},
            {"source": "sw2", "source_port": "GE0/0/3", "target": "sw3", "target_port": "GE0/0/2"},
        ],
    }
    requirement = "PC1和PC3属于VLAN10，PC2和PC4属于VLAN20，VLAN之间可以互相通信"

    intent = _derive_intent(requirement, graph)
    intent["renderer_mode"] = "huawei_vlan"
    plan = _planning_idea_text(requirement, graph, intent)

    assert intent["feature"] == "multi_vlan_intervlan"
    assert intent["vlan_ids"] == [10, 20]
    assert intent["pc_vlan_map"] == {"pc1": 10, "pc3": 10, "pc2": 20, "pc4": 20}
    assert intent["l3_core_node_id"] == "sw3"
    assert "VLAN 10：PC1、PC3" in plan
    assert (
        "SW1：接入口 GE0/0/1→VLAN 10, GE0/0/3→VLAN 20；"
        "上联 Trunk GE0/0/4 放通 VLAN 10, 20。"
    ) in plan
    assert "SW3：上联 Trunk GE0/0/1, GE0/0/2 放通 VLAN 10, 20；三层网关" in plan


def test_topology_context_contains_all_devices_links_and_explicit_missing_values() -> None:
    context = _topology_context_for_llm(
        {
            "nodes": [
                {"id": "sw1", "kind": "switch", "name": "SW1", "ip": "10.0.0.1", "prefix": 24},
                {"id": "pc1", "kind": "pc", "name": "PC1"},
                {"id": "sw2", "kind": "switch", "name": "SW2", "gateway": "10.0.0.254"},
            ],
            "links": [
                {
                    "id": "l1",
                    "source": "sw1",
                    "source_port": "GE0/0/1",
                    "target": "pc1",
                    "target_port": "Ethernet0/0/1",
                },
                {
                    "id": "l2",
                    "source": "sw1",
                    "source_port": "UNMAPPED",
                    "target": "sw2",
                    "target_port": "GE0/0/2",
                },
            ],
        }
    )

    assert [item["name"] for item in context["devices"]] == ["SW1", "PC1", "SW2"]
    assert context["devices"][0]["ip"] == "10.0.0.1"
    assert context["devices"][1]["ip"] == "未提供"
    assert context["devices"][2]["gateway"] == "10.0.0.254"
    assert context["devices"][0]["prefix"] == 24
    assert context["devices"][2]["prefix"] == "未提供"
    assert len(context["links"]) == 2
    assert context["links"][1]["source"]["port"] == "未提供"
    assert context["coverage"]["switch_to_switch_links"] == 1
    assert context["coverage"]["switch_to_pc_links"] == 1
    assert context["coverage"]["all_saved_links_included"] is True
    assert context["coverage"]["topology_input_status"] == "partial"
    assert context["coverage"]["missing_link_endpoint_count"] == 1


def test_multi_vlan_l2_requirement_marks_inter_switch_trunk_as_a_planning_capability() -> None:
    graph = {
        "nodes": [
            {"id": "sw1", "kind": "switch", "name": "SW1"},
            {"id": "sw2", "kind": "switch", "name": "SW2"},
            {"id": "pc1", "kind": "pc", "name": "PC1"},
            {"id": "pc2", "kind": "pc", "name": "PC2"},
            {"id": "pc3", "kind": "pc", "name": "PC3"},
            {"id": "pc4", "kind": "pc", "name": "PC4"},
        ],
        "links": [
            {
                "source": "sw1",
                "source_port": "GE0/0/1",
                "target": "pc1",
                "target_port": "Ethernet0/0/1",
            },
            {
                "source": "sw1",
                "source_port": "GE0/0/2",
                "target": "pc2",
                "target_port": "Ethernet0/0/1",
            },
            {
                "source": "sw1",
                "source_port": "GE0/0/3",
                "target": "sw2",
                "target_port": "GE0/0/3",
            },
            {
                "source": "sw2",
                "source_port": "GE0/0/1",
                "target": "pc3",
                "target_port": "Ethernet0/0/1",
            },
            {
                "source": "sw2",
                "source_port": "GE0/0/2",
                "target": "pc4",
                "target_port": "Ethernet0/0/1",
            },
        ],
    }

    intent = _derive_intent(
        "PC1和PC3属于VLAN10，PC2和PC4属于VLAN20，使用MSTP消除二层冗余风险。", graph
    )

    assert intent["feature"] == "vlan_access"
    assert intent["topology_capabilities"] == ["vlan_access", "vlan_trunk"]


def test_composite_vlan_plan_displays_llm_steps_for_the_non_vlan_capability() -> None:
    graph = {
        "nodes": [
            {"id": "sw1", "kind": "switch", "name": "SW1"},
            {"id": "sw2", "kind": "switch", "name": "SW2"},
            {"id": "pc1", "kind": "pc", "name": "PC1"},
            {"id": "pc2", "kind": "pc", "name": "PC2"},
        ],
        "links": [
            {
                "source": "sw1",
                "source_port": "GE0/0/1",
                "target": "pc1",
                "target_port": "Ethernet0/0/1",
            },
            {
                "source": "sw1",
                "source_port": "GE0/0/2",
                "target": "sw2",
                "target_port": "GE0/0/2",
            },
            {
                "source": "sw2",
                "source_port": "GE0/0/1",
                "target": "pc2",
                "target_port": "Ethernet0/0/1",
            },
        ],
    }
    intent = _derive_intent(
        "PC1和PC2属于VLAN10，使用OSPF IPv4发布三层网段。", graph
    )
    intent.update(
        {
            "renderer_mode": "huawei_vlan",
            "planning_capabilities": ["vlan_access", "vlan_trunk", "l3_ospf_ipv4"],
            "planning_steps": ["创建 VLAN 并配置接入口。", "启用 OSPF 并验证邻居。"],
        }
    )

    plan = _planning_idea_text("PC1和PC2属于VLAN10，使用OSPF IPv4发布三层网段。", graph, intent)

    assert "2. 启用 OSPF 并验证邻居。" in plan
    assert "这是包含多个能力的 LLM 规划草案。" in plan


def test_composite_capability_uses_generic_renderer_but_pure_vlan_keeps_huawei_renderer() -> None:
    composite = {
        "feature": "multi_vlan_intervlan",
        "renderer_mode": "huawei_vlan",
        "planning_capabilities": [
            "vlan_access",
            "vlan_trunk",
            "vlanif_gateway",
            "l3_ospf_ipv4",
        ],
    }
    pure_vlan = {
        "feature": "multi_vlan_intervlan",
        "renderer_mode": "huawei_vlan",
        "planning_capabilities": ["vlan_access", "vlan_trunk", "vlanif_gateway"],
    }

    assert _renderer_mode_for_intent(composite, HUAWEI_VRP) == "generic_evidence_bound"
    assert _renderer_mode_for_intent(pure_vlan, HUAWEI_VRP) == "huawei_vlan"


def test_explicit_device_clause_scopes_configuration_facts_to_that_switch() -> None:
    graph = {
        "nodes": [
            {"id": "sw1", "kind": "switch", "name": "SW1"},
            {"id": "sw3", "kind": "switch", "name": "SW3"},
        ],
        "links": [
            {
                "id": "l1",
                "source": "sw1",
                "source_port": "GE0/0/1",
                "target": "sw3",
                "target_port": "GE0/0/1",
            }
        ],
    }
    intent = _derive_intent(
        "SW3 配置 Vlanif10 地址 10.10.10.1/24。SW1 的 GE0/0/1 配置 172.16.0.1/30。",
        graph,
    )

    sw1_facts = _intent_for_device(intent, "sw1")["required_configuration_facts"]
    sw3_facts = _intent_for_device(intent, "sw3")["required_configuration_facts"]

    assert {item["address"] for item in sw1_facts} == {"172.16.0.1"}
    assert {item["address"] for item in sw3_facts} == {"10.10.10.1"}


def test_llm_planning_text_survives_a_specialized_feature_label_difference(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        llm_refinement,
        "read_provider_settings",
        lambda _session: SimpleNamespace(
            llm_base_url="http://llm.example/v1/",
            llm_model="test-model",
            llm_temperature=0.2,
            llm_thinking_mode="off",
        ),
    )
    monkeypatch.setattr(llm_refinement, "get_provider_secret", lambda _kind: "test-key")

    async def fake_request_text_result(**_kwargs):  # type: ignore[no-untyped-def]
        return LlmTextResult(
            content=json.dumps(
                {
                    "action": "refine_intent",
                    "feature": "inter_vlan_routing",
                    "capabilities": ["vlan_access", "vlan_trunk", "vlanif_gateway", "l3_ospf_ipv4"],
                    "vlan_ids": [10, 20],
                    "retrieval_terms": ["vlan batch", "VLANIF"],
                    "planning_steps": ["创建 VLAN 并配置接入口。", "在三层交换机配置 VLANIF 网关。"],
                    "reason_summary": "通过三层交换机的 VLANIF 实现 VLAN 10 与 VLAN 20 互通。",
                },
                ensure_ascii=False,
            ),
            thinking_requested=False,
            thinking_used=False,
        )

    monkeypatch.setattr(llm_refinement, "request_text_result", fake_request_text_result)
    baseline = {
        "feature": "multi_vlan_intervlan",
        "vlan_ids": [10, 20],
        "pc_vlan_map": {"pc1": 10, "pc2": 20},
        "topology_capabilities": ["vlan_access", "vlan_trunk", "vlanif_gateway"],
        "planning_capabilities": ["vlan_access", "vlan_trunk", "vlanif_gateway"],
    }

    outcome = llm_refinement.refine_intent_with_llm(
        session, requirement="PC1 属于 VLAN10，PC2 属于 VLAN20，要求互通。", baseline=baseline
    )

    assert outcome["llm"]["status"] == "accepted_with_topology_facts"
    assert outcome["intent"]["feature"] == "multi_vlan_intervlan"
    assert outcome["intent"]["planning_summary"].startswith("通过三层交换机")
    assert outcome["intent"]["planning_steps"] == [
        "创建 VLAN 并配置接入口。",
        "在三层交换机配置 VLANIF 网关。",
    ]
    assert outcome["intent"]["planning_capabilities"] == [
        "vlan_access",
        "vlan_trunk",
        "vlanif_gateway",
        "l3_ospf_ipv4",
        "inter_vlan_routing",
    ]


def test_active_retrieval_promotes_exact_tail_query_grammar_without_embedding_retry(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="commands.html",
        stored_path="commands.html",
        source_sha256="7" * 64,
        brand="Huawei",
        file_format="html",
        status=ImportStatus.completed,
    )
    session.add(manual)
    session.flush()
    _command(session, manual, "interface", ["interface { interface-name }"])
    _command(session, manual, "mode（Eth-Trunk接口视图）", ["mode { lacp-static | manual }"])
    _command(session, manual, "stp enable", ["stp enable"])
    _command(session, manual, "stp mode", ["stp mode { mstp | rstp }"])
    session.commit()

    monkeypatch.setattr(planning_service, "_find_evidence", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        planning_service,
        "active_manual_search",
        lambda *_args, **_kwargs: {
            "status": "incomplete",
            "selected_command_ids": [],
            "catalog_anchors": [],
            "candidates": [],
            "rounds": [
                {"tail_queries": ["mode lacp-static", "stp enable", "stp mode"]},
            ],
        },
    )

    outcome = planning_service._active_evidence_recovery(
        session,
        manual_id=manual.id,
        requirement="配置静态 LACP 并启用 MSTP",
        intent={"feature": "generic", "renderer_mode": "generic_evidence_bound"},
        dialect=HUAWEI_VRP,
    )

    assert {
        item["canonical_name"] for item in outcome["evidence"]
    } >= {"mode（Eth-Trunk接口视图）", "stp enable", "stp mode"}
    assert all(
        "manual_exact_query_anchor" in item["retrieval_sources"]
        for item in outcome["evidence"]
        if item["canonical_name"] in {"mode（Eth-Trunk接口视图）", "stp enable", "stp mode"}
    )


def test_manual_selected_planning_needs_no_model_and_excludes_equivalent_protected_port(session) -> None:  # type: ignore[no-untyped-def]
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
    assert task.status.value == "idea_ready"
    assert task.planning_idea.strip()
    original_idea = task.planning_idea
    task = update_planning_idea(session, task.id, "")
    with pytest.raises(ValueError, match="配置思路为空"):
        generate_config_commands(session, task.id)
    task = update_planning_idea(session, task.id, original_idea)
    task = generate_config_commands(session, task.id)
    assert task.status.value == "needs_review"
    assert task.device_plans[0].compatibility_status.value == "manual_selected"
    assert task.device_plans[0].detected_model is None
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
    blocked_task = generate_config_commands(session, blocked_task.id)
    assert blocked_task.status.value == "needs_review"
    # Relaxed planning keeps an editable draft even when the drawn port is
    # marked protected; the warning is shown for the operator to decide.
    assert json.loads(blocked_task.device_plans[0].commands_json)
    assert "受保护端口" in "；".join(json.loads(blocked_task.device_plans[0].validation_json)["warnings"])


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


def test_saved_topology_can_be_updated_as_a_new_revision(session) -> None:  # type: ignore[no-untyped-def]
    created = create_topology(
        session,
        TopologyDraft.model_validate(
            {
                "name": "接入实验",
                "nodes": [
                    {"id": "sw1", "kind": "switch", "name": "SW1", "x": 10, "y": 20},
                    {"id": "pc1", "kind": "pc", "name": "PC1", "x": 200, "y": 20},
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
    updated = update_topology(
        session,
        created.topology_id,
        TopologyDraft.model_validate(
            {
                "name": "接入实验-已编辑",
                "nodes": [
                    {"id": "sw1", "kind": "switch", "name": "SW1", "x": 30, "y": 40},
                    {"id": "pc1", "kind": "pc", "name": "PC1", "x": 260, "y": 40},
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

    loaded = get_topology_revision(session, created.topology_id)
    assert created.revision == 1
    assert updated.revision == 2
    assert loaded is not None
    assert loaded.id == updated.id
    assert loaded.topology.name == "接入实验-已编辑"
    assert json.loads(loaded.graph_json)["nodes"][0]["x"] == 30


def test_vlan_access_uses_interface_syntax_when_manual_has_duplicate_command_names() -> None:
    evidence = [
        {"canonical_name": "vlan batch", "syntax": ["vlan batch { vlan-id1 }"]},
        {"canonical_name": "port link-type", "syntax": ["port link-type access"]},
        {"canonical_name": "port default vlan", "syntax": ["port default vlan vlan-id"]},
        # A similarly named remote-unit command must not replace the interface command.
        {"canonical_name": "port default vlan", "syntax": ["port { port-id1 } default vlan vlan-id"]},
    ]

    commands, validation = _candidate_commands(
        {"feature": "vlan_access", "vlan_ids": [10]},
        evidence,
        ["GE0/0/1"],
    )

    assert validation["status"] == "ready"
    assert commands == [
        "system-view",
        "vlan batch 10",
        "interface GE0/0/1",
        "port link-type access",
        "port default vlan 10",
        "quit",
        "return",
    ]


def test_multi_vlan_intervlan_compiles_access_trunk_and_vlanif_from_drawn_topology(
    session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="huawei.html",
        stored_path="huawei.html",
        source_sha256="1" * 64,
        brand="Huawei",
        release="test",
        file_format="html",
        status=ImportStatus.completed,
    )
    session.add(manual)
    session.flush()
    _command(session, manual, "vlan batch", ["vlan batch { vlan-id1 [ to vlan-id2 ] }"])
    _command(session, manual, "port link-type", ["port link-type access", "port link-type trunk"])
    _command(session, manual, "port default vlan", ["port default vlan vlan-id"])
    _command(session, manual, "port trunk allow-pass vlan", ["port trunk allow-pass vlan vlan-id1"])
    _command(session, manual, "interface", ["interface { interface-name | interface-type interface-number }"])
    _command(session, manual, "ip address", ["ip address ip-address { mask | mask-length }"], ["VLANIF interface view"])
    session.commit()

    topology = create_topology(
        session,
        TopologyDraft.model_validate(
            {
                "name": "双接入双 VLAN 三层互通",
                "nodes": [
                    {"id": "sw1", "kind": "switch", "name": "SW1", "x": 0, "y": 0},
                    {"id": "sw2", "kind": "switch", "name": "SW2", "x": 0, "y": 200},
                    {"id": "sw3", "kind": "switch", "name": "SW3", "x": 280, "y": 100},
                    {"id": "pc1", "kind": "pc", "name": "PC1", "x": 500, "y": 0, "ip": "10.10.10.11", "prefix": 24, "gateway": "10.10.10.1"},
                    {"id": "pc2", "kind": "pc", "name": "PC2", "x": 500, "y": 60, "ip": "10.20.20.12", "prefix": 24, "gateway": "10.20.20.1"},
                    {"id": "pc3", "kind": "pc", "name": "PC3", "x": 500, "y": 200, "ip": "10.10.10.13", "prefix": 24, "gateway": "10.10.10.1"},
                    {"id": "pc4", "kind": "pc", "name": "PC4", "x": 500, "y": 260, "ip": "10.20.20.14", "prefix": 24, "gateway": "10.20.20.1"},
                ],
                "links": [
                    {"id": "pc1", "source": "sw1", "source_port": "GE0/0/1", "target": "pc1", "target_port": "Ethernet0/0/1"},
                    {"id": "pc2", "source": "sw1", "source_port": "GE0/0/2", "target": "pc2", "target_port": "Ethernet0/0/1"},
                    {"id": "u1", "source": "sw1", "source_port": "GE0/0/3", "target": "sw3", "target_port": "GE0/0/1"},
                    {"id": "pc3", "source": "sw2", "source_port": "GE0/0/1", "target": "pc3", "target_port": "Ethernet0/0/1"},
                    {"id": "pc4", "source": "sw2", "source_port": "GE0/0/2", "target": "pc4", "target_port": "Ethernet0/0/1"},
                    {"id": "u2", "source": "sw2", "source_port": "GE0/0/3", "target": "sw3", "target_port": "GE0/0/2"},
                ],
            }
        ),
    )
    task = create_config_task(
        session,
        ConfigTaskCreate(
            topology_revision_id=topology.id,
            manual_id=manual.id,
            requirement_text=(
                "PC1 与 PC3 属于 VLAN 10。PC2 与 PC4 属于 VLAN 20。"
                "SW1、SW2 与 SW3 的交换机链路承载 VLAN 10 和 VLAN 20。"
                "SW3 是三层核心网关，使 VLAN 10 与 VLAN 20 之间三层互通。"
            ),
        ),
    )
    assert task.status.value == "idea_ready"
    llm_call_count = 0

    def shared_llm_plan(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal llm_call_count
        llm_call_count += 1
        # This deliberately contains SW1's literal Access CLI. The command
        # planner cache may reuse handbook selection, but SW3 must never reuse
        # these device-specific lines.
        return {
            "command_plan": {
                "operations": [
                    {
                        "invocations": [
                            {"cli": "system-view"},
                            {"cli": "interface GE0/0/1"},
                            {"cli": "port link-type access"},
                            {"cli": "port default vlan 10"},
                        ]
                    }
                ]
            },
            "llm": {"status": "stubbed"},
        }

    monkeypatch.setattr(planning_service, "_llm_command_plan_outcome", shared_llm_plan)
    task = generate_config_commands(session, task.id)

    plans = {plan.display_name: json.loads(plan.commands_json) for plan in task.device_plans}
    assert task.status.value == "needs_review"
    assert plans["SW1"] == [
        "system-view", "vlan batch 10 20",
        "interface GE0/0/1", "port link-type access", "port default vlan 10", "quit",
        "interface GE0/0/2", "port link-type access", "port default vlan 20", "quit",
        "interface GE0/0/3", "port link-type trunk", "port trunk allow-pass vlan 10 20", "quit", "return",
    ]
    assert plans["SW2"] == plans["SW1"]
    assert plans["SW3"] == [
        "system-view", "vlan batch 10 20",
        "interface GE0/0/1", "port link-type trunk", "port trunk allow-pass vlan 10 20", "quit",
        "interface GE0/0/2", "port link-type trunk", "port trunk allow-pass vlan 10 20", "quit",
        "interface Vlanif10", "ip address 10.10.10.1 255.255.255.0", "quit",
        "interface Vlanif20", "ip address 10.20.20.1 255.255.255.0", "quit", "return",
    ]
    assert llm_call_count == 1


def test_generic_plan_accepts_evidence_keywords_separated_by_parameters() -> None:
    """VRP places the VRID argument between the command's literal keywords."""

    evidence = [
        {
            "command_id": "vrrp-priority",
            "canonical_name": "vrrp vrid priority",
            "syntax": ["vrrp vrid virtual-router-id priority priority-value"],
        }
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "提高 VRRP 优先级",
                    "invocations": [
                        {
                            "command_id": "vrrp-priority",
                            "syntax_index": 0,
                            "cli": "vrrp vrid 10 priority 120",
                        }
                    ],
                }
            ],
            "validation_commands": ["display vrrp"],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={"renderer_mode": "generic_evidence_bound"},
        evidence=evidence,
        topology_ports=[],
        dialect=HUAWEI_VRP,
    )

    assert validation["status"] == "ready"
    assert commands == ["system-view", "vrrp vrid 10 priority 120", "return"]


def test_generic_plan_rejects_control_only_draft() -> None:
    """A view exit is never a complete configuration plan by itself."""

    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "错误的空草案",
                    "invocations": [{"command_id": "__control__", "cli": "quit"}],
                }
            ],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={"renderer_mode": "generic_evidence_bound"},
        evidence=[],
        topology_ports=[],
        dialect=HUAWEI_VRP,
    )

    assert commands == []
    assert validation["status"] == "blocked"
    assert validation["errors"] == ["通用计划没有可编译的证据绑定业务 CLI。"]


def test_generic_missing_plan_keeps_a_visible_manual_reference_draft() -> None:
    """No generic feature may collapse to an empty command panel."""

    commands, validation = _render_command_plan_or_fallback(
        {"renderer_mode": "generic_evidence_bound"},
        [
            {
                "canonical_name": "vrrp vrid priority",
                "examples": ["vrrp vrid 10 priority 120"],
            }
        ],
        [],
        None,
        dialect=HUAWEI_VRP,
    )

    assert commands == ["system-view", "# 手册参考：vrrp vrid priority", "return"]
    assert validation["status"] == "draft_with_warnings"
    assert validation["unverified_draft"] is True
    assert validation["non_executable_reference"] is True


def test_generic_missing_plan_without_evidence_keeps_editable_placeholder() -> None:
    commands, validation = _render_command_plan_or_fallback(
        {"renderer_mode": "generic_evidence_bound"},
        [],
        [],
        None,
        dialect=HUAWEI_VRP,
    )

    assert commands == [
        "system-view",
        "# 未检索到手册命令或 LLM CLI；请补充需求、手册内容或手动填写命令草案。",
        "return",
    ]
    assert validation["non_executable_reference"] is True


def test_generic_unverified_draft_keeps_line_rejected_by_manual_syntax_for_review() -> None:
    """A user-visible draft keeps invalid CLI with compiler errors for correction."""

    command_plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "iStack 链路草案",
                    "invocations": [
                        {
                            "command_id": "interface",
                            "cli": "interface 10GE1/0/3",
                            "target_port_ref": "topology:port:10GE1/0/3",
                        },
                        {
                            "command_id": "link-type",
                            "cli": "port link-type stack",
                        },
                    ],
                }
            ],
        }
    )
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name }"],
        },
        {
            "command_id": "link-type",
            "canonical_name": "port link-type",
            "syntax": ["port link-type access", "port link-type trunk"],
        },
    ]

    commands, validation = _render_command_plan_or_fallback(
        {"renderer_mode": "generic_evidence_bound"},
        evidence,
        ["10GE1/0/3"],
        command_plan.model_dump(mode="json"),
        dialect=HUAWEI_VRP,
    )

    assert commands == [
        "system-view",
        "interface 10GE1/0/3",
        "port link-type stack",
        "return",
    ]
    assert validation["status"] == "draft_with_warnings"
    assert "port link-type stack" in commands
    assert any("未完成手册静态校验" in item for item in validation["warnings"])


def test_generic_plan_requires_an_explicitly_requested_interface_address() -> None:
    evidence = [
        {
            "command_id": "static-route",
            "canonical_name": "ip route-static",
            "syntax": ["ip route-static ip-address mask next-hop-address"],
        }
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "配置远端静态路由",
                    "invocations": [
                        {
                            "command_id": "static-route",
                            "syntax_index": 0,
                            "cli": "ip route-static 192.168.20.0 24 10.0.12.2",
                        }
                    ],
                }
            ],
            "validation_commands": ["display ip routing-table 192.168.20.0 24"],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={
            "renderer_mode": "generic_evidence_bound",
            "required_configuration_facts": [
                {
                    "kind": "interface_address",
                    "port": "GE0/0/1",
                    "address": "10.0.12.1",
                    "prefix": "30",
                }
            ],
        },
        evidence=evidence,
        topology_ports=["GE0/0/1"],
        dialect=HUAWEI_VRP,
    )

    assert commands == []
    assert validation["status"] == "blocked"
    assert "GE0/0/1" in validation["errors"][0]


def test_intent_extracts_an_interface_address_after_peer_description() -> None:
    intent = _derive_intent(
        "当前设备 SW1 的 GE0/0/1 接 SW2，配置 10.0.12.1/30；对端地址为 10.0.12.2/30。",
        {"nodes": [], "links": []},
    )

    assert intent["required_configuration_facts"] == [
        {
            "kind": "interface_address",
            "port": "GE0/0/1",
            "address": "10.0.12.1",
            "prefix": "30",
        }
    ]


def test_intent_records_an_existing_interface_address_without_scheduling_it() -> None:
    intent = _derive_intent(
        "当前设备 SW1 的 GE0/0/1 已配置 10.0.12.1/30；只配置到 192.168.20.0/24 的静态路由。",
        {
            "nodes": [{"id": "sw1", "kind": "switch"}],
            "links": [
                {
                    "source": "sw1",
                    "source_port": "GE0/0/1",
                    "target": "peer1",
                    "target_port": "Ethernet0/0/1",
                }
            ],
        },
    )

    assert intent["required_configuration_facts"] == []
    assert intent["existing_configuration_facts"] == [
        {
            "kind": "existing_interface_address",
            "port": "GE0/0/1",
            "address": "10.0.12.1",
            "prefix": "30",
        }
    ]


def test_intent_resolves_a_unique_deictic_interface_address_from_topology() -> None:
    intent = _derive_intent(
        "Branch1 与 Branch2 通过已绘制的 GE0/0/3 点对点互联。当前设备 Branch1 在该口配置 172.20.12.1/30。",
        {
            "nodes": [],
            "links": [
                {
                    "source": "sw1",
                    "source_port": "GE0/0/3",
                    "target": "sw2",
                    "target_port": "GE0/0/3",
                }
            ],
        },
    )

    assert {
        "kind": "interface_address",
        "port": "GE0/0/3",
        "address": "172.20.12.1",
        "prefix": "30",
    } in intent["required_configuration_facts"]


def test_intent_ignores_a_peer_pc_port_when_resolving_deictic_switch_address() -> None:
    intent = _derive_intent(
        "Branch1 在该口配置 172.20.12.1/30。",
        {
            "nodes": [
                {"id": "sw1", "kind": "switch"},
                {"id": "pc1", "kind": "pc"},
            ],
            "links": [
                {
                    "source": "sw1",
                    "source_port": "GE0/0/3",
                    "target": "pc1",
                    "target_port": "Ethernet0/0/1",
                }
            ],
        },
    )

    assert {
        "kind": "interface_address",
        "port": "GE0/0/3",
        "address": "172.20.12.1",
        "prefix": "30",
    } in intent["required_configuration_facts"]


def test_intent_extracts_a_named_logical_interface_address() -> None:
    intent = _derive_intent(
        "SW1 需要创建 Eth-Trunk 10；聚合口作为三层口，配置地址 10.0.12.1/30。",
        {"nodes": [], "links": []},
    )

    assert {
        "kind": "logical_interface_address",
        "interface": "Eth-Trunk 10",
        "address": "10.0.12.1",
        "prefix": "30",
    } in intent["required_configuration_facts"]


def test_intent_does_not_assign_an_independent_peer_address_to_current_device() -> None:
    intent = _derive_intent(
        "当前设备 Core1 配置 Vlanif10 地址 192.168.10.2/24。"
        "Core2 会独立配置 Vlanif10 地址 192.168.10.3/24。",
        {"nodes": [{"id": "sw1", "kind": "switch", "name": "Core1"}], "links": []},
    )

    assert intent["required_configuration_facts"] == [
        {
            "kind": "logical_interface_address",
            "interface": "Vlanif10",
            "address": "192.168.10.2",
            "prefix": "24",
        }
    ]


def test_intent_keeps_current_device_independent_address_action() -> None:
    intent = _derive_intent(
        "当前设备 Core1 需要独立配置 Vlanif10 地址 192.168.10.2/24。",
        {"nodes": [{"id": "sw1", "kind": "switch", "name": "Core1"}], "links": []},
    )

    assert {
        "kind": "logical_interface_address",
        "interface": "Vlanif10",
        "address": "192.168.10.2",
        "prefix": "24",
    } in intent["required_configuration_facts"]


def test_intent_scopes_multiple_named_device_addresses_in_one_clause() -> None:
    graph = {
        "nodes": [
            {"id": "sw3", "kind": "switch", "name": "SW3"},
            {"id": "sw4", "kind": "switch", "name": "SW4"},
        ],
        "links": [
            {
                "source": "sw3",
                "source_port": "GE0/0/3",
                "target": "sw4",
                "target_port": "GE0/0/1",
            }
        ],
    }
    intent = _derive_intent(
        "SW3 的 GE0/0/3 配置IP地址172.16.3.1/30，SW4 的 GE0/0/1 配置IP地址172.16.3.2/30。",
        graph,
    )

    assert _intent_for_device(intent, "sw3")["required_configuration_facts"] == [
        {
            "kind": "interface_address",
            "port": "GE0/0/3",
            "address": "172.16.3.1",
            "prefix": "30",
            "device_node_id": "sw3",
        }
    ]
    assert _intent_for_device(intent, "sw4")["required_configuration_facts"] == [
        {
            "kind": "interface_address",
            "port": "GE0/0/1",
            "address": "172.16.3.2",
            "prefix": "30",
            "device_node_id": "sw4",
        }
    ]


def test_intent_extracts_named_logical_address_without_configuration_verb() -> None:
    graph = {
        "nodes": [
            {"id": "core1", "kind": "switch", "name": "Core1"},
            {"id": "core2", "kind": "switch", "name": "Core2"},
        ],
        "links": [],
    }
    intent = _derive_intent(
        "Core1 的 Vlanif10 地址 10.10.10.2/24、优先级120，Core2 的 Vlanif10 地址 10.10.10.3/24、优先级100。",
        graph,
    )

    assert {
        "kind": "logical_interface_address",
        "interface": "Vlanif10",
        "address": "10.10.10.2",
        "prefix": "24",
        "device_node_id": "core1",
    } in intent["required_configuration_facts"]
    assert {
        "kind": "logical_interface_address",
        "interface": "Vlanif10",
        "address": "10.10.10.3",
        "prefix": "24",
        "device_node_id": "core2",
    } in intent["required_configuration_facts"]


def test_generic_plan_uses_manual_examples_to_reject_wrong_virtual_interface_form() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name | interface-type interface-number }"],
        },
        {
            "command_id": "stack-port",
            "canonical_name": "stack-port",
            "syntax": ["stack-port portnum"],
            "examples": ["system-view interface stack-port 1 quit"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "创建堆叠逻辑口",
                    "invocations": [
                        {
                            "command_id": "interface",
                            "syntax_index": 0,
                            "cli": "interface stack-port 1/1",
                        }
                    ],
                }
            ],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={"renderer_mode": "generic_evidence_bound"},
        evidence=evidence,
        topology_ports=[],
        dialect=HUAWEI_VRP,
    )

    assert commands == []
    assert validation["status"] == "blocked"
    assert "标识形式不符合" in validation["errors"][0]


def test_generic_plan_requires_interface_exit_and_normalizes_to_topology_port_spelling() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name | interface-type interface-number }"],
        }
    ]
    missing_quit = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "进入两个逻辑接口",
                    "invocations": [
                        {"command_id": "interface", "cli": "interface Vlanif 10"},
                        {"command_id": "interface", "cli": "interface Vlanif 20"},
                    ],
                }
            ],
        }
    )
    rewritten_port = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "进入拓扑端口",
                    "invocations": [
                        {
                            "command_id": "interface",
                            "target_port_ref": "topology:port:GE0/0/1",
                            "cli": "interface GigabitEthernet 0/0/1",
                        }
                    ],
                }
            ],
        }
    )

    _, missing_quit_validation = compile_command_plan(
        missing_quit,
        intent={"renderer_mode": "generic_evidence_bound"},
        evidence=evidence,
        topology_ports=[],
        dialect=HUAWEI_VRP,
    )
    rewritten_port_commands, rewritten_port_validation = compile_command_plan(
        rewritten_port,
        intent={"renderer_mode": "generic_evidence_bound"},
        evidence=evidence,
        topology_ports=["GE0/0/1"],
        dialect=HUAWEI_VRP,
    )

    assert "必须先退出当前接口视图" in missing_quit_validation["errors"][0]
    assert rewritten_port_validation["status"] == "ready"
    assert rewritten_port_commands == ["system-view", "interface GE0/0/1", "return"]


def test_command_plan_splits_an_oversized_operation_without_changing_invocation_order() -> None:
    raw = json.dumps(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "长操作",
                    "invocations": [
                        {"command_id": "cmd", "cli": f"display value {index}"}
                        for index in range(9)
                    ],
                }
            ],
            "verification_notes": [],
            "validation_commands": [],
            "assumptions": [],
            "risks": [],
        }
    )

    plan = LlmCommandPlan.model_validate_json(llm_command_plan._normalize_operation_cardinality(raw))

    assert [len(operation.invocations) for operation in plan.operations] == [8, 1]
    assert [item.cli for operation in plan.operations for item in operation.invocations] == [
        f"display value {index}" for index in range(9)
    ]


def test_command_plan_repairs_a_schema_only_llm_response(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        llm_base_url="https://example.invalid/v1",
        llm_model="test-model",
        llm_temperature=0.1,
        llm_thinking_mode="adaptive",
    )
    responses = iter(
        [
            LlmTextResult(
                content='{"action":"command_plan","operations":[]}',
                thinking_requested=True,
                thinking_used=True,
            ),
            LlmTextResult(
                content=(
                    '{"action":"command_plan","operations":[{"purpose":"test","invocations":['
                    '{"command_id":"cmd","syntax_index":0,"arguments":{},'
                    '"target_port_ref":null,"cli":"display version"}]}],'
                    '"verification_notes":[],"validation_commands":["display version"],'
                    '"assumptions":[],"risks":[]}'
                ),
                thinking_requested=False,
                thinking_used=False,
            ),
        ]
    )
    requests: list[dict[str, object]] = []

    async def fake_request_text_result(**kwargs):  # type: ignore[no-untyped-def]
        requests.append(kwargs)
        return next(responses)

    monkeypatch.setattr(llm_command_plan, "read_provider_settings", lambda _session: settings)
    monkeypatch.setattr(llm_command_plan, "get_provider_secret", lambda _provider: "test-key")
    monkeypatch.setattr(llm_command_plan, "request_text_result", fake_request_text_result)

    plan, audit = llm_command_plan.plan_commands_with_llm(
        None,
        requirement="检查版本",
        intent={"renderer_mode": "generic_evidence_bound"},
        evidence=[],
        topology_ports=[],
        dialect=HUAWEI_VRP,
    )

    assert plan is not None
    assert plan.operations[0].invocations[0].cli == "display version"
    assert audit["format_repair_attempted"] is True
    assert len(requests) == 2
    assert requests[0]["thinking"] is True
    assert requests[0]["stream"] is False
    assert requests[1]["thinking"] is False


def test_command_plan_requests_plain_cli_after_a_control_only_json_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        llm_base_url="https://example.invalid/v1",
        llm_model="test-model",
        llm_temperature=0.1,
        llm_thinking_mode="adaptive",
    )
    responses = iter(
        [
            LlmTextResult(
                content=(
                    '{"action":"command_plan","operations":[{"purpose":"empty","invocations":['
                    '{"command_id":"__control__","syntax_index":0,"arguments":{},'
                    '"target_port_ref":null,"cli":"quit"}]}],'
                    '"verification_notes":[],"validation_commands":[],"assumptions":[],"risks":[]}'
                ),
                thinking_requested=True,
                thinking_used=True,
            ),
            LlmTextResult(
                content="interface GE0/0/1\nport link-type access\nport default vlan 10",
                thinking_requested=False,
                thinking_used=False,
            ),
        ]
    )

    async def fake_request_text_result(**_kwargs):  # type: ignore[no-untyped-def]
        return next(responses)

    monkeypatch.setattr(llm_command_plan, "read_provider_settings", lambda _session: settings)
    monkeypatch.setattr(llm_command_plan, "get_provider_secret", lambda _provider: "test-key")
    monkeypatch.setattr(llm_command_plan, "request_text_result", fake_request_text_result)

    plan, audit = llm_command_plan.plan_commands_with_llm(
        None,
        requirement="将端口加入 VLAN 10",
        intent={"renderer_mode": "generic_evidence_bound"},
        evidence=[],
        topology_ports=["GE0/0/1"],
        dialect=HUAWEI_VRP,
    )

    assert plan is not None
    assert [
        item.cli for operation in plan.operations for item in operation.invocations
    ] == ["interface GE0/0/1", "port link-type access", "port default vlan 10"]
    assert audit["status"] == "unverified_plain_cli_draft"


def test_command_plan_retries_with_a_smaller_prompt_after_unexpected_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        llm_base_url="https://example.invalid/v1",
        llm_model="test-model",
        llm_temperature=0.1,
        llm_thinking_mode="adaptive",
    )
    response = LlmTextResult(
        content=(
            '{"action":"command_plan","operations":[{"purpose":"test","invocations":['
            '{"command_id":"cmd","syntax_index":0,"arguments":{},'
            '"target_port_ref":null,"cli":"display version"}]}],'
            '"verification_notes":[],"validation_commands":["display version"],'
            '"assumptions":[],"risks":[]}'
        ),
        thinking_requested=False,
        thinking_used=False,
    )
    requests: list[dict[str, object]] = []

    async def fake_request_text_result(**kwargs):  # type: ignore[no-untyped-def]
        requests.append(kwargs)
        if len(requests) == 1:
            raise llm_command_plan.ThinkingBudgetExceeded("unexpected reasoning")
        return response

    monkeypatch.setattr(llm_command_plan, "read_provider_settings", lambda _session: settings)
    monkeypatch.setattr(llm_command_plan, "get_provider_secret", lambda _provider: "test-key")
    monkeypatch.setattr(llm_command_plan, "request_text_result", fake_request_text_result)

    plan, audit = llm_command_plan.plan_commands_with_llm(
        None,
        requirement="检查版本",
        intent={
            "renderer_mode": "generic_evidence_bound",
            "confirmed_planning_idea": "规划说明" * 1000,
        },
        evidence=[],
        topology_ports=[],
        dialect=HUAWEI_VRP,
    )

    assert plan is not None
    assert audit["compact_retry_attempted"] is True
    assert len(requests) == 2
    assert len(str(requests[1]["messages"])) < len(str(requests[0]["messages"]))


def test_intent_maps_multiple_pc_vlan_assignments_in_one_sentence() -> None:
    graph = {
        "nodes": [
            {"id": "sw1", "kind": "switch", "name": "SW1"},
            {"id": "sw2", "kind": "switch", "name": "SW2"},
            {"id": "sw3", "kind": "switch", "name": "SW3"},
            *[{"id": f"pc{index}", "kind": "pc", "name": f"PC{index}"} for index in range(1, 5)],
        ],
        "links": [],
    }

    intent = _derive_intent(
        "PC1 与 PC3 属于 VLAN 10，PC2 与 PC4 属于 VLAN 20。"
        "SW3 是三层核心网关，使 VLAN 10 与 VLAN 20 之间三层互通。",
        graph,
    )

    assert intent["feature"] == "multi_vlan_intervlan"
    assert intent["pc_vlan_map"] == {"pc1": 10, "pc2": 20, "pc3": 10, "pc4": 20}


def test_vlanif_only_requirement_stays_on_generic_evidence_bound_path() -> None:
    intent = _derive_intent(
        "Core1 创建 VLAN 10，配置 Vlanif10 地址并配置 VRRP；不要配置物理端口或 Access。",
        {"nodes": [{"id": "core1", "kind": "switch", "name": "Core1"}], "links": []},
    )

    assert intent["feature"] == "generic"
