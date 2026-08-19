"""Constrained, capability-neutral LLM intent refinement for the planning graph."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Event
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import parse_json_response, request_text_result, should_enable_thinking
from app.planning.runtime import PlanningCancelled
from app.schemas import LlmIntentRefinement
from app.services.settings import get_provider_secret, read_provider_settings

# This is an output-token budget, not a wall-clock timeout. Intent refinement
# only returns a compact JSON object, so a bounded reasoning trace prevents an
# overloaded provider from spending an unbounded response on this first stage.
INTENT_REFINEMENT_MAX_TOKENS = 4_096


def _run_async(coroutine):  # type: ignore[no-untyped-def]
    """Run the fixed async OpenAI-compatible client from synchronous graph nodes."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    # FastAPI executes the current planning endpoint in a worker thread, but a
    # small thread bridge keeps this helper correct when called by async tests.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _prompt(requirement: str, baseline: dict[str, Any]) -> list[dict[str, str]]:
    # Intent refinement needs topology-derived facts and a template's approach,
    # not its historic device CLI.  Passing an entire template snapshot here
    # wastes a small local model's context window and increases the chance it
    # copies stale ports or addresses.  The command planner later receives its
    # own compact, current-device template reference.
    template = dict(baseline.get("template_reference") or {})
    compact_template = (
        {
            "title": str(template.get("title") or "")[:200],
            "description": str(template.get("description") or "")[:600],
            "reference_requirement": str(template.get("reference_requirement") or "")[:1_000],
            "reference_planning_idea": str(template.get("reference_planning_idea") or "")[:1_400],
        }
        if template
        else None
    )
    prompt_baseline = {
        "feature": baseline.get("feature"),
        "topology_capabilities": baseline.get("topology_capabilities", []),
        "vlan_ids": baseline.get("vlan_ids", []),
        "pc_vlan_map": baseline.get("pc_vlan_map", {}),
        "l3_core_node_id": baseline.get("l3_core_node_id"),
        "vlan_gateways": baseline.get("vlan_gateways", {}),
        "required_configuration_facts": baseline.get("required_configuration_facts", []),
        "existing_configuration_facts": baseline.get("existing_configuration_facts", []),
        "planning_warnings": baseline.get("planning_warnings", []),
        "template_reference": compact_template,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是工业交换机配置规划节点。"
                "输出一个 JSON 对象；planning_idea 是给用户审阅和修改的中文配置思路，不是最终 CLI。"
                "JSON Schema: "
                '{"action":"refine_intent","feature":"能力标签，如 l3_ospf_ipv4",'
                '"capabilities":["全部需要规划的能力标签"],'
                '"vlan_ids":[1..4094],"retrieval_terms":["最多10个手册检索词"],'
                '"planning_steps":["实施步骤"],"planning_idea":"完整可编辑思路",'
                '"requirement_gaps":["需求中缺少但建议补充的事项"],"reason_summary":"简短总结"}。'
                "feature 只是能力标签，不是命令白名单；可使用 generic、l3_ospf_ipv4、static_routing、"
                "link_aggregation、stp、acl 等小写下划线标签。"
                "capabilities 必须列出用户要求的全部能力。组合需求不能只保留其中一项；例如 VLAN 加 OSPF"
                "可列 vlan_access、vlan_trunk、vlanif_gateway、l3_ospf_ipv4。"
                "请先给出你自己的实现方案，再列出需求中可能缺少的参数、验收条件或风险。"
                "不要因为应用当前没有内置某项能力而删掉用户要求；不确定处写进 requirement_gaps，"
                "不要假装已经确定。planning_idea 可以使用自然语言、Markdown 标题和列表，但不要写最终 CLI。"
            ),
        },
        {
            "role": "user",
            "content": (f"用户需求：{requirement}\n拓扑和已有事实：{prompt_baseline}\n"
                        "请返回包含完整 planning_idea 和 requirement_gaps 的规划 JSON。"),
        },
    ]


