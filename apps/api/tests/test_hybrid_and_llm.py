from __future__ import annotations

import asyncio
import json

from app.agents.graph import build_planning_graph
from app.llm.client import (
    _next_stream_chunk,
    should_enable_thinking,
    thinking_extra_body,
)
from app.models import Command, KnowledgeDocument, Manual
from app.planning.dialect import CISCO_IOS, GENERIC_MANUAL, HUAWEI_VRP, resolve_cli_dialect
from app.planning.llm_command_plan import (
    _bind_plain_cli_draft_to_evidence,
    _matches_evidence_syntax,
    _plain_cli_draft_plan,
    _resolve_evidence_binding,
    build_explicit_port_assignment_fallback_plan,
    compile_command_plan,
    complete_command_plan_from_review,
    normalize_huawei_vlan_creation_plan,
    prune_command_plan_for_incomplete_syntax,
    prune_command_plan_for_known_facts,
    prune_command_plan_for_review_feedback,
)
from app.retrieval.hybrid import hybrid_command_search
from app.schemas import LlmCommandPlan


def _manual_with_command(session):  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="search.html",
        stored_path="search.html",
        source_sha256="1" * 64,
        file_format="html",
        brand="Huawei",
    )
    session.add(manual)
    session.flush()
    document = KnowledgeDocument(
        manual_id=manual.id,
        source_path="vlan-batch.html",
        title="vlan batch",
        text_content="Create VLANs in a batch.",
    )
    session.add(document)
    session.flush()
    command = Command(
        manual_id=manual.id,
        document_id=document.id,
        canonical_name="vlan batch",
        syntax_json=json.dumps(["vlan batch { vlan-id }"]),
        views_json="[]",
        parameters_json="[]",
        preconditions_json="[]",
        constraints_json="[]",
        examples_json="[]",
        evidence_json=json.dumps({"source_path": "vlan-batch.html"}),
    )
    session.add(command)
    session.commit()
    return manual


def test_hybrid_search_keeps_exact_name_when_fts_or_embedding_is_unavailable(session) -> None:  # type: ignore[no-untyped-def]
    manual = _manual_with_command(session)
    hits = hybrid_command_search(session, query="vlan batch", manual_id=manual.id, limit=5)
    assert hits
    assert hits[0].command.canonical_name == "vlan batch"
    assert "exact_name" in hits[0].sources


def test_plain_cli_fallback_keeps_business_commands_but_removes_session_and_unsafe_lines() -> None:
    plan = _plain_cli_draft_plan(
        """
        ```text
        system-view
        interface GE0/0/1
        undo portswitch
        ip address 10.0.0.1 30
        quit
        save
        return
        ```
        """,
        HUAWEI_VRP,
    )

    assert plan is not None
    commands = [
        invocation.cli
        for operation in plan.operations
        for invocation in operation.invocations
    ]
    assert commands == ["interface GE0/0/1", "undo portswitch", "ip address 10.0.0.1 30"]
    assert all(
        invocation.command_id == "__unverified_draft__"
        for operation in plan.operations
        for invocation in operation.invocations
    )


def test_command_plan_pruning_preserves_route_when_address_is_declared_existing() -> None:
    plan = _plain_cli_draft_plan(
        """
        interface GE0/0/1
        ip address 10.0.12.1 255.255.255.252
        quit
        ip route-static 192.168.20.0 255.255.255.0 10.0.12.2
        """,
        HUAWEI_VRP,
        topology_ports=["GE0/0/1"],
    )

    assert plan is not None
    pruned, removed = prune_command_plan_for_known_facts(
        plan,
        intent={
            "existing_configuration_facts": [
                {
                    "kind": "existing_interface_address",
                    "port": "GE0/0/1",
                    "address": "10.0.12.1",
                    "prefix": "30",
                }
            ]
        },
        dialect=HUAWEI_VRP,
    )

    assert removed == ["ip address 10.0.12.1 255.255.255.252"]
    assert [
        invocation.cli
        for operation in pruned.operations
        for invocation in operation.invocations
    ] == ["ip route-static 192.168.20.0 255.255.255.0 10.0.12.2"]


def test_command_plan_pruning_removes_only_quoted_reviewer_cli() -> None:
    plan = _plain_cli_draft_plan(
        """
        interface 10GE1/0/1
        port link-type trunk
        stack-port 1
        quit
        stack member 1 priority 150
        """,
        HUAWEI_VRP,
        topology_ports=["10GE1/0/1"],
    )

    assert plan is not None
    pruned, removed = prune_command_plan_for_review_feedback(
        plan,
        review={"issues": ["额外配置了'port link-type trunk'，需求未授权。"]},
        dialect=HUAWEI_VRP,
    )

    assert removed == ["port link-type trunk"]
    assert [
        invocation.cli
        for operation in pruned.operations
        for invocation in operation.invocations
    ] == ["interface 10GE1/0/1", "stack-port 1", "quit", "stack member 1 priority 150"]


def test_command_plan_pruning_removes_bare_parameterised_manual_title() -> None:
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "创建 VLAN",
                    "invocations": [
                        {"command_id": "vlan", "cli": "vlan batch 30"},
                        {"command_id": "vlan", "cli": "vlan batch"},
                    ],
                }
            ],
        }
    )
    pruned, removed = prune_command_plan_for_incomplete_syntax(
        plan,
        evidence=[
            {
                "command_id": "vlan",
                "canonical_name": "vlan batch",
                "syntax": ["vlan batch { vlan-id1 [ to vlan-id2 ] } &<1-10>"],
            }
        ],
        dialect=HUAWEI_VRP,
    )

    assert removed == ["vlan batch"]
    assert [
        invocation.cli
        for operation in pruned.operations
        for invocation in operation.invocations
    ] == ["vlan batch 30"]


def test_huawei_vlan_creation_uses_selected_manual_batch_syntax() -> None:
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "创建 VLAN",
                    "invocations": [{"command_id": "__unverified_draft__", "cli": "vlan 30"}],
                }
            ],
        }
    )
    normalized, rewrites = normalize_huawei_vlan_creation_plan(
        plan,
        intent={"vlan_ids": [30]},
        evidence=[
            {
                "command_id": "vlan-batch",
                "canonical_name": "vlan batch",
                "syntax": ["vlan batch { vlan-id1 [ to vlan-id2 ] } &<1-10>"],
            }
        ],
        dialect=HUAWEI_VRP,
    )

    assert rewrites == ["vlan batch 30"]
    invocation = normalized.operations[0].invocations[0]
    assert invocation.cli == "vlan batch 30"
    assert invocation.command_id == "vlan-batch"


