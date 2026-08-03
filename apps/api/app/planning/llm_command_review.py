"""Independent, non-executing review of compiled command plans."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import parse_json_response, request_text_result, should_enable_thinking
from app.schemas import LlmCommandReview
from app.services.settings import get_provider_secret, read_provider_settings


def _run_async(coroutine):  # type: ignore[no-untyped-def]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _prompt(
    *,
    intent: dict[str, Any],
    command_plan: dict[str, Any],
    commands: list[str],
    validation: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> list[dict[str, str]]:
    compact_evidence = [
        {
            "command_id": item.get("command_id"),
            "canonical_name": item.get("canonical_name"),
            "syntax": item.get("syntax", []),
        }
        for item in evidence
    ]
    return [
        {
            "role": "system",
            "content": (
                "你是工业交换机配置的独立安全审查节点。只输出 JSON，不输出 Markdown、替代 CLI、"
                "密码或工具调用。你只能判断给定命令是否符合给定意图、手册证据和拓扑端口范围；"
                "不得新增命令、端口、VLAN 或设备。只有发现明确的严重不一致才 verdict=reject。"
                'JSON Schema: {"action":"command_review","verdict":"approve|reject",'
                '"issues":[],"required_changes":[],"reason_summary":"不超过400字"}。'
            ),
        },
        {
            "role": "user",
            "content": (
                f"意图：{intent}\n命令计划：{command_plan}\n编译后的 CLI：{commands}\n"
                f"静态校验：{validation}\n手册证据：{compact_evidence}\n请输出审查 JSON。"
            ),
        },
    ]


def review_commands_with_llm(
    session: Session,
    *,
    intent: dict[str, Any],
    command_plan: dict[str, Any],
    commands: list[str],
    validation: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> tuple[LlmCommandReview | None, dict[str, Any]]:
    settings = read_provider_settings(session)
    secret = get_provider_secret("llm")
    if not settings.llm_base_url or not settings.llm_model or not secret:
        return None, {"status": "disabled", "node": "command_review"}
    try:
        result = _run_async(
            request_text_result(
                base_url=settings.llm_base_url,
                api_key=secret,
                model=settings.llm_model,
                messages=_prompt(
                    intent=intent,
                    command_plan=command_plan,
                    commands=commands,
                    validation=validation,
                    evidence=evidence,
                ),
                temperature=0.0,
                thinking=should_enable_thinking(settings.llm_thinking_mode, "command_review"),
            )
        )
        review = parse_json_response(result.content, LlmCommandReview)
    except Exception as exc:
        return None, {"status": "fallback", "node": "command_review", "reason": str(exc)[:240]}
    return review, {
        "status": "accepted",
        "node": "command_review",
        "model": settings.llm_model,
        "verdict": review.verdict,
        "thinking_requested": result.thinking_requested,
        "thinking_used": result.thinking_used,
        "thinking_fallback": result.thinking_fallback,
        "thinking_fallback_reason": result.fallback_reason,
    }