def refine_intent_with_llm(
    session: Session,
    *,
    requirement: str,
    baseline: dict[str, Any],
    on_event: Callable[[str, str, str], None] | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Return a capability-neutral refinement envelope or a safe fallback.

    The model may label a previously unsupported capability and add planning
    steps/retrieval terms.  It still cannot alter deterministically extracted
    VLAN IDs, emit CLI, invent topology facts, or invoke a tool.
    """

    settings = read_provider_settings(session)
    secret = get_provider_secret("llm")
    if not settings.llm_base_url or not settings.llm_model or not secret:
        return {
            "intent": baseline,
            "llm": {"status": "disabled", "reason": "未配置 LLM Base URL、模型或 API Key"},
        }
    try:
        llm_result = _run_async(
            request_text_result(
                base_url=settings.llm_base_url,
                api_key=secret,
                model=settings.llm_model,
                messages=_prompt(requirement, baseline),
                temperature=settings.llm_temperature,
                thinking=should_enable_thinking(settings.llm_thinking_mode, "intent_refinement"),
                max_tokens=INTENT_REFINEMENT_MAX_TOKENS,
                stream=bool(on_event),
                on_chunk=(
                    lambda thinking, formal: (
                        on_event("意图理解", "thinking", thinking) if thinking and on_event else None,
                        on_event("意图理解", "output", formal) if formal and on_event else None,
                    )
                ),
                cancel_event=cancel_event,
            )
        )
        refinement = parse_json_response(llm_result.content, LlmIntentRefinement)
    except PlanningCancelled:
        raise
    except Exception as exc:
        return {
            "intent": baseline,
            "llm": {"status": "fallback", "reason": f"LLM 结果不可用：{str(exc)[:240]}"},
        }

    # Keep topology-derived facts as the command compiler's source of truth,
    # but never discard a useful LLM planning explanation merely because it
    # uses another capability label (for example ``inter_vlan_routing``) or
    # repeats VLAN facts in a different order. The entire first-stage plan is
    # editable by the user; retaining the LLM's steps gives that review a real
    # proposal instead of an opaque generic fallback.
    baseline_vlan_ids = [int(item) for item in baseline.get("vlan_ids", [])]
    built_in_feature = baseline.get("feature") in {"vlan_access", "multi_vlan_intervlan"}
    vlan_fact_mismatch = refinement.vlan_ids != baseline_vlan_ids
    feature_label_mismatch = built_in_feature and refinement.feature != baseline.get("feature")
    model_capabilities = [
        item
        for item in [*refinement.capabilities, refinement.feature]
        if item and item not in {"generic", "unclassified"}
    ]
    planning_capabilities = list(
        dict.fromkeys(
            [
                *[
                    str(item).strip()
                    for item in baseline.get("planning_capabilities", [])
                    if str(item).strip()
                ],
                *model_capabilities,
            ]
        )
    )
    refined = {
        **baseline,
        "source": "llm_refined_with_topology_facts",
        # A specialized renderer depends on the deterministic feature, not on
        # the model's descriptive label. Generic capabilities stay open-ended.
        "feature": baseline.get("feature") if built_in_feature else refinement.feature,
        "llm_feature_label": refinement.feature,
        "llm_reported_vlan_ids": refinement.vlan_ids,
        "llm_reported_capabilities": refinement.capabilities,
        "planning_capabilities": planning_capabilities,
        "retrieval_terms": refinement.retrieval_terms,
        "planning_steps": refinement.planning_steps,
        "planning_summary": refinement.reason_summary,
        "llm_planning_idea": refinement.planning_idea,
        "requirement_gaps": refinement.requirement_gaps,
    }
    if vlan_fact_mismatch or feature_label_mismatch:
        warnings = list(refined.get("planning_warnings", []))
        warnings.append(
            "LLM 的能力标签或 VLAN 表述与拓扑解析不完全一致；已保留 LLM 的规划说明，"
            "命令生成仍以当前拓扑和需求中的 VLAN/端口事实为准。"
        )
        refined["planning_warnings"] = warnings
    return {
        "intent": refined,
        "llm": {
            "status": "accepted_with_topology_facts"
            if vlan_fact_mismatch or feature_label_mismatch
            else "accepted",
            "model": settings.llm_model,
            "node": "intent_refinement",
            "thinking_requested": llm_result.thinking_requested,
            "thinking_used": llm_result.thinking_used,
            "thinking_fallback": llm_result.thinking_fallback,
            "thinking_fallback_reason": llm_result.fallback_reason,
        },
    }