def test_reviewer_required_evidence_command_is_inserted_in_matching_interface_view() -> None:
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "配置聚合口",
                    "invocations": [
                        {"command_id": "interface", "cli": "interface Eth-Trunk 20"},
                        {"command_id": "control", "cli": "quit"},
                    ],
                }
            ],
        }
    )
    completed, additions = complete_command_plan_from_review(
        plan,
        review={"required_changes": ["在 Eth-Trunk 20 接口视图配置 'mode lacp-static'。"]},
        evidence=[
            {
                "command_id": "interface",
                "canonical_name": "interface",
                "syntax": ["interface interface-name"],
                "views": ["system view"],
            },
            {
                "command_id": "mode",
                "canonical_name": "mode lacp-static",
                "syntax": ["mode lacp-static"],
                "views": ["Eth-Trunk interface view"],
            },
        ],
        dialect=HUAWEI_VRP,
    )

    assert additions == ["mode lacp-static"]
    assert [
        invocation.cli
        for operation in completed.operations
        for invocation in operation.invocations
    ] == ["interface Eth-Trunk 20", "mode lacp-static", "quit"]


def test_reviewer_delete_instruction_is_never_treated_as_missing_command() -> None:
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "配置堆叠端口",
                    "invocations": [
                        {"command_id": "stack-port", "cli": "stack-port 1"},
                    ],
                }
            ],
        }
    )

    completed, additions = complete_command_plan_from_review(
        plan,
        review={
            "required_changes": [
                "删除 'port link-type trunk' 和 'port trunk allow-pass vlan all' 命令，仅保留堆叠配置。"
            ]
        },
        evidence=[
            {
                "command_id": "trunk",
                "canonical_name": "port link-type trunk",
                "syntax": ["port link-type trunk"],
            },
            {
                "command_id": "allow-vlan",
                "canonical_name": "port trunk allow-pass vlan",
                "syntax": ["port trunk allow-pass vlan all"],
            },
        ],
        dialect=HUAWEI_VRP,
    )

    assert additions == []
    assert [
        invocation.cli
        for operation in completed.operations
        for invocation in operation.invocations
    ] == ["stack-port 1"]


def test_reviewer_unquoted_explicit_cli_is_recovered_only_from_matching_evidence() -> None:
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "配置堆叠端口",
                    "invocations": [
                        {"command_id": "stack-port", "cli": "stack-port 1"},
                    ],
                }
            ],
        }
    )

    completed, additions = complete_command_plan_from_review(
        plan,
        review={
            "required_changes": [
                "在完成端口配置后，进入堆叠视图并执行设置成员优先级的命令，"
                "例如：stack member 1 priority 160。"
            ]
        },
        evidence=[
            {
                "command_id": "stack-member",
                "canonical_name": "stack member",
                "syntax": [
                    "stack member",
                    "member-id",
                    "priority",
                    "priority-value",
                ],
            },
        ],
        dialect=HUAWEI_VRP,
    )

    assert additions == ["stack member 1 priority 160"]
    assert [
        invocation.cli
        for operation in completed.operations
        for invocation in operation.invocations
    ] == ["stack-port 1", "stack member 1 priority 160"]


def test_plain_cli_fallback_keeps_internal_view_exit_and_topology_port_spelling() -> None:
    plan = _plain_cli_draft_plan(
        """
        interface Eth-Trunk 1
        mode lacp-static
        quit
        interface GigabitEthernet0/0/1
        eth-trunk 1
        quit
        stp enable
        """,
        HUAWEI_VRP,
        topology_ports=["GE0/0/1"],
    )

    assert plan is not None
    assert [
        invocation.cli
        for operation in plan.operations
        for invocation in operation.invocations
    ] == [
        "interface Eth-Trunk 1",
        "mode lacp-static",
        "quit",
        "interface GE0/0/1",
        "eth-trunk 1",
        "quit",
        "stp enable",
    ]


def test_plain_cli_fallback_inserts_missing_exit_between_interface_contexts() -> None:
    plan = _plain_cli_draft_plan(
        """
        interface Eth-Trunk 10
        mode lacp-static
        interface GigabitEthernet0/0/1
        eth-trunk 10
        interface GigabitEthernet0/0/2
        eth-trunk 10
        """,
        HUAWEI_VRP,
        topology_ports=["GE0/0/1", "GE0/0/2"],
    )

    assert plan is not None
    assert [
        invocation.cli
        for operation in plan.operations
        for invocation in operation.invocations
    ] == [
        "interface Eth-Trunk 10",
        "mode lacp-static",
        "quit",
        "interface GE0/0/1",
        "eth-trunk 10",
        "quit",
        "interface GE0/0/2",
        "eth-trunk 10",
    ]


def test_plain_cli_fallback_binds_unique_manual_syntax_and_topology_port() -> None:
    plan = _plain_cli_draft_plan(
        """
        interface GE0/0/1
        eth-trunk 10
        quit
        interface Eth-Trunk 10
        mode lacp-static
        undo portswitch
        ip address 10.0.12.1 30
        """,
        HUAWEI_VRP,
        topology_ports=["GE0/0/1"],
    )
    assert plan is not None
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name }"],
        },
        {"command_id": "member", "canonical_name": "eth-trunk", "syntax": ["eth-trunk trunk-id"]},
        {"command_id": "mode", "canonical_name": "mode", "syntax": ["mode { lacp-static | manual }"]},
        {"command_id": "portswitch", "canonical_name": "portswitch", "syntax": ["undo portswitch"]},
        {"command_id": "address", "canonical_name": "ip address", "syntax": ["ip address ip-address mask"]},
    ]

    bound, unbound = _bind_plain_cli_draft_to_evidence(
        plan,
        evidence=evidence,
        topology_ports=["GE0/0/1"],
        dialect=HUAWEI_VRP,
    )

    assert not unbound
    invocations = [
        invocation
        for operation in bound.operations
        for invocation in operation.invocations
    ]
    assert [item.command_id for item in invocations] == [
        "interface",
        "member",
        "__control__",
        "interface",
        "mode",
        "portswitch",
        "address",
    ]
    assert invocations[0].target_port_ref == "topology:port:GE0/0/1"
    assert all(item.target_port_ref is None for item in invocations[1:])


