from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

IntentRefiner = Callable[[str, dict[str, Any]], dict[str, Any]]
EvidenceRetriever = Callable[[dict[str, Any]], list[dict[str, Any]] | dict[str, Any]]
CommandPlanner = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]
CommandReviewer = Callable[[dict[str, Any]], dict[str, Any]]
CommandRenderer = Callable[
    [dict[str, Any], list[dict[str, Any]], dict[str, Any] | None], tuple[list[str], dict[str, Any]]
]


class PlanningState(TypedDict, total=False):
    task_id: str
    device_id: str
    requirement: str
    intent: dict[str, Any]
    llm: dict[str, Any]
    evidence: list[dict[str, Any]]
    retrieval_audit: dict[str, Any]
    command_plan: dict[str, Any]
    command_plan_llm: dict[str, Any]
    command_review: dict[str, Any]
    candidate_commands: list[str]
    validation: dict[str, Any]
    validation_errors: list[str]
    messages: Annotated[list, add_messages]
    next_action: Literal[
        "llm_refine", "retrieve", "command_plan", "generate", "validate", "command_review", "review", "end"
    ]


def _validate_input(state: PlanningState) -> PlanningState:
    if not state.get("task_id") or not state.get("device_id"):
        return {"validation_errors": ["缺少任务或设备标识。"], "next_action": "end"}
    if state.get("validation_errors"):
        return {"next_action": "end"}
    return {"next_action": "llm_refine"}


def _route(state: PlanningState) -> str:
    return state.get("next_action", "end")


