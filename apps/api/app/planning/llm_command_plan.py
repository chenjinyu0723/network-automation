"""Evidence-bound LLM command planning for the first supported feature.

The model chooses handbook records and arguments.  It never emits executable
CLI; ``compile_command_plan`` is the sole producer of CLI text.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import parse_json_response, request_text_result, should_enable_thinking
from app.schemas import LlmCommandPlan
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
    requirement: str,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
) -> list[dict[str, str]]:
    compact_evidence = [
        {
            "command_id": item.get("command_id"),
            "canonical_name": item.get("canonical_name"),
            "syntax": item.get("syntax", []),
            "views": item.get("views", []),
        }
        for item in evidence
    ]
    return [
        {
            "role": "system",
            "content": (
                "你是工业交换机命令计划节点。只输出一个 JSON，不输出 Markdown、CLI、密码或工具调用。"
                "每个 invocation 必须引用给定 evidence 的 command_id；不能创建新 command_id、"
                "不能新增设备/端口/VLAN。只能为已给出的拓扑端口生成 Access VLAN 计划。"
                'JSON Schema: {"action":"command_plan","operations":[{"purpose":"...",'
                '"invocations":[{"command_id":"...","syntax_index":0,"arguments":{},'
                '"target_port_ref":"topology:port:<原始端口>"}]}],'
                '"verification_notes":[],"assumptions":[],"risks":[]}。'
                "vlan batch 的 arguments 使用 vlan_ids 数组；port link-type 使用 link_type；"
                "port default vlan 使用 vlan_id。每个必要命令只引用一次，端口命令必须带 target_port_ref。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户需求：{requirement}\n意图：{intent}\n拓扑端口（只能使用这些）：{topology_ports}\n"
                f"手册证据：{compact_evidence}\n请输出受约束 command_plan JSON。"
            ),
        },
    ]


def plan_commands_with_llm(
    session: Session,
    *,
    requirement: str,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
) -> tuple[LlmCommandPlan | None, dict[str, Any]]:
    settings = read_provider_settings(session)
    secret = get_provider_secret("llm")
    if not settings.llm_base_url or not settings.llm_model or not secret:
        return None, {"status": "disabled", "node": "command_plan"}
    try:
        result = _run_async(
            request_text_result(
                base_url=settings.llm_base_url,
                api_key=secret,
                model=settings.llm_model,
                messages=_prompt(requirement, intent, evidence, topology_ports),
                temperature=min(settings.llm_temperature, 0.2),
                thinking=should_enable_thinking(settings.llm_thinking_mode, "command_plan"),
            )
        )
        plan = parse_json_response(result.content, LlmCommandPlan)
    except Exception as exc:
        return None, {"status": "fallback", "node": "command_plan", "reason": str(exc)[:240]}
    return plan, {
        "status": "accepted",
        "node": "command_plan",
        "model": settings.llm_model,
        "thinking_requested": result.thinking_requested,
        "thinking_used": result.thinking_used,
        "thinking_fallback": result.thinking_fallback,
        "thinking_fallback_reason": result.fallback_reason,
    }


def compile_command_plan(
    plan: LlmCommandPlan,
    *,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
) -> tuple[list[str], dict[str, Any]]:
    """Validate references and compile the limited VLAN plan into Huawei CLI."""

    vlan_ids = intent.get("vlan_ids", [])
    if intent.get("feature") != "vlan_access" or not vlan_ids:
        return [], {"status": "blocked", "errors": ["当前意图不是可编译的 VLAN Access。"]}
    by_id = {str(item.get("command_id")): item for item in evidence}
    required = {"vlan batch", "port link-type", "port default vlan"}
    invocations = [invocation for operation in plan.operations for invocation in operation.invocations]
    names: list[str] = []
    compiled_by_port: dict[str, dict[str, Any]] = {}
    vlan_invocation = None
    for invocation in invocations:
        evidence_item = by_id.get(invocation.command_id)
        if not evidence_item:
            return [], {
                "status": "blocked",
                "errors": [f"LLM 引用了不存在的手册命令：{invocation.command_id}"],
            }
        name = str(evidence_item.get("canonical_name", "")).lower()
        names.append(name)
        syntax = evidence_item.get("syntax") or []
        if invocation.syntax_index >= len(syntax):
            return [], {
                "status": "blocked",
                "errors": [f"LLM 选择的 syntax_index 越界：{invocation.command_id}"],
            }
        if name == "vlan batch":
            if vlan_invocation is not None:
                return [], {"status": "blocked", "errors": ["LLM 重复规划 vlan batch。"]}
            vlan_invocation = invocation
        elif name in {"port link-type", "port default vlan"}:
            port = invocation.target_port_ref or ""
            if not port.startswith("topology:port:"):
                return [], {"status": "blocked", "errors": [f"端口命令缺少拓扑引用：{name}"]}
            port = port.removeprefix("topology:port:")
            if port not in topology_ports:
                return [], {"status": "blocked", "errors": [f"LLM 引用了拓扑外端口：{port}"]}
            if name in compiled_by_port.get(port, {}):
                return [], {"status": "blocked", "errors": [f"端口 {port} 重复规划命令：{name}"]}
            compiled_by_port.setdefault(port, {})[name] = invocation
        else:
            return [], {"status": "blocked", "errors": [f"当前功能不允许命令：{name}"]}
    if set(names) != required or not vlan_invocation:
        return [], {"status": "blocked", "errors": ["LLM 命令计划未覆盖 VLAN Access 所需的三类手册命令。"]}
    for port in topology_ports:
        item = compiled_by_port.get(port, {})
        if set(item) != {"port link-type", "port default vlan"}:
            return [], {"status": "blocked", "errors": [f"端口 {port} 缺少完整 Access VLAN 命令。"]}
    if vlan_invocation.arguments.get("vlan_ids") != vlan_ids:
        return [], {"status": "blocked", "errors": ["LLM 生成的 VLAN 参数与确定性意图不一致。"]}
    commands = ["system-view", f"vlan batch {' '.join(str(item) for item in vlan_ids)}"]
    for port in topology_ports:
        link = compiled_by_port[port]["port link-type"]
        default = compiled_by_port[port]["port default vlan"]
        if link.arguments.get("link_type") != "access":
            return [], {"status": "blocked", "errors": [f"端口 {port} 的链路类型不是 access。"]}
        if default.arguments.get("vlan_id") != vlan_ids[0]:
            return [], {"status": "blocked", "errors": [f"端口 {port} 的 PVID 与确定性意图不一致。"]}
        commands.extend(
            [f"interface {port}", "port link-type access", f"port default vlan {vlan_ids[0]}", "quit"]
        )
    commands.append("return")
    return commands, {
        "status": "ready",
        "errors": [],
        "source": "llm_command_plan_compiled",
        "command_plan": plan.model_dump(mode="json"),
        "checks": ["每条命令绑定手册 command_id", "每个端口来自拓扑", "参数与确定性 Intent 一致"],
        "validation_commands": [
            f"display vlan {' '.join(str(item) for item in vlan_ids)}",
            *[f"display port vlan {port}" for port in topology_ports],
        ],
    }