def test_evidence_binding_prefers_matching_physical_port_view_for_duplicate_titles() -> None:
    ge_member = {
        "command_id": "ge-member",
        "canonical_name": "eth-trunk",
        "syntax": ["eth-trunk trunk-id"],
        "views": ["GE interface view"],
    }
    profile_member = {
        "command_id": "profile-member",
        "canonical_name": "eth-trunk",
        "syntax": ["eth-trunk universal-id"],
        "views": ["Load-balance profile view"],
    }

    resolved = _resolve_evidence_binding(
        "eth-trunk 1",
        [profile_member, ge_member],
        current_physical_port="GE0/0/1",
        prior_commands=["interface GE0/0/1"],
    )

    assert resolved is not None
    assert resolved["command_id"] == "ge-member"


def test_hybrid_search_prioritizes_exact_name_over_many_partial_names(session) -> None:  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="interface.html",
        stored_path="interface.html",
        source_sha256="2" * 64,
        file_format="html",
        brand="Huawei",
    )
    session.add(manual)
    session.flush()
    for index in range(40):
        document = KnowledgeDocument(
            manual_id=manual.id,
            source_path=f"partial-{index}.html",
            title=f"partial interface {index}",
            text_content="partial interface reference",
        )
        session.add(document)
        session.flush()
        session.add(
            Command(
                manual_id=manual.id,
                document_id=document.id,
                canonical_name=f"partial interface {index}",
                syntax_json='["partial interface"]',
                views_json="[]",
                parameters_json="[]",
                preconditions_json="[]",
                constraints_json="[]",
                examples_json="[]",
                evidence_json="{}",
            )
        )
    exact_document = KnowledgeDocument(
        manual_id=manual.id,
        source_path="interface.html",
        title="interface",
        text_content="interface reference",
    )
    session.add(exact_document)
    session.flush()
    session.add(
        Command(
            manual_id=manual.id,
            document_id=exact_document.id,
            canonical_name="interface",
            syntax_json='["interface { interface-name }"]',
            views_json="[]",
            parameters_json="[]",
            preconditions_json="[]",
            constraints_json="[]",
            examples_json="[]",
            evidence_json="{}",
        )
    )
    session.commit()

    hits = hybrid_command_search(
        session,
        query="interface",
        manual_id=manual.id,
        limit=1,
        use_semantic=False,
    )

    assert hits[0].command.canonical_name == "interface"
    assert "exact_name" in hits[0].sources


def test_hybrid_search_prioritizes_exact_syntax_when_title_has_a_view_suffix(session) -> None:  # type: ignore[no-untyped-def]
    manual = Manual(
        original_filename="address.html",
        stored_path="address.html",
        source_sha256="3" * 64,
        file_format="html",
        brand="Huawei",
    )
    session.add(manual)
    session.flush()
    for name, syntax in (
        ("ip address（接口视图）", ["ip address", "ip-address"]),
        ("ip address bootp-alloc", ["ip address bootp-alloc"]),
    ):
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
                parameters_json="[]",
                preconditions_json="[]",
                constraints_json="[]",
                examples_json="[]",
                evidence_json="{}",
            )
        )
    session.commit()

    hits = hybrid_command_search(
        session,
        query="ip address",
        manual_id=manual.id,
        limit=1,
        use_semantic=False,
    )

    assert hits[0].command.canonical_name == "ip address（接口视图）"
    assert "exact_syntax" in hits[0].sources


def test_evidence_syntax_rejects_a_different_enum_choice_on_a_same_prefix_page() -> None:
    """``stp mode mstp`` must not bind to the separate VBST page."""

    vbst = {
        "canonical_name": "stp mode vbst",
        "syntax": ["stp mode vbst"],
    }
    mstp = {
        "canonical_name": "stp mode",
        "syntax": ["stp mode", "{", "mstp", "|", "rstp", "stp", "}"],
    }

    assert _matches_evidence_syntax("stp mode mstp", vbst) is False
    assert _matches_evidence_syntax("stp mode mstp", mstp) is True


def test_evidence_syntax_rejects_undocumented_fixed_suffix() -> None:
    """Independent syntax rows must not turn a command title into a wildcard."""

    link_type = {
        "canonical_name": "port link-type",
        "syntax": [
            "port link-type access",
            "port link-type hybrid",
            "port link-type trunk",
        ],
    }

    assert _matches_evidence_syntax("port link-type trunk", link_type) is True
    assert _matches_evidence_syntax("port link-type stack", link_type) is False

    broad_port = {
        "canonical_name": "port",
        "syntax": ["port { interface-name | interface-type interface-number } { master | slave }"],
    }
    assert _matches_evidence_syntax("port GE0/0/1 master", broad_port) is True
    assert _matches_evidence_syntax("port link-type stack", broad_port) is False

    tokenized_numeric_port = {
        "canonical_name": "port（Portal interface view）",
        "syntax": ["port", "port-number", "[", "all", "]"],
    }
    assert _matches_evidence_syntax("port link-type stack", tokenized_numeric_port) is False


def test_evidence_syntax_requires_a_specific_title_after_a_shared_prefix() -> None:
    priority = {
        "canonical_name": "vrrp vrid priority",
        "syntax": ["vrrp vrid", "virtual-router-id", "priority", "priority-value"],
    }

    assert _matches_evidence_syntax("vrrp vrid 10 priority 120", priority) is True
    assert _matches_evidence_syntax("vrrp vrid 10 virtual-ip 192.168.10.1", priority) is False


def test_evidence_syntax_rejects_non_numeric_ospf_process_argument() -> None:
    ospf = {
        "canonical_name": "ospf",
        "syntax": ["ospf", "process-id", "[", "router-id", "route-id", "]"],
    }

    assert _matches_evidence_syntax("ospf 1", ospf) is True
    assert _matches_evidence_syntax("ospf ipv4", ospf) is False