def build_planning_graph(
    *,
    intent_refiner: IntentRefiner | None = None,
    evidence_retriever: EvidenceRetriever | None = None,
    command_planner: CommandPlanner | None = None,
    command_reviewer: CommandReviewer | None = None,
    command_renderer: CommandRenderer | None = None,
):
    """Build the auditable LangGraph workflow without native tool calling.

    The LLM-facing node can only update a validated intent.  Retrieval and
    rendering are explicit graph nodes invoked by the application, not model
    tool calls; their results are written back into state for later review.
    """

    def refine_with_llm(state: PlanningState) -> PlanningState:
        if not intent_refiner:
            return {"llm": {"status": "disabled"}, "next_action": "retrieve"}
        outcome = intent_refiner(str(state.get("requirement", "")), dict(state.get("intent", {})))
        return {
            "intent": dict(outcome.get("intent", state.get("intent", {}))),
            "llm": dict(outcome.get("llm", {})),
            "next_action": "retrieve",
        }

    def retrieve_evidence(state: PlanningState) -> PlanningState:
        """Explicit retrieval tool node; the model cannot invoke it directly."""

        outcome = (
            evidence_retriever(dict(state.get("intent", {})))
            if evidence_retriever
            else state.get("evidence", [])
        )
        if isinstance(outcome, dict):
            evidence = list(outcome.get("evidence", []))
            retrieval_audit = dict(outcome.get("audit", {}))
        else:
            evidence = outcome
            retrieval_audit = {}
        if not evidence:
            # A handbook can be incomplete or incorrectly segmented.  Do not
            # end with an empty command panel: the following LLM node is asked
            # for an explicitly labelled, editable best-effort CLI draft.  It
            # remains non-executable until the operator reviews it.
            retrieval_audit = {
                **retrieval_audit,
                "warning": "检索未返回直接手册证据；将生成未验证的人工审阅草案。",
            }
        updated_intent = dict(state.get("intent", {}))
        followup_terms = retrieval_audit.get("followup_terms", [])
        if isinstance(followup_terms, list):
            # Pass precise ReAct follow-ups to the planner without treating
            # model text as a tool call. The compiler only uses terms that
            # are independently proven to be complete handbook commands.
            updated_intent["retrieval_followup_terms"] = [
                str(item).strip() for item in followup_terms if str(item).strip()
            ]
        return {
            "intent": updated_intent,
            "evidence": evidence,
            "retrieval_audit": retrieval_audit,
            "next_action": "command_plan" if command_planner else "generate",
        }

    def plan_commands_with_llm(state: PlanningState) -> PlanningState:
        """Explicit LLM planning node; it emits data, never a tool invocation."""

        if not command_planner:
            return {"next_action": "generate"}
        outcome = command_planner(dict(state.get("intent", {})), list(state.get("evidence", [])))
        result: PlanningState = {
            "command_plan": dict(outcome.get("command_plan") or {}),
            "command_plan_llm": dict(outcome.get("llm") or {}),
            "next_action": "generate",
        }
        # The application may complete the compact retrieval packet by doing a
        # local syntax lookup after the model has named concrete CLI.  This is
        # still an explicit application node result, not a model tool call.
        recovered_evidence = outcome.get("evidence")
        if isinstance(recovered_evidence, list):
            result["evidence"] = recovered_evidence
        return result

    def generate_commands(state: PlanningState) -> PlanningState:
        if not command_renderer:
            commands = state.get("candidate_commands", [])
            validation = {"status": "ready" if commands else "blocked", "errors": []}
        else:
            intent = dict(state.get("intent", {}))
            evidence = list(state.get("evidence", []))
            if len(inspect.signature(command_renderer).parameters) >= 3:
                commands, validation = command_renderer(
                    intent, evidence, dict(state.get("command_plan") or {}) or None
                )
            else:  # Backward-compatible adapter for existing renderer tests/plugins.
                commands, validation = command_renderer(intent, evidence)  # type: ignore[call-arg]
        if validation.get("errors") or not commands:
            return {
                "candidate_commands": commands,
                "validation": validation,
                "validation_errors": list(validation.get("errors", [])) or ["没有证据约束的候选命令。"],
                "next_action": "end",
            }
        return {
            "candidate_commands": commands,
            "validation": validation,
            "validation_errors": [],
            "next_action": "validate",
        }

    def validate_commands(state: PlanningState) -> PlanningState:
        if state.get("validation_errors"):
            return {"next_action": "end"}
        return {"next_action": "command_review" if command_reviewer else "review"}

    def review_commands_with_llm(state: PlanningState) -> PlanningState:
        if not command_reviewer:
            return {"next_action": "review"}
        outcome = command_reviewer(dict(state))
        review = dict(outcome.get("review") or {})
        llm = dict(outcome.get("llm") or {})
        # This node is advisory.  The operator, not an LLM verdict, decides
        # whether the displayed per-device command set is approved for send.
        return {"command_review": {"llm": llm, "review": review}, "next_action": "review"}

    def prepare_human_review(_state: PlanningState) -> PlanningState:
        # Approval is persisted in DevicePlan, independently of this in-memory
        # run. Restarting FastAPI cannot skip the user review gate.
        return {"next_action": "review"}

    graph = StateGraph(PlanningState)
    graph.add_node("validate_input", _validate_input)
    graph.add_node("refine_with_llm", refine_with_llm)
    graph.add_node("retrieve_evidence", retrieve_evidence)
    graph.add_node("plan_commands_with_llm", plan_commands_with_llm)
    graph.add_node("generate_commands", generate_commands)
    graph.add_node("validate_commands", validate_commands)
    graph.add_node("review_commands_with_llm", review_commands_with_llm)
    graph.add_node("prepare_human_review", prepare_human_review)
    graph.add_edge(START, "validate_input")
    graph.add_conditional_edges(
        "validate_input",
        _route,
        {"llm_refine": "refine_with_llm", "end": END},
    )
    graph.add_conditional_edges(
        "refine_with_llm",
        _route,
        {"retrieve": "retrieve_evidence", "end": END},
    )
    graph.add_conditional_edges(
        "retrieve_evidence",
        _route,
        {"command_plan": "plan_commands_with_llm", "generate": "generate_commands", "end": END},
    )
    graph.add_conditional_edges(
        "plan_commands_with_llm",
        _route,
        {"generate": "generate_commands", "end": END},
    )
    graph.add_conditional_edges(
        "generate_commands",
        _route,
        {"validate": "validate_commands", "end": END},
    )
    graph.add_conditional_edges(
        "validate_commands",
        _route,
        {"command_review": "review_commands_with_llm", "review": "prepare_human_review", "end": END},
    )
    graph.add_conditional_edges(
        "review_commands_with_llm",
        _route,
        {"review": "prepare_human_review", "end": END},
    )
    graph.add_edge("prepare_human_review", END)
    return graph.compile()
