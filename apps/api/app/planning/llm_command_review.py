"""Independent, non-executing review of compiled command plans."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Event
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import parse_json_response, request_text_result, should_enable_thinking
from app.planning.runtime import PlanningCancelled
from app.schemas import LlmCommandReview
from app.services.settings import get_provider_secret, read_provider_settings

# Command review returns a compact verdict JSON; a token budget avoids an
# unbounded reasoning trace without imposing a wall-clock request timeout.
COMMAND_REVIEW_MAX_TOKENS = 4_096


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
    # The compiler may retain a wider catalogue packet so late-discovered
    # commands remain bindable. The reviewer only needs the highest-ranked
    # provenance pages; keeping this packet compact avoids turning a review
    # into another retrieval-sized model request.
    compact_evidence = [
        {
            "command_id": item.get("command_id"),
            "canonical_name": item.get("canonical_name"),
            "syntax": item.get("syntax", []),
        }
        for item in evidence[:32]
    ]
    return [
        {
            "role": "system",
            "content": (
                "你是工业交换机配置的独立安全审查节点。只输出 JSON，不输出 Markdown、替代 CLI、"
                "密码或工具调用。你只能判断给定命令是否符合给定意图、手册证据和拓扑端口范围；"
                "不得新增命令、端口、VLAN 或设备。当前输入只是一台设备的命令切片："
                "不得因为其他交换机应有的 Access、Trunk 或 VLANIF 命令没有出现在当前 CLI 中而 reject；"
                "必须以 current_device_scope 判断这台设备应承担的角色。"
                "只有发现当前设备范围内明确的严重不一致才 verdict=reject。"
                "中文需求中“接口/端口 配置 <地址或参数>”在没有“已配置、已经、当前已、现有、已存在”"
                "等完成标记时是待执行动作；若相应 CLI 缺失，必须 reject，不能把它当作既有前提。"
                "反过来，若 CLI 包含用户需求、已确认配置思路、结构化事实和当前设备角色范围均未明确要求的"
                "可变业务状态，也必须 reject；不得把‘最佳实践’或模板参考当成授权。"
                "例如只要求启用协议/模式时，不能额外设置区域名、实例、VLAN、优先级、地址、策略或其他"
                "可选子配置；若额外子配置未完成且会造成半配置，更应 reject。"
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


def _format_repair_prompt(answer: str) -> list[dict[str, str]]:
    """Recover a review verdict when a compatible model wraps its JSON in prose."""

    return [
        {
            "role": "system",
            "content": (
                "你是 JSON 格式修复节点。只输出一个合法 JSON 对象，不输出 Markdown、CLI、"
                "密码或解释。不得新增、删除或改写原审阅的结论、问题或修改要求。"
                '目标 Schema: {"action":"command_review","verdict":"approve|reject",'
                '"issues":[],"required_changes":[],"reason_summary":""}。'
            ),
        },
        {"role": "user", "content": f"待修复审阅输出：\n{answer}"},
    ]


def review_commands_with_llm(
    session: Session,
    *,
    intent: dict[str, Any],
    command_plan: dict[str, Any],
    commands: list[str],
    validation: dict[str, Any],
    evidence: list[dict[str, Any]],
    on_event: Callable[[str, str, str], None] | None = None,
    cancel_event: Event | None = None,
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
                max_tokens=COMMAND_REVIEW_MAX_TOKENS,
                stream=bool(on_event),
                on_chunk=(lambda thinking, formal: (
                    on_event("命令审阅", "thinking", thinking) if thinking and on_event else None,
                    on_event("命令审阅", "output", formal) if formal and on_event else None,
                )),
                cancel_event=cancel_event,
            )
        )
        try:
            review = parse_json_response(result.content, LlmCommandReview)
            format_repair_attempted = False
        except ValueError:
            repaired = _run_async(
                request_text_result(
                    base_url=settings.llm_base_url,
                    api_key=secret,
                    model=settings.llm_model,
                    messages=_format_repair_prompt(result.content),
                    temperature=0.0,
                    thinking=False,
                    stream=False,
                    cancel_event=cancel_event,
                )
            )
            review = parse_json_response(repaired.content, LlmCommandReview)
            format_repair_attempted = True
    except PlanningCancelled:
        raise
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
        "format_repair_attempted": format_repair_attempted,
    }