def test_evidence_binding_prefers_title_keywords_across_parameter_slots() -> None:
    root = {
        "command_id": "vrrp-root",
        "canonical_name": "vrrp vrid",
        "syntax": ["vrrp vrid", "virtual-router-id", "[", "virtual-ip", "virtual-address", "]"],
    }
    priority = {
        "command_id": "vrrp-priority",
        "canonical_name": "vrrp vrid priority",
        "syntax": ["vrrp vrid", "virtual-router-id", "priority", "priority-value"],
    }

    resolved = _resolve_evidence_binding(
        "vrrp vrid 10 priority 120",
        [root, priority],
        current_physical_port=None,
        prior_commands=[],
    )

    assert resolved is not None
    assert resolved["command_id"] == "vrrp-priority"


def test_evidence_examples_reject_combined_mutually_exclusive_actions() -> None:
    stack_member = {
        "canonical_name": "stack member",
        "syntax": ["stack member", "member-id", "renumber", "new-member-id", "priority", "priority-value"],
        "examples": [
            "system-view\nstack\nstack member 1 renumber 2",
            "system-view\nstack\nstack member 1 priority 150",
        ],
    }

    assert _matches_evidence_syntax("stack member 1 priority 150", stack_member) is True
    assert _matches_evidence_syntax("stack member 1 renumber 1 priority 150", stack_member) is False


def test_evidence_binding_prefers_the_most_specific_canonical_prefix() -> None:
    generic = {
        "command_id": "stp-root",
        "canonical_name": "stp",
        "syntax": ["stp { enable | disable }"],
    }
    enable = {
        "command_id": "stp-enable",
        "canonical_name": "stp enable",
        "syntax": ["stp", "{", "enable", "|", "disable", "}"],
    }

    resolved = _resolve_evidence_binding(
        "stp enable",
        [generic, enable],
        current_physical_port=None,
        prior_commands=[],
    )

    assert resolved is not None
    assert resolved["command_id"] == "stp-enable"


def test_generic_compiler_moves_inline_read_only_cli_to_validation() -> None:
    evidence = [
        {
            "command_id": "stack",
            "canonical_name": "stack",
            "syntax": ["stack"],
        },
        {
            "command_id": "display-stack",
            "canonical_name": "display stack",
            "syntax": ["display stack"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "配置并检查",
                    "invocations": [
                        {"command_id": "stack", "cli": "stack"},
                        {"command_id": "display-stack", "cli": "display stack"},
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

    assert validation["status"] == "ready"
    assert commands == ["system-view", "stack", "return"]
    assert validation["validation_commands"] == ["display stack"]


def test_langgraph_llm_output_is_refined_before_explicit_retrieval_and_renderer() -> None:
    calls: list[str] = []

    def refiner(requirement: str, baseline: dict):  # type: ignore[no-untyped-def]
        calls.append("llm")
        assert requirement == "创建 VLAN 10"
        return {
            "intent": {**baseline, "retrieval_terms": ["vlan batch"]},
            "llm": {"status": "accepted"},
        }

    def retriever(intent: dict):  # type: ignore[no-untyped-def]
        calls.append("retrieve")
        assert intent["retrieval_terms"] == ["vlan batch"]
        return [{"canonical_name": "vlan batch"}]

    def renderer(intent: dict, evidence: list[dict]):  # type: ignore[no-untyped-def]
        calls.append("render")
        assert intent["vlan_ids"] == [10]
        assert evidence
        return ["system-view", "return"], {"status": "ready", "errors": []}

    state = build_planning_graph(
        intent_refiner=refiner,
        evidence_retriever=retriever,
        command_renderer=renderer,
    ).invoke(
        {
            "task_id": "task",
            "device_id": "switch",
            "requirement": "创建 VLAN 10",
            "intent": {"feature": "vlan_access", "vlan_ids": [10]},
        }
    )
    assert calls == ["llm", "retrieve", "render"]
    assert state["llm"]["status"] == "accepted"
    assert state["candidate_commands"] == ["system-view", "return"]


def test_langgraph_preserves_retrieval_audit_from_explicit_retriever() -> None:
    state = build_planning_graph(
        evidence_retriever=lambda _: {
            "evidence": [{"command_id": "evidence"}],
            "audit": {"status": "found", "rounds": [{"round": 1}]},
        },
        command_renderer=lambda _intent, _evidence: (
            ["system-view", "return"],
            {"status": "ready", "errors": []},
        ),
    ).invoke(
        {
            "task_id": "task",
            "device_id": "switch",
            "requirement": "创建 VLAN 10",
            "intent": {"feature": "vlan_access", "vlan_ids": [10]},
        }
    )

    assert state["retrieval_audit"]["status"] == "found"


def test_adaptive_thinking_only_enables_reasoning_nodes() -> None:
    assert should_enable_thinking("adaptive", "command_plan") is True
    assert should_enable_thinking("adaptive", "command_repair") is True
    assert should_enable_thinking("adaptive", "intent_refinement") is False
    assert should_enable_thinking("adaptive", "static_validation") is False
    assert should_enable_thinking("always", "static_validation") is True
    assert should_enable_thinking("off", "command_repair") is False
    assert thinking_extra_body(False) == {
        "chat_template_kwargs": {"enable_thinking": False},
        "thinking": {"type": "disabled"},
    }
    assert thinking_extra_body(True) == {
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking": {"type": "enabled"},
    }


def test_stream_request_waits_for_a_delayed_provider_chunk_without_an_agent_deadline() -> None:
    async def stalled_stream():  # type: ignore[no-untyped-def]
        await asyncio.sleep(1)
        yield object()

    async def exercise() -> None:
        iterator = stalled_stream().__aiter__()
        chunk = await _next_stream_chunk(
            iterator,
            formal_received=False,
            first_formal_deadline=0.0,
        )
        assert chunk is not None

    asyncio.run(exercise())


def test_llm_command_plan_compiles_only_evidence_bound_topology_port() -> None:
    evidence = [
        {"command_id": "vlan", "canonical_name": "vlan batch", "syntax": ["vlan batch { vlan-id }"]},
        {"command_id": "link", "canonical_name": "port link-type", "syntax": ["port link-type access"]},
        {
            "command_id": "pvid",
            "canonical_name": "port default vlan",
            "syntax": ["port default vlan vlan-id"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "建 VLAN",
                    "invocations": [{"command_id": "vlan", "arguments": {"vlan_ids": [10]}}],
                },
                {
                    "purpose": "配接入口",
                    "invocations": [
                        {
                            "command_id": "link",
                            "arguments": {"link_type": "access"},
                            "target_port_ref": "topology:port:GE0/0/1",
                        },
                        {
                            "command_id": "pvid",
                            "arguments": {"vlan_id": 10},
                            "target_port_ref": "topology:port:GE0/0/1",
                        },
                    ],
                },
            ],
        }
    )
    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "vlan_access", "vlan_ids": [10]},
        evidence=evidence,
        topology_ports=["GE0/0/1"],
    )
    assert validation["status"] == "ready"
    assert validation["source"] == "llm_command_plan_compiled"
    assert commands == [
        "system-view",
        "vlan batch 10",
        "interface GE0/0/1",
        "port link-type access",
        "port default vlan 10",
        "quit",
        "return",
    ]


def test_llm_command_plan_cannot_expand_to_a_port_outside_topology() -> None:
    evidence = [
        {"command_id": "vlan", "canonical_name": "vlan batch", "syntax": ["vlan batch { vlan-id }"]},
        {"command_id": "link", "canonical_name": "port link-type", "syntax": ["port link-type access"]},
        {
            "command_id": "pvid",
            "canonical_name": "port default vlan",
            "syntax": ["port default vlan vlan-id"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "建 VLAN",
                    "invocations": [{"command_id": "vlan", "arguments": {"vlan_ids": [10]}}],
                },
                {
                    "purpose": "配口",
                    "invocations": [
                        {
                            "command_id": "link",
                            "arguments": {"link_type": "access"},
                            "target_port_ref": "topology:port:GE0/0/2",
                        },
                        {
                            "command_id": "pvid",
                            "arguments": {"vlan_id": 10},
                            "target_port_ref": "topology:port:GE0/0/2",
                        },
                    ],
                },
            ],
        }
    )
    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "vlan_access", "vlan_ids": [10]},
        evidence=evidence,
        topology_ports=["GE0/0/1"],
    )
    assert commands == []
    assert validation["status"] == "blocked"
    assert "拓扑外端口" in validation["errors"][0]


