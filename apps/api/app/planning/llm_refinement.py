"""Constrained LLM intent refinement for the planning graph.

The model is never asked to emit CLI or invoke a tool.  It can only return a
small JSON intent that is checked against deterministic facts extracted from the
user requirement.  The renderer, safety validation and execution gates remain
deterministic.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import parse_json_response, request_text_result, should_enable_thinking
from app.schemas import LlmIntentRefinement
from app.services.settings import get_provider_secret, read_provider_settings


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
    return [
        {
            "role": "system",
            "content": (
                "你是工业交换机配置规划中的受限意图分析节点。"
                "只输出一个 JSON 对象，不能输出 Markdown、CLI 命令、工具调用或解释性文本。"
                "JSON Schema: "
                '{"action":"refine_intent","feature":"vlan_access|unclassified",'
                '"vlan_ids":[1..4094],"retrieval_terms":["最多8个手册检索词"],'
                '"reason_summary":"不超过300字"}。'
                "只能从用户需求明确表达的信息中提取 VLAN 编号；不确定则 feature=unclassified、vlan_ids=[]。"
                "retrieval_terms 只能是用于检索手册的功能或命令关键词，不能包含命令参数或设备操作。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户需求：{requirement}\n"
                f"确定性基线：{baseline}\n"
                "请返回受限意图 JSON。"
            ),
        },
    ]


def refine_intent_with_llm(
    session: Session,
    *,
    requirement: str,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Return a safe refinement envelope; outages and invalid JSON fall back.

    A model is allowed to add an explanatory summary and retrieval terms only
    after it agrees with deterministic VLAN parsing.  It cannot introduce a new
    VLAN ID, a command, a target device, or a tool action.
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
            )
        )
        refinement = parse_json_response(llm_result.content, LlmIntentRefinement)
    except Exception as exc:
        return {
            "intent": baseline,
            "llm": {"status": "fallback", "reason": f"LLM 结果不可用：{str(exc)[:240]}"},
        }

    baseline_vlan_ids = [int(item) for item in baseline.get("vlan_ids", [])]
    if (
        baseline.get("feature") != "vlan_access"
        or refinement.feature != "vlan_access"
        or refinement.vlan_ids != baseline_vlan_ids
    ):
        return {
            "intent": baseline,
            "llm": {
                "status": "rejected",
                "reason": "LLM 意图与确定性 VLAN 编号不一致，已保留安全基线。",
            },
        }

    refined = {
        **baseline,
        "source": "llm_refined_with_deterministic_guard",
        "retrieval_terms": refinement.retrieval_terms,
        "planning_summary": refinement.reason_summary,
    }
    return {
        "intent": refined,
        "llm": {
            "status": "accepted",
            "model": settings.llm_model,
            "node": "intent_refinement",
            "thinking_requested": llm_result.thinking_requested,
            "thinking_used": llm_result.thinking_used,
            "thinking_fallback": llm_result.thinking_fallback,
            "thinking_fallback_reason": llm_result.fallback_reason,
        },
    }
