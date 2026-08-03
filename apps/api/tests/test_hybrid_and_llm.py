from __future__ import annotations

import json

from app.agents.graph import build_planning_graph
from app.llm.client import should_enable_thinking, thinking_extra_body
from app.models import Command, KnowledgeDocument, Manual
from app.planning.llm_command_plan import compile_command_plan
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


def test_adaptive_thinking_only_enables_reasoning_nodes() -> None:
    assert should_enable_thinking("adaptive", "command_plan") is True
    assert should_enable_thinking("adaptive", "static_validation") is False
    assert should_enable_thinking("always", "static_validation") is True
    assert should_enable_thinking("off", "command_plan") is False
    assert thinking_extra_body(False) == {"chat_template_kwargs": {"enable_thinking": False}}
    assert thinking_extra_body(True) == {
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking": {"type": "enabled"},
    }


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
            "operations": [
                {"purpose": "任意", "invocations": [{"command_id": "x", "arguments": {}}]}
            ],
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


def test_langgraph_reviewer_can_block_compiled_commands_without_rewriting_them() -> None:
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
    assert state["validation_errors"] == ["发现明确的命令/意图不一致。"]