def test_llm_command_plan_blocks_unclassified_intent_without_throwing() -> None:
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [{"purpose": "任意", "invocations": [{"command_id": "x", "arguments": {}}]}],
        }
    )
    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "unclassified", "vlan_ids": []},
        evidence=[],
        topology_ports=["GE0/0/1"],
    )
    assert commands == []
    assert validation["status"] == "blocked"


def test_generic_evidence_bound_plan_compiles_ospf_without_a_vlan_plugin() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name | interface-type interface-number }"],
        },
        {
            "command_id": "portswitch",
            "canonical_name": "portswitch",
            "syntax": ["portswitch", "undo portswitch"],
        },
        {
            "command_id": "address",
            "canonical_name": "ip address（接口视图）",
            "syntax": ["ip address", "ip-address", "{", "mask", "|", "mask-length", "}"],
        },
        {
            "command_id": "ospf",
            "canonical_name": "ospf",
            "syntax": ["ospf", "process-id", "[", "router-id", "route-id", "]"],
        },
        {
            "command_id": "area",
            "canonical_name": "area",
            "syntax": ["area { area-id | area-idipv4 }"],
        },
        {
            "command_id": "network",
            "canonical_name": "network（OSPF区域视图）",
            "syntax": ["network", "address", "wildcard-mask"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "将 PC1 所在端口转为三层口并配置地址",
                    "invocations": [
                        {
                            "command_id": "interface",
                            "target_port_ref": "topology:port:GE0/0/1",
                            "cli": "interface GE0/0/1",
                        },
                        {"command_id": "portswitch", "cli": "undo portswitch"},
                        {"command_id": "address", "cli": "ip address 10.10.1.1 255.255.255.0"},
                        {"command_id": "__control__", "cli": "quit"},
                    ],
                },
                {
                    "purpose": "启动 OSPF Area 0 并发布直连网段",
                    "invocations": [
                        {"command_id": "ospf", "cli": "ospf 1 router-id 1.1.1.1"},
                        {"command_id": "area", "cli": "area 0"},
                        {"command_id": "network", "cli": "network 10.10.1.0 0.0.0.255"},
                    ],
                },
            ],
            "validation_commands": ["display ospf peer brief", "display ospf routing"],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={
            "feature": "l3_ospf_ipv4",
            "vlan_ids": [],
            "required_configuration_facts": [
                {"kind": "interface_address", "port": "GE0/0/1", "address": "10.10.1.1", "prefix": "24"}
            ],
        },
        evidence=evidence,
        topology_ports=["GE0/0/1", "GE0/0/2"],
        device_scope={"protected_ports": ["GE0/0/2"]},
    )

    assert validation["status"] == "ready"
    assert validation["source"] == "generic_evidence_bound_compiler"
    assert validation["validation_commands"] == ["display ospf peer brief", "display ospf routing"]
    assert commands == [
        "system-view",
        "interface GE0/0/1",
        "undo portswitch",
        "ip address 10.10.1.1 255.255.255.0",
        "quit",
        "ospf 1 router-id 1.1.1.1",
        "area 0",
        "network 10.10.1.0 0.0.0.255",
        "return",
    ]
    assert not any("vlan" in command.casefold() for command in commands)


