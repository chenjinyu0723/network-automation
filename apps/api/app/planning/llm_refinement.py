"""Constrained, capability-neutral LLM intent refinement for the planning graph."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from threading import Event
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import (
    json_format_repair_prompt,
    parse_json_response,
    request_text_result,
    should_enable_thinking,
)
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
    prompt_baseline = {
        "topology_context": baseline.get("topology_context", {}),
        "feature": baseline.get("feature"),
        "topology_capabilities": baseline.get("topology_capabilities", []),
        "vlan_ids": baseline.get("vlan_ids", []),
        "pc_vlan_map": baseline.get("pc_vlan_map", {}),
        "l3_core_node_id": baseline.get("l3_core_node_id"),
        "vlan_gateways": baseline.get("vlan_gateways", {}),
        "required_configuration_facts": baseline.get("required_configuration_facts", []),
        "existing_configuration_facts": baseline.get("existing_configuration_facts", []),
        "planning_warnings": baseline.get("planning_warnings", []),
        "reference_template": baseline.get("template_reference"),
    }
    baseline_json = json.dumps(prompt_baseline, ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": (
                "你是工业交换机配置规划节点。"
                "请先独立理解用户真正想实现的网络，再起草一份给用户审阅和修改的中文配置思路；它不是最终 CLI。"
                "不要把应用当前已实现的功能当作能力边界：任何厂商、协议、组网方式、业务组合都可以提出方案。"
                "需求缺少的参数、前置条件、验收条件或风险要明确列成建议补充项，不要因此拒绝规划。"
                "最后输出一个 JSON 对象，便于应用保存这份可编辑草案。"
                "JSON Schema: "
                '{"action":"refine_intent","feature":"能力标签，如 l3_ospf_ipv4",'
                '"capabilities":["需求中涉及的全部能力或子任务"],'
                '"vlan_ids":[1..4094],"retrieval_terms":["需要查手册的关键词"],'
                '"planning_steps":["实施步骤"],"planning_idea":"完整可编辑思路",'
                '"requirement_gaps":["需求中缺少但建议补充的事项"],"reason_summary":"简短总结"}。'
                "feature 和 capabilities 只是帮助组织需求的自由文本标签，不是白名单、命令白名单或拦截条件；"
                "可以使用任意厂商、协议、业务和设备能力名称，组合需求必须完整保留。"
                "完整拓扑中的设备、IP、前缀、网关和真实端口连接均已提供；值为“未提供”时，"
                "请把建议值明确标为建议/待确认，而不是当成已有事实。"
                "planning_idea 可以使用自然语言、Markdown 标题和列表，但不要写最终 CLI。"
                "如提供参考模板，它只用于借鉴业务拆解、步骤组织和可能遗漏项。当前用户需求与当前拓扑"
                "才是唯一事实；不得照搬模板中的设备名、端口、IP、网关、VLAN 或 CLI。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户需求：{requirement}\n拓扑和已有事实：{baseline_json}\n"
                "请返回包含完整 planning_idea 和 requirement_gaps 的规划 JSON。"
            ),
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
    llm_result = None
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
        try:
            refinement = parse_json_response(llm_result.content, LlmIntentRefinement)
        except ValueError:
            if not llm_result.content.strip():
                raise
            if on_event:
                on_event("意图理解", "stage", "模型正式输出不是有效 JSON，正在进行一次格式修复。")
            repaired_result = _run_async(
                request_text_result(
                    base_url=settings.llm_base_url,
                    api_key=secret,
                    model=settings.llm_model,
                    messages=json_format_repair_prompt(
                        schema_contract=(
                            '{"action":"refine_intent","feature":"string","capabilities":["string"],'
                            '"vlan_ids":[1],"retrieval_terms":["string"],"planning_steps":["string"],'
                            '"planning_idea":"string","requirement_gaps":["string"],"reason_summary":"string"}'
                        ),
                        answer=llm_result.content,
                    ),
                    temperature=0.0,
                    thinking=False,
                    max_tokens=INTENT_REFINEMENT_MAX_TOKENS,
                    cancel_event=cancel_event,
                )
            )
            refinement = parse_json_response(repaired_result.content, LlmIntentRefinement)
            llm_result = repaired_result
    except PlanningCancelled:
        raise
    except Exception as exc:
        # A small/weak local model sometimes gives a useful natural-language
        # plan but malformed JSON. Preserve that proposal for the user's
        # editable first stage instead of replacing it with the application's
        # old fixed VLAN/topology outline.
        raw_plan = str(getattr(llm_result, "content", "") or "").strip()
        if raw_plan:
            return {
                "intent": {
                    **baseline,
                    "source": "llm_unstructured_planning_idea",
                    "llm_planning_idea": raw_plan[:12_000],
                    "requirement_gaps": [],
                    "retrieval_terms": list(baseline.get("retrieval_terms", [])),
                },
                "llm": {
                    "status": "unstructured_fallback",
                    "reason": f"LLM 返回的规划文本未满足 JSON 格式：{str(exc)[:240]}",
                },
            }
        return {
            "intent": baseline,
            "llm": {"status": "fallback", "reason": f"LLM 结果不可用：{str(exc)[:240]}"},
        }

    # Keep topology-derived facts for the later command stage, but leave the
    # human-facing planning text unconstrained. The operator is explicitly
    # allowed to review and edit the model's proposal.
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