def test_generic_huawei_composite_plan_rejects_split_trunk_vlan_membership() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name }"],
        },
        {
            "command_id": "link-type",
            "canonical_name": "port link-type",
            "syntax": ["port link-type { access | trunk }"],
        },
        {
            "command_id": "allow-pass",
            "canonical_name": "port trunk allow-pass vlan",
            "syntax": ["port trunk allow-pass vlan { vlan-id }"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "交换机互联链路",
                    "invocations": [
                        {
                            "command_id": "interface",
                            "target_port_ref": "topology:port:GE0/0/1",
                            "cli": "interface GE0/0/1",
                        },
                        {"command_id": "link-type", "cli": "port link-type trunk"},
                        {"command_id": "allow-pass", "cli": "port trunk allow-pass vlan 10"},
                    ],
                }
            ],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={
            "feature": "multi_vlan_intervlan",
            "renderer_mode": "generic_evidence_bound",
            "vlan_ids": [10, 20],
        },
        evidence=evidence,
        topology_ports=["GE0/0/1"],
        device_scope={
            "vlan_l2_roles": {
                "vlan_ids": [10, 20],
                "access_ports": [],
                "trunk_ports": ["GE0/0/1"],
            }
        },
    )

    assert commands == []
    assert validation["status"] == "blocked"
    assert "放行全部 VLAN" in validation["errors"][0]


def test_huawei_physical_address_requires_evidence_bound_l3_conversion() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name }"],
        },
        {
            "command_id": "address",
            "canonical_name": "ip address（接口视图）",
            "syntax": ["ip address", "ip-address"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "为物理端口配置地址",
                    "invocations": [
                        {
                            "command_id": "interface",
                            "target_port_ref": "topology:port:GE0/0/1",
                            "cli": "interface GE0/0/1",
                        },
                        {"command_id": "address", "cli": "ip address 10.10.1.1 255.255.255.0"},
                    ],
                }
            ],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={
            "feature": "generic",
            "required_configuration_facts": [
                {"kind": "interface_address", "port": "GE0/0/1", "address": "10.10.1.1", "prefix": "24"}
            ],
        },
        evidence=evidence,
        topology_ports=["GE0/0/1"],
    )

    assert commands == []
    assert validation["status"] == "blocked"
    assert any("undo portswitch" in error for error in validation["errors"])


def test_huawei_address_command_requires_an_interface_view() -> None:
    evidence = [
        {
            "command_id": "address",
            "canonical_name": "ip address（接口视图）",
            "syntax": ["ip address", "ip-address", "mask"],
            "views": ["接口视图"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "错误地在系统视图配置地址",
                    "invocations": [
                        {"command_id": "address", "cli": "ip address 10.10.1.1 255.255.255.0"},
                    ],
                }
            ],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={
            "feature": "generic",
            "required_configuration_facts": [
                {"kind": "interface_address", "port": "GE0/0/1", "address": "10.10.1.1", "prefix": "24"}
            ],
        },
        evidence=evidence,
        topology_ports=["GE0/0/1"],
    )

    assert commands == []
    assert validation["status"] == "blocked"
    assert any("缺少接口视图上下文" in error for error in validation["errors"])


def test_huawei_eth_trunk_address_inserts_evidence_bound_l3_conversion() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name }"],
        },
        {
            "command_id": "portswitch",
            "canonical_name": "portswitch",
            "syntax": ["portswitch", "undo portswitch"],
        },
        {
            "command_id": "address",
            "canonical_name": "ip address（接口视图）",
            "syntax": ["ip address", "ip-address"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "配置三层聚合口地址",
                    "invocations": [
                        {"command_id": "interface", "cli": "interface Eth-Trunk 10"},
                        {"command_id": "address", "cli": "ip address 10.0.12.1 255.255.255.252"},
                    ],
                }
            ],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "generic"},
        evidence=evidence,
        topology_ports=["GE0/0/1", "GE0/0/2"],
    )

    assert validation["status"] == "ready"
    assert commands == [
        "system-view",
        "interface Eth-Trunk 10",
        "undo portswitch",
        "ip address 10.0.12.1 255.255.255.252",
        "return",
    ]
    assert validation["automatic_prerequisites"] == [
        "在 virtual:eth-trunk 10 的地址配置前补齐 undo portswitch"
    ]


def test_generic_compiler_rebinds_unambiguous_manual_evidence_and_preserves_huawei_port_spelling() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name }"],
        },
        {
            "command_id": "portswitch",
            "canonical_name": "portswitch",
            "syntax": ["portswitch", "undo portswitch"],
        },
        {
            "command_id": "address",
            "canonical_name": "ip address（接口视图）",
            "syntax": ["ip address", "ip-address"],
            "views": ["GE接口视图"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "配置三层互联地址",
                    "invocations": [
                        {
                            "command_id": "address",
                            "target_port_ref": "topology:port:GE0/0/1",
                            "cli": "interface GigabitEthernet0/0/1",
                        },
                        {"command_id": "address", "cli": "undo portswitch"},
                        {"command_id": "address", "cli": "ip address 10.10.1.1 255.255.255.0"},
                    ],
                }
            ],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={
            "feature": "generic",
            "required_configuration_facts": [
                {"kind": "interface_address", "port": "GE0/0/1", "address": "10.10.1.1", "prefix": "24"}
            ],
        },
        evidence=evidence,
        topology_ports=["GE0/0/1"],
    )

    assert validation["status"] == "ready"
    assert commands == [
        "system-view",
        "interface GE0/0/1",
        "undo portswitch",
        "ip address 10.10.1.1 255.255.255.0",
        "return",
    ]
    assert [item["resolved_command_id"] for item in validation["resolved_evidence_bindings"]] == [
        "interface",
        "portswitch",
    ]


def test_generic_plan_allows_child_command_to_repeat_current_port_reference() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name }"],
        },
        {
            "command_id": "portswitch",
            "canonical_name": "portswitch",
            "syntax": ["portswitch", "undo portswitch"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "切换已绘制物理口为三层口",
                    "invocations": [
                        {
                            "command_id": "interface",
                            "target_port_ref": "topology:port:GE0/0/1",
                            "cli": "interface GE0/0/1",
                        },
                        {
                            "command_id": "portswitch",
                            "target_port_ref": "topology:port:GE0/0/1",
                            "cli": "undo portswitch",
                        },
                    ],
                }
            ],
            "validation_commands": ["display interface GE0/0/1"],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "l3_interface", "renderer_mode": "generic_evidence_bound"},
        evidence=evidence,
        topology_ports=["GE0/0/1"],
    )

    assert validation["status"] == "ready"
    assert commands == ["system-view", "interface GE0/0/1", "undo portswitch", "return"]


def test_generic_compiler_recovers_mislabeled_business_commands_and_retrieval_followup() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name }"],
        },
        {
            "command_id": "lacp-mode",
            "canonical_name": "mode（Eth-Trunk接口视图）",
            "syntax": ["mode { lacp-static | manual }"],
            "views": ["Eth-Trunk接口视图"],
            "preconditions": ["静态 LACP 模式使用 mode lacp-static 配置。"],
        },
        {
            "command_id": "member",
            "canonical_name": "eth-trunk",
            "syntax": ["eth-trunk trunk-id"],
            "views": ["GE接口视图"],
        },
        {
            "command_id": "stp-mode-system",
            "canonical_name": "stp mode",
            "syntax": ["stp mode { mstp | rstp | stp }"],
            "views": ["系统视图"],
        },
        {
            "command_id": "stp-mode-process",
            "canonical_name": "stp mode（MSTP进程视图）",
            "syntax": ["stp mode { mstp | rstp | stp }"],
            "views": ["MSTP进程视图"],
        },
        {
            "command_id": "stp-enable",
            "canonical_name": "stp enable",
            "syntax": ["stp { enable | disable }"],
            "views": ["系统视图"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "创建并配置聚合口",
                    "invocations": [
                        {"command_id": "interface", "cli": "interface Eth-Trunk1"},
                        # A weak model labeled both ordinary CLI lines as controls.
                        {"command_id": "__control__", "cli": "lacp mode lacp-static"},
                        {"command_id": "__control__", "cli": "quit"},
                        {
                            "command_id": "interface",
                            "target_port_ref": "topology:port:GE0/0/1",
                            "cli": "interface GE0/0/1",
                        },
                        {"command_id": "__control__", "cli": "eth-trunk 1"},
                        {"command_id": "__control__", "cli": "quit"},
                        {"command_id": "__unverified_draft__", "cli": "stp mode mstp"},
                    ],
                }
            ],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "generic", "retrieval_followup_terms": ["stp enable"]},
        evidence=evidence,
        topology_ports=["GE0/0/1"],
    )

    assert validation["status"] == "ready"
    assert commands == [
        "system-view",
        "interface Eth-Trunk 1",
        "mode lacp-static",
        "quit",
        "interface GE0/0/1",
        "eth-trunk 1",
        "quit",
        "stp mode mstp",
        "stp enable",
        "return",
    ]
    assert {item["resolved_command_id"] for item in validation["resolved_evidence_bindings"]} >= {
        "lacp-mode",
        "member",
        "stp-mode-system",
        "stp-enable",
    }


def test_retrieval_followup_does_not_expand_a_bare_manual_root_to_an_unrelated_cli() -> None:
    """A search phrase must not become a command without a full syntax page."""

    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "启用生成树",
                    "invocations": [{"command_id": "stp-enable", "cli": "stp enable"}],
                }
            ],
        }
    )
    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "generic", "retrieval_followup_terms": ["stack priority"]},
        evidence=[
            {"command_id": "stp-enable", "canonical_name": "stp enable", "syntax": ["stp enable"]},
            {"command_id": "stack", "canonical_name": "stack", "syntax": ["stack"]},
        ],
        topology_ports=[],
    )

    assert validation["status"] == "ready"
    assert commands == ["system-view", "stp enable", "return"]


def test_retrieval_followup_does_not_inject_a_parameterised_command_title() -> None:
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "启用生成树",
                    "invocations": [{"command_id": "stp-enable", "cli": "stp enable"}],
                }
            ],
        }
    )
    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "generic", "retrieval_followup_terms": ["ip address"]},
        evidence=[
            {"command_id": "stp-enable", "canonical_name": "stp enable", "syntax": ["stp enable"]},
            {
                "command_id": "ip-address",
                "canonical_name": "ip address",
                "syntax": ["ip address", "ip-address", "mask"],
                "views": ["VLANIF interface view"],
            },
        ],
        topology_ports=[],
    )

    assert validation["status"] == "ready"
    assert commands == ["system-view", "stp enable", "return"]


def test_retrieval_followup_never_injects_bare_parameterised_command_roots() -> None:
    """Search terms must not become incomplete address/VLAN/stack CLI lines."""

    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "启用生成树",
                    "invocations": [{"command_id": "stp-enable", "cli": "stp enable"}],
                }
            ],
        }
    )
    commands, validation = compile_command_plan(
        plan,
        intent={
            "feature": "generic",
            "retrieval_followup_terms": ["ip address", "vlan batch", "stack-port"],
        },
        evidence=[
            {"command_id": "stp-enable", "canonical_name": "stp enable", "syntax": ["stp enable"]},
            {
                "command_id": "ip-address",
                "canonical_name": "ip address",
                "syntax": ["ip address", "ip-address", "{", "mask", "|", "mask-length", "}"],
                "views": ["接口视图"],
            },
            {
                "command_id": "vlan-batch",
                "canonical_name": "vlan batch",
                "syntax": ["vlan batch { vlan-id }"],
                "views": ["系统视图"],
            },
            {
                "command_id": "stack-port",
                "canonical_name": "stack-port",
                "syntax": ["stack-port", "portnum", "undo stack-port", "[", "]"],
                "views": ["10GE接口视图"],
            },
        ],
        topology_ports=[],
    )

    assert validation["status"] == "ready"
    assert commands == ["system-view", "stp enable", "return"]


def test_tokenized_chm_syntax_keeps_a_hyphenated_command_root() -> None:
    """CHM token tables may split ``stack-port portnum`` into separate cells."""

    evidence = {
        "command_id": "stack-port",
        "canonical_name": "stack-port",
        "syntax": ["stack-port", "portnum", "undo stack-port", "[", "]"],
    }

    assert _matches_evidence_syntax("stack-port 1", evidence) is True
    assert _matches_evidence_syntax(
        "stack priority", {"canonical_name": "stack", "syntax": ["stack"]}
    ) is False


def test_explicit_port_assignment_fallback_rebuilds_only_manual_backed_port_actions() -> None:
    evidence = [
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface { interface-name | interface-type interface-number }"],
        },
        {
            "command_id": "stack-port",
            "canonical_name": "stack-port",
            "syntax": ["stack-port", "portnum", "undo stack-port", "[", "]"],
            "views": ["10GE interface view"],
        },
        {
            "command_id": "stack-member",
            "canonical_name": "stack member",
            "syntax": ["stack member", "member-id", "priority", "priority-value"],
        },
    ]
    weak_plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "错误的逻辑口形式",
                    "invocations": [
                        {"command_id": "interface", "cli": "interface stack-port 1/1"},
                        {
                            "command_id": "__unverified_draft__",
                            "cli": "port member-group interface 10GE1/0/3",
                        },
                        {"command_id": "__control__", "cli": "quit"},
                        {"command_id": "stack-port", "cli": "stack-port 1"},
                        {"command_id": "stack-member", "cli": "stack member 1 priority 160"},
                    ],
                }
            ],
        }
    )
    intent = {
        "renderer_mode": "generic_evidence_bound",
        "requirement": "10GE1/0/3 加入 Stack-Port 1，并将成员 1 的堆叠优先级设为 160。",
        "required_port_command_facts": [
            {
                "kind": "port_command_assignment",
                "port": "10GE1/0/3",
                "command_hint": "Stack-Port",
                "argument": "1",
            }
        ],
    }

    fallback = build_explicit_port_assignment_fallback_plan(
        weak_plan,
        intent=intent,
        evidence=evidence,
        topology_ports=["10GE1/0/3"],
    )

    assert fallback is not None
    commands, validation = compile_command_plan(
        fallback,
        intent=intent,
        evidence=evidence,
        topology_ports=["10GE1/0/3"],
    )
    assert validation["status"] == "ready"
    assert commands == [
        "system-view",
        "interface 10GE1/0/3",
        "stack-port 1",
        "quit",
        "stack member 1 priority 160",
        "return",
    ]


def test_generic_evidence_plan_rejects_an_unmapped_physical_interface() -> None:
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "越权端口",
                    "invocations": [
                        {
                            "command_id": "interface",
                            "target_port_ref": "topology:port:GE0/0/2",
                            "cli": "interface GE0/0/2",
                        }
                    ],
                }
            ],
        }
    )
    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "l3_ospf_ipv4", "vlan_ids": []},
        evidence=[
            {
                "command_id": "interface",
                "canonical_name": "interface",
                "syntax": ["interface { interface-name }"],
            }
        ],
        topology_ports=["GE0/0/1"],
    )
    assert commands == []
    assert validation["status"] == "blocked"
    assert "拓扑外物理端口" in validation["errors"][0]


def test_non_huawei_vlan_request_uses_evidence_bound_cisco_cli_not_huawei_plugin() -> None:
    evidence = [
        {"command_id": "vlan", "canonical_name": "vlan", "syntax": ["vlan vlan-id"]},
        {
            "command_id": "interface",
            "canonical_name": "interface",
            "syntax": ["interface interface-id"],
        },
        {
            "command_id": "switchport",
            "canonical_name": "switchport access vlan",
            "syntax": ["switchport access vlan vlan-id"],
        },
    ]
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {"purpose": "创建 VLAN", "invocations": [{"command_id": "vlan", "cli": "vlan 10"}]},
                {
                    "purpose": "接入口加入 VLAN",
                    "invocations": [
                        {
                            "command_id": "interface",
                            "target_port_ref": "topology:port:GigabitEthernet0/1",
                            "cli": "interface Gi0/1",
                        },
                        {"command_id": "switchport", "cli": "switchport access vlan 10"},
                        {"command_id": "__control__", "cli": "exit"},
                    ],
                },
            ],
            "validation_commands": ["show vlan brief"],
        }
    )

    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "vlan_access", "vlan_ids": [10], "renderer_mode": "generic_evidence_bound"},
        evidence=evidence,
        topology_ports=["GigabitEthernet0/1"],
        dialect=CISCO_IOS,
    )

    assert validation["status"] == "ready"
    assert validation["source"] == "generic_evidence_bound_compiler"
    assert commands == [
        "configure terminal",
        "vlan 10",
        "interface Gi0/1",
        "switchport access vlan 10",
        "exit",
        "end",
    ]
    assert "system-view" not in commands
    assert "vlan batch 10" not in commands


def test_unknown_brand_does_not_inject_a_vendor_configuration_wrapper() -> None:
    plan = LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": [
                {
                    "purpose": "启用手册功能",
                    "invocations": [{"command_id": "feature", "cli": "enable rare-feature"}],
                }
            ],
            "validation_commands": ["show rare-feature"],
        }
    )
    commands, validation = compile_command_plan(
        plan,
        intent={"feature": "rare_feature", "renderer_mode": "generic_evidence_bound"},
        evidence=[
            {"command_id": "feature", "canonical_name": "enable", "syntax": ["enable rare-feature"]}
        ],
        topology_ports=[],
        dialect=GENERIC_MANUAL,
    )

    assert validation["status"] == "ready"
    assert commands == ["enable rare-feature"]
    assert resolve_cli_dialect("auto", "Huawei").key == HUAWEI_VRP.key
    assert resolve_cli_dialect("auto", "任意新品牌").key == GENERIC_MANUAL.key


def test_langgraph_reviewer_is_advisory_and_keeps_compiled_commands() -> None:
    def renderer(intent: dict, evidence: list[dict]):  # type: ignore[no-untyped-def]
        return ["system-view", "return"], {"status": "ready", "errors": []}

    def reviewer(state: dict):  # type: ignore[no-untyped-def]
        assert state["candidate_commands"] == ["system-view", "return"]
        return {
            "review": {"verdict": "reject", "issues": ["发现明确的命令/意图不一致。"]},
            "llm": {"status": "accepted", "node": "command_review"},
        }

    state = build_planning_graph(
        evidence_retriever=lambda _: [{"command_id": "evidence"}],
        command_renderer=renderer,
        command_reviewer=reviewer,
    ).invoke(
        {
            "task_id": "task",
            "device_id": "switch",
            "requirement": "创建 VLAN 10",
            "intent": {"feature": "vlan_access", "vlan_ids": [10]},
        }
    )
    assert state["candidate_commands"] == ["system-view", "return"]
    assert state["validation_errors"] == []
    assert state["command_review"]["review"]["verdict"] == "reject"
