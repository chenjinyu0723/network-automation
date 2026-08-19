"""Evidence-bound command planning for built-in and capability-neutral flows."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from threading import Event
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import (
    FormalResponseTimeout,
    ThinkingBudgetExceeded,
    parse_json_response,
    request_text_result,
    should_enable_thinking,
)
from app.planning.dialect import HUAWEI_VRP, CliDialect, is_huawei_vlan_renderer
from app.planning.runtime import PlanningCancelled
from app.ports import port_identity
from app.schemas import LlmCommandPlan
from app.services.settings import get_provider_secret, read_provider_settings

CONTROL_COMMAND_ID = "__control__"
COMMAND_PLAN_MAX_TOKENS = 8_192
FORBIDDEN_PLAN_PREFIXES = (
    "save",
    "reboot",
    "reset",
    "format",
    "delete",
    "clear",
    "erase",
    "reload",
    "write memory",
    "copy running-config",
)

VLAN_ACCESS_COMMAND_PREFIXES = {
    "vlan batch": "vlan batch",
    "port link-type": "port link-type",
    "port default vlan": "port default vlan",
}
VLAN_INTERVLAN_COMMAND_PREFIXES = {
    **VLAN_ACCESS_COMMAND_PREFIXES,
    "port trunk allow-pass vlan": "port trunk allow-pass vlan",
    "interface": "interface ",
    "ip address": "ip address ",
}


def _prompt_evidence(
    requirement: str,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    dialect: CliDialect,
    topology_ports: list[str],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Keep command-planning context focused without weakening compilation.

    Raw command pages can carry multi-page notes and examples. The compiler
    still receives that complete evidence, while this LLM-facing projection
    keeps only the command grammar and compact applicability clues, ranked from
    the current intent instead of a vendor/feature-specific list.
    """

    source_text = " ".join(
        [
            requirement,
            str(intent.get("planning_summary") or ""),
            " ".join(str(item) for item in intent.get("retrieval_terms", [])),
            " ".join(
                str(item.get("kind") or "")
                for item in intent.get("required_configuration_facts", [])
                if isinstance(item, dict)
            ),
            " ".join(
                f"{item.get('port', '')} {item.get('command_hint', '')} {item.get('argument', '')}"
                for item in intent.get("required_port_command_facts", [])
                if isinstance(item, dict)
            ),
        ]
    )
    terms = {
        item.casefold()
        for item in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", source_text)
        if len(item) >= 3
    }
    requires_interface_address = any(
        isinstance(item, dict)
        and item.get("kind") in {"interface_address", "logical_interface_address"}
        for item in intent.get("required_configuration_facts", [])
    ) or bool(
        re.search(
            r"(?:interface|vlanif|loopback|svi)[^。；;\n]{0,32}(?:ip\s*)?地址",
            requirement,
            re.IGNORECASE,
        )
    )
    if requires_interface_address:
        terms.update({"interface", "ip", "address"})
    interface_view_hints = {
        value.casefold()
        for value in re.findall(
            r"\b([A-Za-z][A-Za-z-]*)(?=\d+(?:/\d+)*\b)", requirement
        )
        if len(value) >= 2
    }
    conversion_evidence = (dialect.l3_physical_interface_conversion_evidence or "").casefold()
    feature_terms = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]*", str(intent.get("feature") or ""))
        if len(token) >= 3 and token.casefold() not in {"ipv4", "ipv6"}
    }
    port_families = {
        matched.group(1).casefold()
        for port in topology_ports
        if (matched := re.match(r"([A-Za-z]+)", port.strip()))
    }

    def score(item: dict[str, Any]) -> tuple[
        int, int, int, int, int, int, int, int, int, int, int, float, int, str
    ]:
        material = " ".join(
            [
                str(item.get("canonical_name") or ""),
                " ".join(str(value) for value in item.get("syntax", [])),
                " ".join(str(value) for value in item.get("views", [])),
            ]
        ).casefold()
        matches = sum(1 for term in terms if term in material)
        name = str(item.get("canonical_name") or "").casefold()
        conversion_match = int(bool(conversion_evidence and name == conversion_evidence))
        address_action_match = int(
            requires_interface_address
            and (name == "interface" or name.startswith("ip address"))
        )
        exact_intent_name_match = int(name in terms)
        feature_name_match = int(name in feature_terms)
        view_material = " ".join(str(value) for value in item.get("views", [])).casefold()
        logical_view_match = int(
            bool(interface_view_hints)
            and any(hint in view_material for hint in interface_view_hints)
        )
        canonical_root = name.split("（", 1)[0].split("(", 1)[0].strip()
        bare_syntax_match = int(
            any(
                _normalize_cli(str(value)).casefold() == canonical_root
                for value in item.get("syntax", [])
            )
        )
        manual_requirement_relevance = int(item.get("manual_requirement_relevance") or 0)
        feature_view_match = int(any(term in view_material for term in feature_terms))
        physical_view_match = int(
            bool(port_families)
            and any(
                family in view_material
                for family in port_families
            )
        )
        exact_match = int("exact_name" in set(item.get("retrieval_sources", [])))
        active_priority = item.get("active_retrieval_priority")
        try:
            active_rank = int(active_priority)
        except (TypeError, ValueError):
            active_rank = 10_000
        return (
            -conversion_match,
            -address_action_match,
            -logical_view_match,
            -bare_syntax_match,
            -manual_requirement_relevance,
            -feature_name_match,
            -feature_view_match,
            -exact_intent_name_match,
            -matches,
            -physical_view_match,
            -exact_match,
            -float(item.get("retrieval_score") or 0),
            # An active retrieval follow-up can break ties between comparable
            # pages, but it must not outrank the command's actual syntax/view
            # context. For example, a broad ``ip address`` search may return
            # an ACL view before a physical-interface view.
            active_rank,
            str(item.get("canonical_name") or ""),
        )

    def short_list(value: Any, *, items: int, chars: int) -> list[str]:
        result: list[str] = []
        for item in value or []:
            compact = _normalize_cli(str(item))
            if compact:
                result.append(compact[:chars])
            if len(result) >= items:
                break
        return result

    compact: list[dict[str, Any]] = []
    seen_command_names: set[str] = set()
    mandatory_names = (
        {"vlan batch"}
        if intent.get("vlan_ids") and dialect.supports_huawei_vlan_renderer
        else set()
    )
    # An explicit VLAN ID is a structural fact, and the selected Huawei VRP
    # dialect documents VLAN creation under ``vlan batch``. Preserve that
    # evidence before generic semantic ranking so an unrelated interface page
    # cannot make a model assume the VLAN already exists. Other vendors simply
    # do not activate this dialect capability and continue on the generic path.
    for item in evidence:
        command_name = str(item.get("canonical_name") or "").split("（", 1)[0].split("(", 1)[0].strip()
        name_key = command_name.casefold()
        if name_key not in mandatory_names or name_key in seen_command_names:
            continue
        seen_command_names.add(name_key)
        compact.append(
            {
                "command_id": item.get("command_id"),
                "canonical_name": item.get("canonical_name"),
                "matched_command": item.get("matched_command"),
                "syntax": short_list(item.get("syntax"), items=8, chars=220),
                "views": short_list(item.get("views"), items=8, chars=100),
                "preconditions": short_list(item.get("preconditions"), items=2, chars=260),
                "constraints": short_list(item.get("constraints"), items=2, chars=220),
                "examples": short_list(item.get("examples"), items=1, chars=260),
            }
        )
        if len(compact) >= limit:
            return compact
    # Keep the LLM-facing packet smaller than the compiler's complete evidence
    # set.  It needs the precise pages found by the active search, whereas the
    # compiler still retains all neighbouring pages for provenance checks.
    for item in sorted(evidence, key=score):
        command_name = str(item.get("canonical_name") or "").split("（", 1)[0].split("(", 1)[0].strip()
        name_key = command_name.casefold()
        if name_key and name_key in seen_command_names:
            continue
        if name_key:
            seen_command_names.add(name_key)
        compact.append(
            {
                "command_id": item.get("command_id"),
                "canonical_name": item.get("canonical_name"),
                "matched_command": item.get("matched_command"),
                "syntax": short_list(item.get("syntax"), items=8, chars=220),
                "views": short_list(item.get("views"), items=8, chars=100),
                "preconditions": short_list(item.get("preconditions"), items=2, chars=260),
                "constraints": short_list(item.get("constraints"), items=2, chars=220),
                "examples": short_list(item.get("examples"), items=1, chars=260),
            }
        )
        if len(compact) >= limit:
            break
    return compact


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
    device_scope: dict[str, Any] | None,
    dialect: CliDialect,
    *,
    compact: bool = False,
) -> list[dict[str, str]]:
    compact_evidence = _prompt_evidence(
        requirement,
        intent,
        evidence,
        dialect,
        topology_ports,
        limit=7 if compact else 8,
    )
    if compact:
        compact_evidence = [
            {
                "command_id": item.get("command_id"),
                "canonical_name": item.get("canonical_name"),
                "syntax": list(item.get("syntax", []))[:3],
                "views": list(item.get("views", []))[:2],
            }
            for item in compact_evidence
        ]
    plugin_active = is_huawei_vlan_renderer(intent, dialect)
    is_intervlan = plugin_active and intent.get("feature") == "multi_vlan_intervlan"
    is_vlan_access = plugin_active and intent.get("feature") == "vlan_access"
    vlan_l2_roles = dict((device_scope or {}).get("vlan_l2_roles") or {})
    vlan_l2_rules = (
        "当前拓扑还给出了 VLAN 二层端口职责，必须严格遵守："
        f"Access 端口={vlan_l2_roles.get('access_ports', [])}，"
        f"交换机互联 Trunk 端口={vlan_l2_roles.get('trunk_ports', [])}，"
        f"业务 VLAN={vlan_l2_roles.get('vlan_ids', [])}。"
        "每个交换机互联 Trunk 必须允许全部业务 VLAN，不能把不同 VLAN 拆到不同上联；"
        "PC 端口只能配置对应 Access VLAN；显式三层端口不属于二层职责。"
        if vlan_l2_roles
        else ""
    )
    generic_session_rules = (
        "配置入口 "
        f"{', '.join(dialect.configuration_enter)} 和结尾 "
        f"{', '.join(dialect.configuration_exit)} 由编译器添加，"
        "不应输出；仅在需要退出子视图时可以使用 command_id=__control__，"
        f"cli 只能是：{', '.join(sorted(dialect.control_commands))}。"
        if dialect.configuration_enter
        else (
            "当前手册未选择会话方言，不能擅自添加配置入口或结尾命令；"
            "如手册要求进入视图，必须引用对应的手册 evidence。"
        )
    )
    generic_rules = (
        f"当前是通用手册驱动能力，CLI 方言为 {dialect.label}。请针对当前设备生成按顺序执行的单行 CLI 草案。"
        "每条业务 CLI 必须引用一个给定 evidence 的 command_id，并与该命令页的语法、视图、"
        "前置条件和示例一致；不能使用手册外知识。"
        "不能输出 save、reboot、reset、format、delete、clear、erase、reload、"
        "write memory 或 copy running-config。"
        + generic_session_rules
        + "物理接口的 interface 命令必须带 target_port_ref=topology:port:<端口>，且只能使用"
        "当前设备的拓扑端口。物理接口进入命令必须引用 canonical_name 为 interface 的证据；"
        "接口视图内的子命令（例如地址、二三层切换、聚合成员）必须使用 target_port_ref=null；"
        "完成该接口全部子命令后先用 __control__/quit 退出，再进入下一个物理接口。"
        + (
            "当前设备以下物理端口已由用户明确要求配置三层地址："
            f"{', '.join(str(item) for item in device_scope.get('explicit_l3_ports', []))}。"
            "这些端口只能生成进入接口、手册定义的三层转换和地址等与该事实直接相关的动作；"
            "不得再对同一端口生成二层接入口、Trunk、VLAN归属或其它二层成员动作。"
            if device_scope and device_scope.get("explicit_l3_ports")
            else ""
        )
        + vlan_l2_rules
        + "若同一逻辑聚合接口既需要接收物理成员又需要三层地址，先完整配置所有物理成员并退出其视图，"
        "再进入逻辑接口完成三层配置。"
        "不要配置其他设备、未连线物理端口、密码或 SSH。"
        "系统视图/全局命令（例如路由协议、静态路由）不得附加 target_port_ref。"
        "用户需求或已确认配置思路中明确要求“配置/切换/创建”的每项动作都必须有对应 CLI；"
        "不能把它改写为假设。只有用户明确说明已经存在或已配置的事实，才可以列为假设。"
        "只生成实现当前明确需求所必需的最小配置；不得以经验、最佳实践或模板内容为由，"
        "擅自添加未被需求、已确认配置思路或结构化事实要求的可变业务状态。"
        "例如需求只要求启用某协议或模式时，不得自行新增区域名、实例、VLAN、优先级、地址、"
        "策略或其他可选子配置；缺少必填业务参数时，在 risks 说明，不能编造。"
        "retrieval_followup_terms 是检索节点发现的缺失动作；若其能对应给定手册证据中的完整命令，"
        "必须纳入本设备命令序列，不能仅写入 risks。"
        "operations 不能为空；不能只输出 quit、exit 等控制命令。即使某个参数的手册证据不完整，"
        "也必须基于最接近的给定手册证据输出完整、可人工审阅的业务 CLI 草案，并在 risks 中说明不确定点。"
        "特别地，“接口/端口 配置 <IP 地址或参数>”在没有“已配置、已经、当前已、现有、已存在”"
        "等完成标记时是待执行动作，必须生成对应接口命令。"
        + (
            "当前 CLI 方言规定：为物理交换端口或指定的三层逻辑接口配置上述地址前，必须先在同一接口视图使用"
            f"手册中 canonical_name 为 {dialect.l3_physical_interface_conversion_evidence} 的证据执行 "
            f"{dialect.l3_physical_interface_conversion_command}；不可假设端口已经是三层模式。"
            if dialect.l3_physical_interface_conversion_command
            and dialect.l3_physical_interface_conversion_evidence
            else ""
        )
    )
    relaxed_mode = bool(intent.get("relaxed_command_mode"))
    mode_rules = (
        "当前为宽松的人工审阅草案模式。请先利用给定手册证据和主动检索线索理解需求，"
        "再按设备角色输出你认为合理的完整 CLI 顺序；command_id、syntax_index 和端口引用"
        "可以留空或使用最接近的证据，手册没有覆盖的命令也可以按模型知识给出，并在 risks 中说明。"
        "不要因为无法完成静态绑定、型号差异或参数不完整而省略命令，目标是给用户一套可编辑的"
        "大致正确草案。"
        + vlan_l2_rules
        if relaxed_mode
        else (
            "当前是多 VLAN 跨交换机互通：device_scope 已经给出不可更改的 access_ports、"
            "trunk_ports、vlanifs。你必须为以下命令各选择匹配的手册证据：vlan batch、"
            "port link-type、port default vlan、port trunk allow-pass vlan、interface、ip address。"
            "Access 端口的 link_type=access 和 vlan_id 必须对应 device_scope；Trunk 端口的 "
            "link_type=trunk、vlan_ids 必须对应 device_scope；VLANIF 用 target_port_ref="
            "topology:vlanif:<VLAN ID>。"
            if is_intervlan
            else (
                "当前是单 VLAN Access：只能为已给出的拓扑端口生成 Access VLAN 计划。"
                if is_vlan_access
                else generic_rules
            )
        )
    )
    template_rules = (
        "\n模板参考只用于借鉴设备角色、实施顺序和命令组织。模板中的设备名、端口、VLAN、"
        "IP、掩码及任何 CLI 参数均不是当前任务事实，禁止复制；只能使用当前意图、设备角色范围"
        "和拓扑端口中明确给出的值。"
        if dict(intent.get("template_reference") or {}).get("title")
        else ""
    )
    repair_rules = (
        "\n上一次命令草案未通过独立审阅或静态手册校验。以下是只能用于修复当前草案的反馈："
        f"{dict(intent.get('command_repair_feedback') or {})}。"
        "必须从头输出完整 command_plan，逐项补齐反馈指出的已确认动作；"
        "不得只输出增量、不得把缺项写入 assumptions，也不得修改拓扑事实。"
        if dict(intent.get("command_repair_feedback") or {})
        else ""
    )
    template_reference = dict(intent.get("template_reference") or {})
    compact_template_reference = {
        "title": template_reference.get("title"),
        "description": template_reference.get("description"),
        "reference_planning_idea": str(template_reference.get("reference_planning_idea") or "")[
            : 300 if compact else 1600
        ],
    }
    prompt_intent: dict[str, Any] = {
        "feature": intent.get("feature"),
        "required_configuration_facts": intent.get("required_configuration_facts", []),
        "existing_configuration_facts": intent.get("existing_configuration_facts", []),
        "required_port_command_facts": intent.get("required_port_command_facts", []),
        "retrieval_terms": intent.get("retrieval_terms", []),
        "retrieval_followup_terms": intent.get("retrieval_followup_terms", []),
        "planning_warnings": intent.get("planning_warnings", []),
        "template_reference": compact_template_reference if template_reference else None,
    }
    if compact:
        prompt_intent = {
            "feature": intent.get("feature"),
            "required_configuration_facts": intent.get("required_configuration_facts", []),
            "existing_configuration_facts": intent.get("existing_configuration_facts", []),
            "required_port_command_facts": intent.get("required_port_command_facts", []),
            "retrieval_followup_terms": intent.get("retrieval_followup_terms", []),
            "template_reference": compact_template_reference if template_reference else None,
        }
    compact_scope = (
        {
            "device": dict(device_scope or {}).get("device", {}),
            "all_ports": dict(device_scope or {}).get("all_ports", topology_ports),
            "protected_ports": dict(device_scope or {}).get("protected_ports", []),
        }
        if compact
        else (device_scope or {})
    )
    confirmed_idea_limit = 500 if compact else 2400
    confirmed_idea = str(intent.get("confirmed_planning_idea", ""))[:confirmed_idea_limit]
    planning_scope = str(intent.get("planning_idea_scope") or "generated_review_only")
    return [
        {
            "role": "system",
            "content": (
                "你是工业交换机命令计划节点。只输出一个 JSON；不能输出密码或工具调用。"
                + (
                    "这是人工审阅草案，不要求每条命令都能被当前手册静态绑定；保留你认为有帮助的命令。\n"
                    if relaxed_mode
                    else (
                        "不能输出 Markdown、JSON 外的自由 CLI。每个 invocation 必须引用给定 evidence 的 "
                        "command_id；不能创建新 command_id、不能新增设备/端口/VLAN。\n"
                    )
                )
                + mode_rules
                + template_rules
                + repair_rules
                + '\nJSON Schema: {"action":"command_plan","operations":[{"purpose":"...",'
                '"invocations":[{"command_id":"...","syntax_index":0,"arguments":{},'
                '"target_port_ref":"topology:port:<原始端口>","cli":"通用模式的一行 CLI"}]}],'
                '"verification_notes":[],"validation_commands":["只读查询或连通性命令"],'
                '"assumptions":[],"risks":[]}。'
                + (
                    "vlan batch 的 arguments 使用 vlan_ids 数组；port link-type 使用 link_type；"
                    "port default vlan 使用 vlan_id。每个必要命令只引用一次，端口命令必须带 target_port_ref。"
                    if (is_intervlan or is_vlan_access) and not relaxed_mode
                    else "命令按执行顺序列出即可；不确定的参数放入 risks，仍要给出可编辑 CLI。"
                    if relaxed_mode
                    else "通用模式中每个 invocation 的 cli 必须恰好一行；arguments 可为空。"
                )
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户需求：{requirement}\n执行意图：{json.dumps(prompt_intent, ensure_ascii=False)}\n"
                f"拓扑端口（只能使用这些）：{topology_ports}\n"
                f"用户主动编辑后授权的思路补充：{confirmed_idea}\n"
                f"思路授权状态：{planning_scope}（generated_review_only 时不得据自动思路扩展需求）。\n"
                f"设备角色范围（不可修改）：{json.dumps(compact_scope, ensure_ascii=False)}\n"
                f"手册证据：{compact_evidence}\n请输出受约束 command_plan JSON。"
            ),
        },
    ]


def _format_repair_prompt(answer: str) -> list[dict[str, str]]:
    """Ask for a bounded JSON-only repair after a weak model misses the schema.

    This node is deliberately non-reasoning: it only preserves the prior
    response's meaning in the required structure.  The normal handbook and
    topology compiler remains the authority on whether any repaired CLI can be
    retained.
    """

    return [
        {
            "role": "system",
            "content": (
                "你是 JSON 格式修复节点。只输出一个合法 JSON 对象，不能输出 Markdown、解释、"
                "密码或工具调用。只整理给定草案中已有的命令意图、端口和参数，不得新增、删除或改写"
                "任何网络事实。每个 invocation 保留 command_id、syntax_index、arguments、"
                "target_port_ref、cli 字段；缺失数组字段使用 []。"
                "每个 operation 最多只能有 8 个 invocation；如果草案更长，必须按原顺序拆成多个 operation，"
                "不得删除、合并或改写任何 invocation。"
                '目标 Schema: {"action":"command_plan","operations":[{"purpose":"...",'
                '"invocations":[{"command_id":"...","syntax_index":0,"arguments":{},'
                '"target_port_ref":null,"cli":"一行 CLI"}]}],"verification_notes":[],'
                '"validation_commands":[],"assumptions":[],"risks":[]}。'
            ),
        },
        {"role": "user", "content": f"待修复草案：\n{answer}"},
    ]


def _plain_cli_draft_prompt(
    requirement: str,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    device_scope: dict[str, Any] | None,
    dialect: CliDialect,
) -> list[dict[str, str]]:
    """Last-resort output mode for providers that cannot keep the JSON schema.

    This is deliberately a display-only draft.  The normal compiler will mark
    it unverified because no invocation-to-evidence binding exists, but the UI
    must still show the model's best command answer instead of an empty panel.
    """

    compact_evidence = _prompt_evidence(
        requirement, intent, evidence, dialect, topology_ports, limit=8
    )
    return [
        {
            "role": "system",
            "content": (
                "你是工业交换机配置草案节点。上一个 JSON 输出格式失败；现在只输出可人工审阅的 CLI。"
                "每行恰好一条业务配置命令，不能输出 JSON、Markdown、解释、序号、密码、"
                "save、reboot、reset、delete、clear、format、reload 或复制配置命令。"
                "必须根据用户需求和用户主动编辑后授权的思路补充输出完整草案；即使手册证据不完整也不能留空。"
                "用户明确写为已配置、当前已或现有的接口地址是当前状态事实；不得重复写入、修改或删除该状态。"
                "只允许使用当前设备已连线、未受保护的物理端口；不要输出 system-view、return、"
                "configure terminal、end 等会话入口/结尾，它们由系统显示层补齐。需要从接口或"
                "子视图回到上级视图时，必须单独输出 quit/exit，不能省略。"
                "当逻辑聚合接口既有物理成员又需要三层地址时，先完成每个物理成员接口块，再配置逻辑接口的三层命令。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户需求：{requirement}\n"
                f"用户主动编辑后授权的思路补充：{str(intent.get('confirmed_planning_idea') or '')[:2000]}\n"
                "不可重复写入的既有配置事实："
                f"{json.dumps(intent.get('existing_configuration_facts', []), ensure_ascii=False)}\n"
                f"当前设备范围：{json.dumps(device_scope or {}, ensure_ascii=False)}\n"
                f"可用物理端口：{topology_ports}\n"
                f"手册证据：{compact_evidence}\n"
                "请只返回每行一条 CLI 草案。"
            ),
        },
    ]


def _normalize_draft_interface_port(cli: str, topology_ports: list[str]) -> str:
    """Use the topology's exact spelling for a fallback physical interface.

    The main evidence compiler performs the same normalization through the
    structured port reference. Plain CLI fallback has no such reference, so it
    needs a small, alias-only equivalent to keep a user-entered ``GE0/0/1``
    from turning into ``GigabitEthernet0/0/1`` in the editable command panel.
    """

    matched = re.fullmatch(r"(interface\s+)(.+)", cli, re.IGNORECASE)
    if not matched:
        return cli
    candidate = matched.group(2).strip()
    candidate_key = _topology_port_key(candidate)
    for topology_port in topology_ports:
        if _topology_port_key(topology_port) == candidate_key:
            return f"{matched.group(1)}{topology_port}"
    return cli


def _normalize_huawei_logical_interface(cli: str, dialect: CliDialect) -> str:
    """Normalize Huawei's common logical-interface spelling in display drafts.

    VRP examples consistently use ``interface Eth-Trunk <id>``.  Small models
    often collapse the separator to ``Eth-Trunk<id>`` while constructing a
    plain-text fallback.  This is a presentation/CLI spelling repair only;
    unknown vendor dialects are left untouched.
    """

    if dialect.key != "huawei_vrp":
        return cli
    return re.sub(
        r"^(interface\s+Eth-Trunk)(\d+)$",
        r"\1 \2",
        cli,
        flags=re.IGNORECASE,
    )


def _plain_cli_draft_plan(
    text: str,
    dialect: CliDialect,
    topology_ports: list[str] | None = None,
) -> LlmCommandPlan | None:
    """Turn a plain-text model fallback into the normal editable draft shape."""

    raw_lines: list[str] = []
    session_commands = {
        *(item.casefold() for item in dialect.configuration_enter),
        *(item.casefold() for item in dialect.configuration_exit),
        "configure terminal",
        "end",
    }
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            continue
        if not stripped:
            continue
        stripped = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s*)", "", stripped).strip("` ")
        line = _normalize_cli(stripped)
        lowered = line.casefold()
        if (
            not line
            or lowered in session_commands
            or any(_starts_with_prefix(lowered, prefix) for prefix in FORBIDDEN_PLAN_PREFIXES)
            or line.startswith(("{", "}", "[", "]", '"'))
            or "：" in line
            or re.search(r"[\u4e00-\u9fff]", line)
            or not re.match(r"^[A-Za-z][A-Za-z0-9 ./_:-]*$", line)
        ):
            continue
        raw_lines.append(line)

    # A plain-text fallback has no structured ``target_port_ref`` for the
    # compiler to normalize. Retain the spelling entered in the topology for
    # physical interface context commands, including the GE/GigabitEthernet
    # family chosen by the operator. This only rewrites aliases of an existing
    # in-scope port; logical interfaces and unfamiliar vendor formats remain
    # untouched.
    topology_ports = topology_ports or []
    lines = [
        _normalize_huawei_logical_interface(
            _normalize_draft_interface_port(line, topology_ports), dialect
        )
        for line in raw_lines
    ]

    # View-exit commands carry execution meaning in CLI sequences. Earlier
    # fallback parsing discarded every control command, which could leave a
    # following ``interface`` command inside the previous interface view. Keep
    # only controls between business commands, discard leading/repeated/trailing
    # exits, and continue letting the display renderer add global enter/exit.
    filtered: list[str] = []
    business_after = [False] * len(lines)
    seen_business = False
    for index in range(len(lines) - 1, -1, -1):
        business_after[index] = seen_business
        if lines[index].casefold() not in dialect.control_commands:
            seen_business = True
    seen_business = False
    for line, has_later_business in zip(lines, business_after, strict=True):
        is_control = line.casefold() in dialect.control_commands
        if is_control:
            if not seen_business or not has_later_business:
                continue
            if filtered and filtered[-1].casefold() in dialect.control_commands:
                continue
        else:
            seen_business = True
        filtered.append(line)

    # A weak model may omit a view exit altogether before beginning another
    # interface block. For dialects that document explicit interface exits,
    # restore that minimal session transition in the display-only fallback.
    # This is deliberately based on generic ``interface`` context recognition
    # and the selected dialect's control command, not a feature or vendor map.
    if dialect.requires_explicit_interface_exit and dialect.control_commands:
        view_exit = next(iter(dialect.control_commands))
        contextual_lines: list[str] = []
        in_interface_view = False
        for line in filtered:
            if _context_interface_name(line):
                if in_interface_view and (
                    not contextual_lines
                    or contextual_lines[-1].casefold() not in dialect.control_commands
                ):
                    contextual_lines.append(view_exit)
                in_interface_view = True
            elif line.casefold() in dialect.control_commands:
                in_interface_view = False
            contextual_lines.append(line)
        filtered = contextual_lines

    # Preserve the supplied order and repetitions. The same member command may
    # be required in two different interface views, so global deduplication
    # would silently delete valid configuration work from a fallback draft.
    lines = filtered[:80]
    if not lines:
        return None
    operations = []
    for index in range(0, len(lines), 8):
        purpose = "未验证的 LLM CLI 草案"
        if index:
            purpose = f"未验证的 LLM CLI 草案（续 {index // 8 + 1}）"
        operations.append(
            {
                "purpose": purpose,
                "invocations": [
                    {
                        "command_id": "__unverified_draft__",
                        "syntax_index": 0,
                        "arguments": {},
                        "target_port_ref": None,
                        "cli": line,
                    }
                    for line in lines[index : index + 8]
                ],
            }
        )
    return LlmCommandPlan.model_validate(
        {
            "action": "command_plan",
            "operations": operations,
            "verification_notes": [],
            "validation_commands": [],
            "assumptions": [],
            "risks": ["JSON 命令计划格式失败后生成的纯 CLI 草案，未完成证据绑定。"],
        }
    )


def _bind_plain_cli_draft_to_evidence(
    plan: LlmCommandPlan,
    *,
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    dialect: CliDialect,
) -> tuple[LlmCommandPlan, list[str]]:
    """Recover deterministic evidence bindings from a plain-text fallback.

    A provider that cannot preserve a JSON schema may still produce correct
    ordered CLI. The imported manual, not the model, can recover an opaque
    command ID when exactly one command page matches that CLI's syntax. Physical
    interface context is resolved only to an existing topology port. Ambiguous
    or unmatched lines deliberately retain the unverified sentinel so the
    caller can expose, rather than conceal, the uncertainty.
    """

    payload = plan.model_dump(mode="json")
    allowed_ports = {_topology_port_key(port): port for port in topology_ports}
    current_physical_port: str | None = None
    prior_commands: list[str] = []
    unbound: list[str] = []
    for operation in payload["operations"]:
        for invocation in operation["invocations"]:
            cli = _normalize_cli(str(invocation.get("cli") or ""))
            if not cli:
                continue
            lowered = cli.casefold()
            if lowered in dialect.control_commands:
                invocation["command_id"] = CONTROL_COMMAND_ID
                invocation["target_port_ref"] = None
                current_physical_port = None
                prior_commands.append(cli)
                continue

            interface_name = _context_interface_name(cli)
            physical_name = _physical_interface_name(interface_name) if interface_name else None
            target_port_ref: str | None = None
            if physical_name:
                expected_port = allowed_ports.get(_topology_port_key(physical_name))
                if expected_port:
                    if dialect.preserves_topology_port_spelling:
                        cli = f"interface {expected_port}"
                    target_port_ref = f"topology:port:{expected_port}"
            evidence_item = _resolve_evidence_binding(
                cli,
                evidence,
                current_physical_port=current_physical_port,
                prior_commands=prior_commands,
            )
            invocation["cli"] = cli
            invocation["target_port_ref"] = target_port_ref
            if evidence_item:
                invocation["command_id"] = str(evidence_item.get("command_id") or "__unverified_draft__")
            else:
                invocation["command_id"] = "__unverified_draft__"
                unbound.append(cli)

            if physical_name and target_port_ref:
                current_physical_port = _topology_port_key(physical_name)
            elif interface_name:
                current_physical_port = None
            prior_commands.append(cli)
    return LlmCommandPlan.model_validate(payload), unbound


def _has_business_cli(plan: LlmCommandPlan, dialect: CliDialect) -> bool:
    """Tell an empty/control-only generic plan from a usable CLI proposal."""

    return any(
        bool(invocation.cli and _normalize_cli(invocation.cli))
        and _normalize_cli(invocation.cli).casefold() not in dialect.control_commands
        for operation in plan.operations
        for invocation in operation.invocations
    )


def _normalize_operation_cardinality(answer: str) -> str:
    """Split only oversized operation lists before strict schema validation.

    Small models sometimes return an otherwise-valid command plan with all CLI
    lines grouped into a single operation.  Group boundaries carry no execution
    semantics: compiling always follows invocation order.  Splitting such a
    list preserves every command, argument, and order while conforming to the
    bounded schema used to protect the workflow from unbounded output.
    """

    candidate = answer.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[1] if "\n" in candidate else ""
        candidate = candidate.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return answer
    if not isinstance(data, dict) or not isinstance(data.get("operations"), list):
        return answer
    normalized_operations: list[dict[str, Any]] = []
    changed = False
    for operation in data["operations"]:
        if not isinstance(operation, dict) or not isinstance(operation.get("invocations"), list):
            normalized_operations.append(operation)
            continue
        invocations = operation["invocations"]
        if len(invocations) <= 8:
            normalized_operations.append(operation)
            continue
        changed = True
        for index in range(0, len(invocations), 8):
            chunk = dict(operation)
            chunk["invocations"] = invocations[index : index + 8]
            if index:
                chunk["purpose"] = f"{str(operation.get('purpose') or '命令操作')}（续 {index // 8 + 1}）"
            normalized_operations.append(chunk)
    if not changed:
        return answer
    data["operations"] = normalized_operations
    return json.dumps(data, ensure_ascii=False)


def plan_commands_with_llm(
    session: Session,
    *,
    requirement: str,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    device_scope: dict[str, Any] | None = None,
    on_event: Callable[[str, str, str], None] | None = None,
    cancel_event: Event | None = None,
    dialect: CliDialect = HUAWEI_VRP,
) -> tuple[LlmCommandPlan | None, dict[str, Any]]:
    settings = read_provider_settings(session)
    secret = get_provider_secret("llm")
    if not settings.llm_base_url or not settings.llm_model or not secret:
        return None, {"status": "disabled", "node": "command_plan"}
    try:
        thinking_node = "command_repair" if intent.get("command_repair_feedback") else "command_plan"
        thinking_enabled = should_enable_thinking(settings.llm_thinking_mode, thinking_node)

        def request_plan(*, compact: bool, stream: bool):
            return _run_async(
                request_text_result(
                    base_url=settings.llm_base_url,
                    api_key=secret,
                    model=settings.llm_model,
                    messages=_prompt(
                        requirement,
                        intent,
                        evidence,
                        topology_ports,
                        device_scope,
                        dialect,
                        compact=compact,
                    ),
                    temperature=min(settings.llm_temperature, 0.2),
                    thinking=thinking_enabled,
                    # Bounded output prevents a reasoning provider from
                    # spending indefinitely on a compact command plan. This
                    # is deliberately not an HTTP/application timeout.
                    max_tokens=COMMAND_PLAN_MAX_TOKENS,
                    # Keep UI event streams when requested. For a non-streaming
                    # caller, ordinary completion is the more compatible JSON
                    # transport for several OpenAI-compatible gateways.
                    stream=stream,
                    on_chunk=(
                        lambda thinking, formal: (
                            on_event("命令计划", "thinking", thinking) if thinking and on_event else None,
                            on_event("命令计划", "output", formal) if formal and on_event else None,
                        )
                    ),
                    cancel_event=cancel_event,
                )
            )

        compact_retry_reason: str | None = None
        try:
            result = request_plan(compact=False, stream=bool(on_event))
        except (ThinkingBudgetExceeded, FormalResponseTimeout) as exc:
            # Some compatible gateways ignore ``enable_thinking=false`` for a
            # reasoning model. Retry once with the same evidence and policy,
            # but a smaller context packet; this keeps the normal command
            # planner deterministic, observable, and bounded.
            compact_retry_reason = str(exc)[:240]
            try:
                result = request_plan(compact=True, stream=bool(on_event))
            except (ThinkingBudgetExceeded, FormalResponseTimeout):
                if not on_event:
                    raise
                # A stream failure must not make a correct handbook plan
                # impossible when the same provider supports regular JSON.
                result = request_plan(compact=True, stream=False)
        try:
            plan = parse_json_response(_normalize_operation_cardinality(result.content), LlmCommandPlan)
            format_repair_attempted = False
        except ValueError as initial_error:
            # A thinking-capable gateway can finish its stream with reasoning
            # chunks only. Formatting an empty formal response can never
            # recover a command plan, so save one local LLM round and request
            # the human-reviewable CLI draft directly instead.
            format_repair_attempted = bool(result.content.strip())
            repaired_content = ""
            if format_repair_attempted:
                repaired = _run_async(
                    request_text_result(
                        base_url=settings.llm_base_url,
                        api_key=secret,
                        model=settings.llm_model,
                        messages=_format_repair_prompt(result.content),
                        temperature=0.0,
                        thinking=False,
                        cancel_event=cancel_event,
                    )
                )
            try:
                if format_repair_attempted:
                    repaired_content = repaired.content
                plan = parse_json_response(_normalize_operation_cardinality(repaired_content), LlmCommandPlan)
            except ValueError as repair_error:
                draft_result = _run_async(
                    request_text_result(
                        base_url=settings.llm_base_url,
                        api_key=secret,
                        model=settings.llm_model,
                        messages=_plain_cli_draft_prompt(
                            requirement,
                            intent,
                            evidence,
                            topology_ports,
                            device_scope,
                            dialect,
                        ),
                        temperature=min(settings.llm_temperature, 0.2),
                        thinking=False,
                        stream=bool(on_event),
                        on_chunk=(
                            lambda thinking, formal: (
                                on_event("命令草案降级", "thinking", thinking)
                                if thinking and on_event
                                else None,
                                on_event("命令草案降级", "output", formal) if formal and on_event else None,
                            )
                        ),
                        cancel_event=cancel_event,
                    )
                )
                if draft_result.cancelled:
                    raise PlanningCancelled("用户已停止配置规划")
                plan = _plain_cli_draft_plan(
                    draft_result.content,
                    dialect,
                    topology_ports=topology_ports,
                )
                if not plan:
                    return None, {
                        "status": "fallback",
                        "node": "command_plan",
                        "reason": str(repair_error)[:240],
                        "initial_parse_error": str(initial_error)[:240],
                        "raw_response_excerpt": result.content[:2400],
                        "format_repair_excerpt": repaired_content[:2400],
                        "plain_draft_excerpt": draft_result.content[:2400],
                    }
                plan, unbound_cli = _bind_plain_cli_draft_to_evidence(
                    plan,
                    evidence=evidence,
                    topology_ports=topology_ports,
                    dialect=dialect,
                )
                return plan, {
                    "status": (
                        "evidence_bound_plain_cli_draft"
                        if not unbound_cli
                        else "unverified_plain_cli_draft"
                    ),
                    "node": "command_plan",
                    "model": settings.llm_model,
                    "reason": str(repair_error)[:240],
                    "initial_parse_error": str(initial_error)[:240],
                    "raw_response_excerpt": result.content[:2400],
                    "format_repair_excerpt": repaired_content[:2400],
                    "plain_draft_excerpt": draft_result.content[:2400],
                    "thinking_requested": False,
                    "thinking_used": False,
                    "thinking_fallback": True,
                    "thinking_fallback_reason": "结构化 JSON 两次失败，已请求仅供人工审阅的纯 CLI 草案。",
                    "unbound_cli": unbound_cli,
                    "format_repair_attempted": format_repair_attempted,
                    "format_repair_skipped_for_empty_formal": not format_repair_attempted,
                    "compact_retry_attempted": compact_retry_reason is not None,
                    "compact_retry_reason": compact_retry_reason,
                }
            format_repair_attempted = True
        # A syntactically valid JSON envelope can still contain only ``quit``
        # or use argument-only invocations for a generic capability.  The
        # latter has no deterministic renderer. Ask for a display-only CLI
        # draft once so the user never receives an empty command panel merely
        # because a weak model followed the JSON shape but omitted the work.
        if not is_huawei_vlan_renderer(intent, dialect) and not _has_business_cli(plan, dialect):
            draft_result = _run_async(
                request_text_result(
                    base_url=settings.llm_base_url,
                    api_key=secret,
                    model=settings.llm_model,
                    messages=_plain_cli_draft_prompt(
                        requirement, intent, evidence, topology_ports, device_scope, dialect
                    ),
                    temperature=min(settings.llm_temperature, 0.2),
                    thinking=False,
                    stream=bool(on_event),
                    on_chunk=(
                        lambda thinking, formal: (
                            on_event("命令草案降级", "thinking", thinking)
                            if thinking and on_event
                            else None,
                            on_event("命令草案降级", "output", formal) if formal and on_event else None,
                        )
                    ),
                    cancel_event=cancel_event,
                )
            )
            if draft_result.cancelled:
                raise PlanningCancelled("用户已停止配置规划")
            draft = _plain_cli_draft_plan(
                draft_result.content,
                dialect,
                topology_ports=topology_ports,
            )
            if draft:
                draft, unbound_cli = _bind_plain_cli_draft_to_evidence(
                    draft,
                    evidence=evidence,
                    topology_ports=topology_ports,
                    dialect=dialect,
                )
                return draft, {
                    "status": (
                        "evidence_bound_plain_cli_draft"
                        if not unbound_cli
                        else "unverified_plain_cli_draft"
                    ),
                    "node": "command_plan",
                    "model": settings.llm_model,
                    "reason": "结构化命令计划没有业务 CLI，已请求仅供人工审阅的纯 CLI 草案。",
                    "thinking_requested": False,
                    "thinking_used": False,
                    "thinking_fallback": True,
                    "thinking_fallback_reason": "结构化计划为空或仅含控制命令。",
                    "unbound_cli": unbound_cli,
                    "format_repair_attempted": format_repair_attempted,
                    "compact_retry_attempted": compact_retry_reason is not None,
                    "compact_retry_reason": compact_retry_reason,
                }
    except PlanningCancelled:
        raise
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
        "format_repair_attempted": format_repair_attempted,
        "compact_retry_attempted": compact_retry_reason is not None,
        "compact_retry_reason": compact_retry_reason,
    }


def _normalize_cli(value: str) -> str:
    return " ".join(value.strip().split())


def _plan_with_operations(
    plan: LlmCommandPlan,
    operations: list[dict[str, Any]],
    *,
    risk: str,
) -> LlmCommandPlan:
    """Return a plan with a narrowly corrected invocation list.

    The correction paths below only remove a CLI that is either an exact
    duplicate of a stated current-state fact or explicitly named by the
    independent reviewer.  They never invent commands or arguments, so the
    normal handbook compiler remains the authority for what is displayed.
    """

    payload = plan.model_dump(mode="json")
    payload["operations"] = operations
    payload["risks"] = [*payload.get("risks", []), risk]
    return LlmCommandPlan.model_validate(payload)


def _drop_empty_physical_view_blocks(
    invocations: list[dict[str, Any]], dialect: CliDialect
) -> list[dict[str, Any]]:
    """Remove an ``interface``/``quit`` block after all of its work was pruned."""

    retained: list[dict[str, Any]] = []
    index = 0
    while index < len(invocations):
        current = invocations[index]
        cli = _normalize_cli(str(current.get("cli") or ""))
        interface_name = _context_interface_name(cli)
        if not interface_name or not _physical_interface_name(interface_name):
            retained.append(current)
            index += 1
            continue
        end = index + 1
        while end < len(invocations):
            candidate = _normalize_cli(str(invocations[end].get("cli") or ""))
            if _context_interface_name(candidate):
                break
            end += 1
            if candidate.casefold() in dialect.control_commands:
                break
        block = invocations[index:end]
        business = [
            item
            for item in block[1:]
            if _normalize_cli(str(item.get("cli") or "")).casefold()
            not in dialect.control_commands
        ]
        if business:
            retained.extend(block)
        index = end
    return retained


def _remove_invocations(
    plan: LlmCommandPlan,
    remove: Callable[[str, str | None], bool],
    *,
    dialect: CliDialect,
    risk: str,
) -> tuple[LlmCommandPlan, list[str]]:
    """Apply a conservative removal predicate while preserving operation order."""

    operations: list[dict[str, Any]] = []
    removed: list[str] = []
    for raw_operation in plan.model_dump(mode="json").get("operations", []):
        current_port: str | None = None
        retained: list[dict[str, Any]] = []
        for raw_invocation in raw_operation.get("invocations", []):
            invocation = dict(raw_invocation)
            cli = _normalize_cli(str(invocation.get("cli") or ""))
            interface_name = _context_interface_name(cli)
            physical_port = _physical_interface_name(interface_name) if interface_name else None
            if physical_port:
                current_port = _topology_port_key(physical_port)
            if remove(cli, current_port):
                removed.append(cli)
                continue
            retained.append(invocation)
            if cli.casefold() in dialect.control_commands:
                current_port = None
        retained = _drop_empty_physical_view_blocks(retained, dialect)
        if retained:
            operation = dict(raw_operation)
            operation["invocations"] = retained
            operations.append(operation)
    if not removed:
        return plan, []
    return _plan_with_operations(plan, operations, risk=risk), list(dict.fromkeys(removed))


def prune_command_plan_for_known_facts(
    plan: LlmCommandPlan,
    *,
    intent: dict[str, Any],
    dialect: CliDialect,
) -> tuple[LlmCommandPlan, list[str]]:
    """Drop exact duplicate address writes for explicitly existing interfaces.

    This is deliberately not an idempotency renderer.  It only handles the
    unambiguous case where the operator has said that the exact address already
    exists on a drawn physical port, avoiding a low-quality fallback draft from
    changing current state while still retaining the requested route/protocol
    commands around it.
    """

    existing: dict[str, set[str]] = {}
    for fact in intent.get("existing_configuration_facts", []):
        if not isinstance(fact, dict) or fact.get("kind") != "existing_interface_address":
            continue
        port = _topology_port_key(str(fact.get("port") or ""))
        address = str(fact.get("address") or "").casefold()
        if port and address:
            existing.setdefault(port, set()).add(address)

    if not existing:
        return plan, []

    def duplicate_existing_address(cli: str, current_port: str | None) -> bool:
        if not current_port or current_port not in existing:
            return False
        tokens = cli.casefold().split()
        return any(address in tokens for address in existing[current_port])

    return _remove_invocations(
        plan,
        duplicate_existing_address,
        dialect=dialect,
        risk="已移除与用户明确既有接口地址完全重复的 LLM 草案命令。",
    )


def prune_command_plan_for_review_feedback(
    plan: LlmCommandPlan,
    *,
    review: dict[str, Any],
    dialect: CliDialect,
) -> tuple[LlmCommandPlan, list[str]]:
    """Remove only complete CLI strings explicitly called out by the reviewer.

    An LLM review remains advisory.  This helper trusts it only when it names a
    full quoted command already present in the current plan; broad prose such
    as "consider VLAN" cannot remove anything.  It therefore works across
    vendors and features without becoming a command-family allow-list.
    """

    quoted: set[str] = set()
    # ``required_changes`` can quote a CLI in a positive instruction such as
    # "保留 ...".  Only the review's problem statements are eligible for this
    # precise removal path; the normal LLM repair still receives all feedback.
    for value in list(review.get("issues") or []):
        for match in re.finditer(r"[\"'“]([^\"'”]{2,180})[\"'”]", str(value)):
            candidate = _normalize_cli(match.group(1)).casefold()
            if candidate and re.match(r"^[a-z][a-z0-9 ./_:-]*$", candidate):
                quoted.add(candidate)
    if not quoted:
        return plan, []
    return _remove_invocations(
        plan,
        lambda cli, _port: cli.casefold() in quoted,
        dialect=dialect,
        risk="已移除独立审阅明确点名、且未被需求授权的额外 CLI。",
    )


def _syntax_prefixes(item: dict[str, Any]) -> list[str]:
    """Derive conservative command prefixes from heterogeneous manual parsers."""

    prefixes: list[str] = []
    canonical = str(item.get("canonical_name") or "").split("（", 1)[0].split("(", 1)[0].strip()
    if canonical:
        prefixes.append(canonical)
    for syntax in item.get("syntax", []) or []:
        text = _normalize_cli(str(syntax))
        if not text:
            continue
        # CHM parsers sometimes retain a complete grammar and sometimes split it
        # into tokens.  The literal portion before the first placeholder is the
        # strongest common prefix available in both representations.
        literal = _literal_syntax_prefix(text)
        if literal:
            prefixes.append(literal)
    unique: list[str] = []
    for prefix in prefixes:
        normalized = _normalize_cli(prefix).casefold()
        if normalized and normalized not in unique:
            unique.append(normalized)
    return unique


def _starts_with_prefix(command: str, prefix: str) -> bool:
    return command == prefix or command.startswith(f"{prefix} ")


def _syntax_forms(item: dict[str, Any]) -> list[str]:
    """Return complete grammar forms, including tokenized CHM extractions."""

    parts = [_normalize_cli(str(value)) for value in item.get("syntax", []) or []]
    parts = [value for value in parts if value]
    forms = list(parts)
    # The CHM table parser often emits one grammar token per array element.
    # Joining those tokens is essential: treating its first token (for example
    # ``stp``) as a complete command would make ``stp mode mstp`` falsely bind
    # to the separate ``stp enable`` page.
    # Both braces and square brackets are emitted as standalone CHM table
    # cells.  A syntax such as ``vrrp vrid <id> [ virtual-ip <address> ]``
    # must therefore be reconstructed before matching its optional keyword.
    tokenized_grammar = ("{" in parts and "}" in parts) or ("[" in parts and "]" in parts)
    if len(parts) > 1 and (
        tokenized_grammar or sum(1 for value in parts if len(value.split()) == 1) >= len(parts) - 1
    ):
        # An extracted undo grammar normally starts a distinct syntax form.
        # Keeping it inside the positive form turns ``undo`` into a required
        # literal and makes the preceding configuration CLI impossible to
        # match.
        undo_index = next(
            (index for index, value in enumerate(parts) if value.casefold().startswith("undo ")),
            len(parts),
        )
        combined = " ".join(parts[:undo_index])
        # When punctuation was emitted as standalone array entries, the
        # combined grammar must be checked before fragments such as ``stp``.
        # Keep separately extracted undo forms available, but do not let the
        # first bare keyword become an unrestricted match.
        if tokenized_grammar:
            forms = [combined, *[value for value in parts if value.casefold().startswith("undo ")]]
        else:
            forms.insert(0, combined)
    return list(dict.fromkeys(forms))


def _syntax_requires_explicit_value(syntax: str) -> bool:
    """Identify a handbook grammar whose literal prefix is not executable.

    The parsers retain heterogeneous BNF-like forms.  We only need the small,
    format-independent distinction between a complete fixed command and a
    grammar that still contains an operator-provided value; this prevents a
    model fallback from displaying a bare command title as executable CLI.
    """

    normalized = _normalize_cli(syntax).casefold()
    if re.search(r"<[^>]+>", normalized):
        return True
    parameter_words = {
        "address",
        "mask",
        "prefix",
        "priority",
        "level",
        "number",
        "name",
        "value",
        "interface",
        "interface-name",
        "interface-type",
        "interface-number",
        "trunk-id",
        "vlan-id",
        "portnum",
        "ip-mask",
        "ip-address",
        "mask-length",
        "process-id",
        "member-id",
        "priority-value",
        "route-id",
        "wildcard-mask",
    }
    for token in re.findall(r"[a-z][a-z0-9-]*", normalized):
        if token in parameter_words or re.search(
            r"(?:-id|-address|-name|-number|-value|-mask|-time|-count|-index|-port)\d*$",
            token,
        ):
            return True
    return False


def prune_command_plan_for_incomplete_syntax(
    plan: LlmCommandPlan,
    *,
    evidence: list[dict[str, Any]],
    dialect: CliDialect,
) -> tuple[LlmCommandPlan, list[str]]:
    """Remove an invocation that is only a parameterised handbook title.

    This is derived exclusively from the selected manual.  It covers any
    vendor command whose grammar says the literal text requires an additional
    value, while leaving complete fixed commands such as ``stp enable`` alone.
    """

    incomplete_literals: set[str] = set()
    for item in evidence:
        for syntax in _syntax_forms(item):
            literal = _literal_syntax_prefix(syntax).casefold()
            if literal and _syntax_requires_explicit_value(syntax):
                incomplete_literals.add(literal)
    if not incomplete_literals:
        return plan, []
    return _remove_invocations(
        plan,
        lambda cli, _port: cli.casefold() in incomplete_literals,
        dialect=dialect,
        risk="已移除手册语法仍要求参数的裸命令标题。",
    )


def normalize_huawei_vlan_creation_plan(
    plan: LlmCommandPlan,
    *,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    dialect: CliDialect,
) -> tuple[LlmCommandPlan, list[str]]:
    """Prefer the selected Huawei manual's batch VLAN creation grammar.

    Small models sometimes emit ``vlan 30`` although the selected VRP command
    reference documents the requested creation action as ``vlan batch 30``.
    The rewrite happens only when the model already supplied that exact VLAN ID
    and the current manual proves the batch form.  It is a dialect syntax
    normalisation, not a feature decision or a generated business parameter.
    """

    if dialect.key != "huawei_vrp":
        return plan, []
    vlan_ids = {str(value) for value in intent.get("vlan_ids", [])}
    if not vlan_ids:
        return plan, []
    batch_evidence = next(
        (
            item
            for item in evidence
            if str(item.get("canonical_name") or "").casefold().startswith("vlan batch")
            and any(
                _matches_evidence_syntax(f"vlan batch {vlan_id}", item)
                for vlan_id in vlan_ids
            )
        ),
        None,
    )
    if not batch_evidence:
        return plan, []
    payload = plan.model_dump(mode="json")
    replacements: list[str] = []
    for operation in payload.get("operations", []):
        for invocation in operation.get("invocations", []):
            cli = _normalize_cli(str(invocation.get("cli") or ""))
            matched = re.fullmatch(r"vlan\s+(\d{1,4})", cli, re.IGNORECASE)
            if not matched or matched.group(1) not in vlan_ids:
                continue
            normalized = f"vlan batch {matched.group(1)}"
            if not _matches_evidence_syntax(normalized, batch_evidence):
                continue
            invocation["cli"] = normalized
            invocation["command_id"] = str(batch_evidence.get("command_id") or "__unverified_draft__")
            invocation["syntax_index"] = 0
            invocation["target_port_ref"] = None
            replacements.append(normalized)
    if not replacements:
        return plan, []
    payload["risks"] = [
        *payload.get("risks", []),
        "已按所选华为手册将模型给出的 VLAN 创建动作规范为 vlan batch 语法。",
    ]
    return LlmCommandPlan.model_validate(payload), list(dict.fromkeys(replacements))


def complete_command_plan_from_review(
    plan: LlmCommandPlan,
    *,
    review: dict[str, Any],
    evidence: list[dict[str, Any]],
    dialect: CliDialect,
) -> tuple[LlmCommandPlan, list[str]]:
    """Add a reviewer-required CLI only when the manual independently proves it.

    This is the inverse of the precise removal path: the reviewer may identify
    a missing command after a weak planner omitted it.  A line is eligible only
    when the reviewer quotes the full CLI in ``required_changes`` and the
    selected manual binds that exact syntax.  Commands with an evident interface
    view are inserted immediately after the matching existing interface entry;
    other documented commands are appended as global operations.  No argument
    is inferred by this function.
    """

    required: list[str] = []
    positive_action = re.compile(r"添加|新增|补充|补全|配置|执行|设置|创建|启用|加入|保留|使用")
    for value in list(review.get("required_changes") or []):
        change = str(value)
        # A reviewer often quotes an unwanted CLI while asking to delete it,
        # for example "删除 'port link-type trunk'".  Quoting alone is not
        # evidence of a missing command.  Treat mixed or deletion-oriented
        # instructions conservatively: the precise prune path handles the
        # named removal and this completion path must never add it back.
        if re.search(r"删除|移除|去除|取消|禁止|不得|不应|仅保留|只保留", change):
            continue
        for match in re.finditer(r"[\"'“]([^\"'”]{2,180})[\"'”]", change):
            cli = _normalize_cli(match.group(1))
            if cli and re.match(r"^[A-Za-z][A-Za-z0-9 ./_:-]*$", cli):
                required.append(cli)
        if not positive_action.search(change):
            continue
        # Smaller models sometimes describe the exact missing CLI without
        # quotation marks (for example, "执行 stack member 1 priority 160").
        # Recover only literal ASCII runs that begin with an already retrieved
        # handbook command title and satisfy that title's full syntax.  This
        # does not infer an argument from prose or permit a new command family.
        for item in evidence:
            canonical = _normalize_cli(str(item.get("canonical_name") or ""))
            if not canonical:
                continue
            pattern = re.compile(
                rf"{re.escape(canonical)}(?:\s+[A-Za-z0-9./_:-]+){{0,12}}",
                re.IGNORECASE,
            )
            for match in pattern.finditer(change):
                cli = _normalize_cli(match.group(0).rstrip(".,;:"))
                if cli and _matches_evidence_syntax(cli, item):
                    required.append(cli)
    if not required:
        return plan, []

    payload = plan.model_dump(mode="json")
    present = {
        _normalize_cli(str(invocation.get("cli") or "")).casefold()
        for operation in payload.get("operations", [])
        for invocation in operation.get("invocations", [])
    }
    additions: list[tuple[str, dict[str, Any]]] = []
    for cli in required:
        lowered = cli.casefold()
        if lowered in present or lowered in dialect.control_commands:
            continue
        item = _resolve_evidence_binding(cli, evidence, current_physical_port=None, prior_commands=[])
        if not item or not _matches_evidence_syntax(cli, item):
            continue
        additions.append((cli, item))
        present.add(lowered)
    if not additions:
        return plan, []

    global_invocations: list[dict[str, Any]] = []
    inserted: list[str] = []
    for cli, item in additions:
        view_material = " ".join(str(value) for value in item.get("views", [])).casefold()
        target_operation: dict[str, Any] | None = None
        target_index: int | None = None
        if view_material:
            for operation in payload.get("operations", []):
                for index, invocation in enumerate(operation.get("invocations", [])):
                    interface_name = _context_interface_name(
                        _normalize_cli(str(invocation.get("cli") or ""))
                    )
                    if not interface_name:
                        continue
                    interface_tokens = [
                        token for token in re.findall(r"[a-z][a-z0-9-]*", interface_name.casefold())
                        if token not in {"interface"}
                    ]
                    if interface_tokens and any(token in view_material for token in interface_tokens):
                        target_operation = operation
                        target_index = index
                        break
                if target_operation is not None:
                    break
        invocation = {
            "command_id": str(item.get("command_id") or "__unverified_draft__"),
            "syntax_index": 0,
            "arguments": {},
            "target_port_ref": None,
            "cli": cli,
        }
        if target_operation is not None and target_index is not None:
            target_operation["invocations"].insert(target_index + 1, invocation)
        else:
            global_invocations.append(invocation)
        inserted.append(cli)
    if global_invocations:
        payload.setdefault("operations", []).append(
            {"purpose": "补全独立审阅明确要求且手册已证明的命令", "invocations": global_invocations}
        )
    payload["risks"] = [
        *payload.get("risks", []),
        "已补全独立审阅明确要求、且与当前手册精确匹配的命令。",
    ]
    return LlmCommandPlan.model_validate(payload), inserted


def _has_fixed_enum_conflict(command: str, syntax: str) -> bool:
    """Reject a CLI that contradicts a literal enumeration in a grammar.

    This is deliberately small and format-neutral.  It recognises the common
    ``keyword { choice-a | choice-b }`` form without attempting to parse a
    vendor's complete BNF.  Variable alternatives such as ``vlan-id`` are
    ignored, so a heterogeneous handbook can still use normal arguments.
    """

    command_tokens = _normalize_cli(command).casefold().split()
    normalized = _normalize_cli(syntax).casefold()
    for match in re.finditer(r"\{\s*([^{}]+?)\s*\}", normalized):
        before = re.sub(r"[\[\]{}|]", " ", normalized[: match.start()])
        prefix_tokens = [token for token in before.split() if token]
        if not prefix_tokens or command_tokens[: len(prefix_tokens)] != prefix_tokens:
            continue
        alternatives = [
            [token for token in re.sub(r"[\[\]{}]", " ", option).split() if token]
            for option in match.group(1).split("|")
        ]
        alternatives = [option for option in alternatives if option]
        if not alternatives:
            continue
        # Hyphenated grammar labels and conventional placeholders are values,
        # not fixed choices.  Only validate fully literal alternatives.
        variable_markers = ("-id", "address", "mask", "number", "name", "value", "portnum")
        literal_alternatives = [
            option
            for option in alternatives
            if not any(any(marker in token for marker in variable_markers) for token in option)
        ]
        if literal_alternatives and not any(
            command_tokens[len(prefix_tokens) : len(prefix_tokens) + len(option)] == option
            for option in literal_alternatives
        ):
            return True
    return False


def _has_fixed_suffix_conflict(command: str, evidence_item: dict[str, Any]) -> bool:
    """Reject a value outside fixed syntax rows with a shared command title.

    Some command-reference pages store alternatives as independent complete
    rows instead of a ``{ a | b }`` grammar.  For example, the ``port
    link-type`` page has one row each for ``access``, ``hybrid`` and ``trunk``.
    Treating ``link-type`` as a placeholder lets an unrelated value such as
    ``stack`` bind to that page.  This check only constrains rows whose suffix
    is fully literal; any row containing ordinary parameter labels remains
    available to the general grammar matcher.
    """

    canonical = _normalize_cli(
        str(evidence_item.get("canonical_name") or "").split("（", 1)[0].split("(", 1)[0]
    ).casefold()
    canonical_tokens = canonical.split()
    command_tokens = _normalize_cli(command).casefold().split()
    if not canonical_tokens or command_tokens[: len(canonical_tokens)] != canonical_tokens:
        return False

    placeholder_markers = (
        "-id",
        "-address",
        "-name",
        "-number",
        "-value",
        "-mask",
        "-time",
        "-count",
        "-index",
        "-port",
        "-slot",
        "-instance",
    )
    generic_placeholders = {
        "address",
        "mask",
        "prefix",
        "priority",
        "level",
        "number",
        "name",
        "value",
        "interface",
        "interface-name",
        "interface-type",
        "interface-number",
        "trunk-id",
        "vlan-id",
        "portnum",
    }
    literal_suffixes: list[list[str]] = []
    for syntax in _syntax_forms(evidence_item):
        normalized = _normalize_cli(syntax).casefold()
        syntax_tokens = normalized.split()
        if (
            not syntax_tokens
            or syntax_tokens[: len(canonical_tokens)] != canonical_tokens
            or any(marker in normalized for marker in ("{", "}", "[", "]", "|", "<", ">"))
        ):
            continue
        suffix = syntax_tokens[len(canonical_tokens) :]
        if not suffix:
            continue
        if any(
            token in generic_placeholders or token.endswith(placeholder_markers)
            for token in suffix
        ):
            continue
        literal_suffixes.append(suffix)

    if not literal_suffixes:
        return False
    command_suffix = command_tokens[len(canonical_tokens) :]
    return not any(command_suffix == suffix for suffix in literal_suffixes)


def _has_interface_argument_conflict(command: str, evidence_item: dict[str, Any]) -> bool:
    """Keep a broad ``port`` grammar from authorising an unrelated subcommand.

    Command references can have a one-word title such as ``port`` for a
    feature-specific grammar whose first argument is an interface name.  That
    page must not validate ``port link-type ...`` merely because its title is
    a prefix.  This rule is grammar-driven and accepts the normal cross-vendor
    interface shapes (a combined name or a type plus number); it does not make
    assumptions about a particular switch family.
    """

    canonical = _normalize_cli(
        str(evidence_item.get("canonical_name") or "").split("（", 1)[0].split("(", 1)[0]
    ).casefold()
    canonical_tokens = canonical.split()
    command_tokens = _normalize_cli(command).casefold().split()
    if len(canonical_tokens) != 1 or command_tokens[:1] != canonical_tokens or len(command_tokens) < 2:
        return False

    expects_interface_argument = False
    for syntax in _syntax_forms(evidence_item):
        normalized = _normalize_cli(syntax).casefold()
        if not normalized.startswith(f"{canonical} {{"):
            continue
        first_group = re.search(r"^\S+\s+\{\s*([^{}]+?)\s*\}", normalized)
        if first_group and any(
            item in first_group.group(1)
            for item in ("interface-name", "interface-type", "ifname", "if-type")
        ):
            expects_interface_argument = True
            break
    if not expects_interface_argument:
        return False
    # Interface names are normally an integrated identifier (GE0/0/1,
    # Vlanif10, Ethernet1/0/1) or a two-token type/number expression.
    candidate = " ".join(command_tokens[1:3])
    return not bool(re.search(r"\d|/", candidate))


def _has_numeric_argument_shape_conflict(command: str, syntax: str) -> bool:
    """Reject a word where a tokenized manual grammar requires a numeric value.

    Some CHM tables split ``port port-number [ all ]`` across cells.  Its
    literal root is only ``port``; without this shape check a generated
    ``port link-type stack`` can incorrectly bind to that unrelated page.
    This is a grammar-level guard for conventional numeric placeholders, not
    a command-family or vendor capability rule.
    """

    literal = _literal_syntax_prefix(syntax)
    literal_tokens = literal.casefold().split()
    command_tokens = _normalize_cli(command).casefold().split()
    if not literal_tokens or command_tokens[: len(literal_tokens)] != literal_tokens:
        return False
    if len(command_tokens) <= len(literal_tokens):
        return False
    remainder = _normalize_cli(syntax)[len(literal) :].lstrip()
    if not remainder or remainder.startswith(("{", "[")):
        return False
    first_token = remainder.split()[0].strip("<>()[]{}*.,;:").casefold()
    # ``interface-id`` and ``route-id`` may be an interface identifier or an
    # IPv4 address. Only these conventional *numeric* placeholders are
    # constrained here; the full handbook grammar remains authoritative for
    # every other argument type.
    numeric_markers = {
        "number",
        "portnum",
        "port-number",
        "process-id",
        "vlan-id",
        "member-id",
        "priority-value",
        "virtual-router-id",
        "trunk-id",
    }
    if first_token not in numeric_markers:
        return False
    # A numeric argument must be a numeric token, not merely a word that
    # happens to contain a digit (``ipv4`` must not satisfy ``process-id``).
    return not bool(re.fullmatch(r"\d+", command_tokens[len(literal_tokens)]))


def _literal_syntax_prefix(syntax: str) -> str:
    """Return the fixed leading words of a manual grammar.

    Different handbook extractors emit parameter placeholders as ordinary
    tokens. Treating ``trunk-id`` in ``eth-trunk trunk-id`` as a literal would
    reject the documented CLI ``eth-trunk 1``. Command words stay fixed while
    conventional placeholder shapes end the fixed prefix.
    """

    leading = _normalize_cli(syntax).split("{", 1)[0].split("[", 1)[0].strip()
    tokens = leading.split()
    placeholder_markers = (
        "-id",
        "-address",
        "-name",
        "-number",
        "-value",
        "-mask",
        "-time",
        "-count",
        "-index",
        "-type",
        "-port",
        "-slot",
        "-instance",
    )
    generic_placeholders = {
        "address",
        "mask",
        "prefix",
        "priority",
        "level",
        "number",
        "name",
        "value",
        "interface",
        "interface-name",
        "interface-type",
        "interface-number",
        "trunk-id",
        "vlan-id",
        "portnum",
    }
    fixed: list[str] = []
    for token in tokens:
        normalized = token.strip("<>()[]{}*.,;:").casefold()
        # ``interface`` and ``ip address`` are command words when they occur
        # at the beginning of a CLI grammar, despite also being common
        # placeholder labels elsewhere in a manual.
        is_leading_command_word = not fixed and (
            normalized == "interface" or ("-" in normalized and len(normalized) > 2)
        )
        is_ip_address_command_word = fixed == ["ip"] and normalized == "address"
        is_placeholder = (
            normalized in generic_placeholders or normalized.endswith(placeholder_markers)
        ) and not is_leading_command_word and not is_ip_address_command_word
        if is_placeholder or normalized.startswith(("<", "$")):
            break
        fixed.append(token)
    return " ".join(fixed).rstrip("*").strip()


def _example_action_conflict(command: str, item: dict[str, Any]) -> bool:
    """Detect mutually exclusive sub-actions demonstrated by manual examples.

    Some CHM pages lose grouping punctuation during extraction.  Their examples
    still make the valid one-action variants explicit, as with ``stack member
    <id> renumber ...`` versus ``stack member <id> priority ...``.  When a
    generated CLI combines two such alternatives, reject it rather than
    accepting a syntactically plausible but operationally wrong command.
    """

    root = _normalize_cli(str(item.get("canonical_name") or "")).split("（", 1)[0].split("(", 1)[0]
    root_tokens = root.casefold().split()
    command_tokens = _normalize_cli(command).casefold().split()
    if not root_tokens or command_tokens[: len(root_tokens)] != root_tokens:
        return False
    variants: set[str] = set()
    for example in item.get("examples", []) or []:
        for line in str(example).splitlines():
            normalized = _normalize_cli(line).casefold()
            position = normalized.find(" ".join(root_tokens))
            if position < 0:
                continue
            tail = normalized[position + len(" ".join(root_tokens)) :].strip().split()
            # Example prompts can prefix the command, but the command's first
            # numeric argument is normally followed by the action selector.
            for token in tail:
                if re.fullmatch(r"\d+(?:\.\d+){0,3}|\d+(?:/\d+)*", token):
                    continue
                if token not in {"undo", "all"}:
                    variants.add(token)
                    break
    if len(variants) < 2:
        return False
    present = [token for token in command_tokens[len(root_tokens) :] if token in variants]
    return len(set(present)) > 1


def _matches_evidence_syntax(command: str, evidence_item: dict[str, Any]) -> bool:
    """Accept a handbook command when its literal keyword skeleton matches.

    Manual grammars commonly place variable parameters between keywords.  For
    example, the canonical command ``vrrp vrid priority`` has the valid CLI
    ``vrrp vrid 10 priority 120``.  A simple textual prefix comparison rejects
    that valid construction because the VRID occupies the middle of the
    command.  Keep exact literal-prefix matching as the first choice, then use
    the indexed canonical command words as an evidence-bound fallback.
    """

    lowered = command.casefold()
    if _example_action_conflict(command, evidence_item):
        return False
    if _has_fixed_suffix_conflict(command, evidence_item):
        return False
    if _has_interface_argument_conflict(command, evidence_item):
        return False
    canonical = str(evidence_item.get("canonical_name") or "")
    canonical = canonical.split("（", 1)[0].split("(", 1)[0].strip()
    keywords = [token.casefold() for token in canonical.split() if token]
    # Command references usually title an undo form as its positive command
    # (``portswitch``) while listing ``undo portswitch`` in the grammar. Use
    # the positive root only for title disambiguation; the loop below still
    # requires the original undo syntax to match exactly.
    canonical_command = lowered.removeprefix("undo ").strip() if lowered.startswith("undo ") else lowered
    command_tokens = canonical_command.split()
    if not keywords or not command_tokens or keywords[0] != command_tokens[0]:
        return False
    # A full grammar form has priority over a bare title/token. It keeps enum
    # values evidence-bound when CHM extraction represents one syntax token per
    # JSON element.
    syntax_forms = _syntax_forms(evidence_item)
    needs_canonical_disambiguation = False
    syntax_shape_conflict = False
    for syntax in syntax_forms:
        if _has_fixed_enum_conflict(command, syntax):
            continue
        if _has_numeric_argument_shape_conflict(command, syntax):
            syntax_shape_conflict = True
            continue
        normalized_syntax = _normalize_cli(syntax).casefold()
        literal = _literal_syntax_prefix(syntax).casefold()
        if literal and _starts_with_prefix(lowered, literal):
            # A bare extracted root (for example ``stack``) documents only
            # that exact command.  It must not authorize an arbitrary longer
            # command such as ``stack priority`` merely because both begin
            # with the same word.  Grammars with an explicit parameter or
            # sub-command still retain normal prefix matching below.
            if normalized_syntax == literal and lowered != literal:
                if len(keywords) > len(literal.split()):
                    needs_canonical_disambiguation = True
                continue
            # Extractors frequently stop a grammar at its first parameter,
            # leaving only a broad root such as ``stp`` for pages titled
            # ``stp bridge-address`` and ``stp mode``.  Let the complete
            # handbook title distinguish those siblings before accepting the
            # common root. A single-word title (``interface``, ``ospf`` or
            # ``stack-port``) remains a valid parameterised root.
            # A short extracted grammar is not enough to bypass the rest of a
            # more specific page title.  ``vrrp vrid`` is the shared prefix of
            # pages such as ``vrrp vrid priority``; accepting it before the
            # title check makes all siblings appear equally valid and prevents
            # local syntax recovery from selecting the correct handbook page.
            if len(literal.split()) >= len(keywords) or len(keywords) <= 1:
                return True
            needs_canonical_disambiguation = True
    # When a parser has retained a grammar, it is stronger evidence than the
    # page title.  Falling back to a short canonical title after every grammar
    # rejected the CLI made broad root pages silently bless unrelated actions.
    # Canonical-title matching remains available only for pages whose grammar
    # was genuinely not extracted, or where a shared one-word grammar root
    # needs the title to select among sibling command pages.
    if syntax_shape_conflict:
        return False
    if syntax_forms and not needs_canonical_disambiguation:
        return False
    position = 0
    for keyword in keywords[1:]:
        try:
            position = command_tokens.index(keyword, position + 1)
        except ValueError:
            return False
    return not any(_has_fixed_enum_conflict(command, syntax) for syntax in _syntax_forms(evidence_item))


def _resolve_evidence_binding(
    command: str,
    evidence: list[dict[str, Any]],
    *,
    current_physical_port: str | None,
    prior_commands: list[str],
) -> dict[str, Any] | None:
    """Recover a wrong LLM command ID only when manual syntax identifies one page.

    The model still writes the CLI. This resolver merely binds that CLI to the
    imported manual page after a weak model confuses opaque IDs. Ambiguous pages
    remain blocked rather than guessed.
    """

    candidates = [item for item in evidence if _matches_evidence_syntax(command, item)]
    if not candidates:
        return None
    lowered = command.casefold()
    if current_physical_port:
        port_family = re.match(r"[a-z]+", current_physical_port.casefold())
        if port_family:
            compatible = [
                item
                for item in candidates
                if port_family.group(0)
                in " ".join(str(value) for value in item.get("views", [])).casefold()
            ]
            if compatible:
                candidates = compatible
    if any(item.casefold().startswith("ospf ") for item in prior_commands):
        ospf_context = [
            item
            for item in candidates
            if re.search(
                r"\bospf\b",
                " ".join(
                    [
                        str(item.get("canonical_name") or ""),
                        " ".join(str(value) for value in item.get("views", [])),
                    ]
                ).casefold(),
            )
        ]
        if ospf_context:
            candidates = ospf_context
    def canonical_prefix(item: dict[str, Any]) -> str:
        canonical = _normalize_cli(str(item.get("canonical_name") or ""))
        return canonical.split("（", 1)[0].split("(", 1)[0].casefold()

    def canonical_keywords_match(value: str, canonical: str) -> bool:
        """Match a handbook title across positional parameters in a CLI.

        Titles such as ``vrrp vrid priority`` omit the numeric VRID argument,
        so they are not a contiguous string prefix of the real command.  The
        ordered keyword sequence is still a stronger provenance signal than a
        shorter parent title such as ``vrrp vrid``.
        """

        tokens = value.split()
        keywords = canonical.split()
        if not tokens or not keywords or tokens[0] != keywords[0]:
            return False
        position = 0
        for keyword in keywords[1:]:
            try:
                position = tokens.index(keyword, position + 1)
            except ValueError:
                return False
        return True

    # ``undo <command>`` often appears on the same handbook page as the
    # positive form titled simply ``<command>`` (for example ``portswitch``).
    # Compare both forms against the canonical title while still requiring the
    # complete grammar match above, so an undo command cannot bind to an
    # unrelated broad page.
    canonical_command = lowered.removeprefix("undo ").strip() if lowered.startswith("undo ") else lowered
    exact_canonical = [
        item
        for item in candidates
        if canonical_keywords_match(lowered, canonical_prefix(item))
        or canonical_keywords_match(canonical_command, canonical_prefix(item))
    ]
    if exact_canonical:
        # A command reference frequently contains a broad root page (``stp``)
        # and a more specific page (``stp enable``). Both prefix-match the
        # generated CLI, but the longest exact canonical prefix is the
        # unambiguous provenance binding. This remains valid for any manual
        # that organises command families in the same way.
        ranked_exact = sorted(
            exact_canonical,
            key=lambda item: (-len(canonical_prefix(item).split()), canonical_prefix(item)),
        )
        # A broad root such as ``stp`` can coexist with two view-specific
        # pages titled ``stp mode``.  The longest canonical prefix is the
        # strongest provenance signal; shorter roots must not make an
        # otherwise valid command appear ambiguous.
        longest = len(canonical_prefix(ranked_exact[0]).split())
        longest_matches = [
            item
            for item in ranked_exact
            if len(canonical_prefix(item).split()) == longest
        ]
        if len(longest_matches) == 1:
            return longest_matches[0]
        if len({canonical_prefix(item) for item in longest_matches}) == 1:
            return longest_matches[0]
    return candidates[0] if len(candidates) == 1 else None


def _normalize_cli_from_evidence_suffix(
    command: str,
    evidence: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    """Remove a uniquely provable, stray leading token from weak-model CLI.

    This is intentionally narrower than a generic rewrite rule.  The retained
    suffix must itself match one exact handbook grammar and the removed token
    must occur in that same command page's syntax/parameters/preconditions.
    It therefore repairs a word-order slip such as ``lacp mode lacp-static``
    only when the imported manual itself proves ``mode lacp-static``.
    """

    words = _normalize_cli(command).split()
    if len(words) < 3:
        return command, None
    protected_prefixes = {"undo", "no", "not", "delete", "clear", "reset", "disable"}
    if words[0].casefold() in protected_prefixes:
        return command, None
    for offset in range(1, min(3, len(words) - 1)):
        candidate_cli = " ".join(words[offset:])
        matches = [item for item in evidence if _matches_evidence_syntax(candidate_cli, item)]
        resolved = _resolve_evidence_binding(
            candidate_cli,
            matches,
            current_physical_port=None,
            prior_commands=[],
        )
        if not resolved:
            continue
        removed = {word.casefold() for word in words[:offset]}
        material = " ".join(
            [
                str(resolved.get("canonical_name") or ""),
                *[str(value) for value in resolved.get("syntax", []) or []],
                *[str(value) for value in resolved.get("parameters", []) or []],
                *[str(value) for value in resolved.get("preconditions", []) or []],
            ]
        ).casefold()
        material_tokens = set(re.findall(r"[a-z][a-z0-9-]*", material))
        if removed.issubset(material_tokens):
            return candidate_cli, resolved
    return command, None


def _virtual_interface_example_error(interface_name: str, evidence: list[dict[str, Any]]) -> str | None:
    """Validate a virtual-interface spelling when the selected manual shows it.

    The generic ``interface`` grammar intentionally accepts a broad
    ``interface-name`` placeholder.  Some manuals narrow that placeholder in
    feature examples, such as ``interface stack-port 1``.  Reuse those examples
    only when their virtual interface type matches the generated command; this
    keeps physical vendor port names and unknown manual formats unrestricted.
    """

    current = re.fullmatch(r"([a-z][a-z0-9-]*)\s+(\d+(?:/\d+)*)", interface_name.casefold())
    if not current:
        return None
    interface_type, identifier = current.groups()
    if _physical_interface_name(interface_name):
        return None
    allowed_identifier_shapes: set[str] = set()
    for item in evidence:
        for example in item.get("examples", []) or []:
            normalized = " ".join(str(example).split()).casefold()
            for matched in re.finditer(r"\binterface\s+([a-z][a-z0-9-]*)\s+(\d+(?:/\d+)*)\b", normalized):
                example_type, example_identifier = matched.groups()
                if example_type != interface_type:
                    continue
                allowed_identifier_shapes.add(
                    "slash" if "/" in example_identifier else "integer"
                )
    if not allowed_identifier_shapes:
        return None
    current_shape = "slash" if "/" in identifier else "integer"
    if current_shape in allowed_identifier_shapes:
        return None
    allowed = "、".join(sorted(allowed_identifier_shapes))
    return (
        f"虚拟接口 {interface_name} 的标识形式不符合已选手册示例；"
        f"手册示例只出现 {interface_type} 的 {allowed} 编号形式。"
    )


def _physical_interface_name(value: str) -> str | None:
    """Return a topology-comparable physical port, keeping virtual views open."""

    compact = value.strip()
    lowered = compact.casefold()
    if lowered.startswith(
        (
            "ge",
            "gigabitethernet",
            "xge",
            "10ge",
            "25ge",
            "40ge",
            "100ge",
            "multige",
            "gi",
            "tengigabitethernet",
            "te",
            "fastethernet",
            "fa",
            "ethernet",
            "xe-",
            "et-",
        )
    ):
        return compact
    return None


def _topology_port_key(value: str) -> str:
    """Normalize only well-known long/short aliases used in topology comparisons."""

    normalized = port_identity(value)
    if normalized.startswith("GI"):
        return f"GE{normalized[2:]}"
    if normalized.startswith("TE"):
        return f"XGE{normalized[2:]}"
    return normalized


def _cli_mentions_port(cli: str, port: str) -> bool:
    """Compare a topology port with its rendered CLI without rewriting either."""

    def aliases(value: str) -> set[str]:
        compact = re.sub(r"\s+", "", value).casefold()
        result = {compact, _topology_port_key(value).casefold()}
        for long_name, short_names in (
            ("gigabitethernet", ("ge", "gi")),
            ("tengigabitethernet", ("xge", "te")),
            ("hundredgigabitethernet", ("100ge",)),
            ("fortygigabitethernet", ("40ge",)),
        ):
            result.update(compact.replace(long_name, short_name) for short_name in short_names)
        return result

    cli_aliases = aliases(cli)
    return bool(cli_aliases & aliases(port)) or any(
        port_alias in cli_alias
        for port_alias in aliases(port)
        if "/" in port_alias
        for cli_alias in cli_aliases
    )


def _preserves_port_spelling(rendered: str, topology_port: str) -> bool:
    """Keep a user-entered physical port family instead of expanding aliases."""

    return re.sub(r"\s+", "", rendered).casefold() == re.sub(r"\s+", "", topology_port).casefold()


def _context_interface_name(cli: str) -> str | None:
    """Extract common physical context forms without assuming a single vendor."""

    direct = re.fullmatch(r"interface\s+(.+)", cli, re.IGNORECASE)
    if direct:
        return direct.group(1).strip()
    set_style = re.match(r"set\s+interfaces?\s+([^\s]+)", cli, re.IGNORECASE)
    return set_style.group(1).strip() if set_style else None


def _append_exact_global_followup_commands(
    commands: list[str],
    evidence_ids: list[str],
    resolved_evidence_bindings: list[dict[str, str]],
    *,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    dialect: CliDialect,
) -> None:
    """Recover an omitted global command explicitly named by active retrieval.

    The retrieval ReAct node can say that an exact command page, such as
    ``stp enable``, is still needed.  A small command model may nevertheless
    omit that zero-argument global action.  Reuse it only when the query itself
    is a complete, evidence-matching CLI and its handbook view is global.
    This avoids inventing arguments, guessing ports, or injecting an
    interface-view command after the model's sequence.
    """

    raw_terms = intent.get("retrieval_followup_terms", [])
    if not isinstance(raw_terms, list):
        return
    present = {_normalize_cli(item).casefold() for item in commands}
    for raw_term in raw_terms:
        if not isinstance(raw_term, str):
            continue
        cli = _normalize_huawei_logical_interface(_normalize_cli(raw_term), dialect)
        lowered = cli.casefold()
        if (
            not cli
            or cli in commands
            or lowered in present
            or _context_interface_name(cli)
            or lowered in dialect.control_commands
            or lowered.startswith(dialect.read_only_prefixes)
            or any(_starts_with_prefix(lowered, prefix) for prefix in FORBIDDEN_PLAN_PREFIXES)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 ./_:-]*", cli)
        ):
            continue
        matches = [item for item in evidence if _matches_evidence_syntax(cli, item)]

        def syntax_has_variable_placeholder(syntax: str) -> bool:
            """Return whether a handbook grammar still needs a user value.

            Active retrieval terms are queries, never argument sources.  This
            deliberately recognises the broad placeholder spellings emitted by
            CHM/PDF/HTML extractors.  It is only used for automatic follow-up
            insertion, so a false positive merely skips an optional recovery;
            it never removes a CLI explicitly generated by the model.
            """

            normalized = _normalize_cli(syntax).casefold()
            placeholder_markers = (
                "-id",
                "-address",
                "-name",
                "-number",
                "-value",
                "-mask",
                "-time",
                "-count",
                "-index",
                "-type",
                "-port",
                "-slot",
                "-instance",
            )
            generic_placeholders = {
                "address",
                "mask",
                "prefix",
                "priority",
                "level",
                "number",
                "name",
                "value",
                "interface",
                "interface-name",
                "interface-type",
                "interface-number",
                "trunk-id",
                "vlan-id",
                "portnum",
                "ip-mask",
                "ip-address",
                "mask-length",
                "process-id",
                "member-id",
                "priority-value",
                "route-id",
                "wildcard-mask",
            }
            for token in re.findall(r"[a-z][a-z0-9-]*", normalized):
                if token in generic_placeholders or token.endswith(placeholder_markers):
                    return True
            return False

        def is_complete_handbook_form(item: dict[str, Any]) -> bool:
            """Require a complete handbook form for retrieval-only injection.

            A ReAct follow-up is a search term, not an authority to create a
            command.  In particular, a broad page named ``stack`` must not
            turn a query for ``stack priority`` into an executable line.  The
            ordinary planner can still use parameterised commands; this extra
            restriction applies only to automatic global additions.
            """

            for syntax in _syntax_forms(item):
                normalized = _normalize_cli(syntax).casefold()
                # Never turn a search title such as ``ip address`` or
                # ``vlan batch`` into a CLI when the grammar still requires
                # an address, mask, VLAN ID, port number, or similar value.
                # The planner must provide those arguments itself.
                if _syntax_requires_explicit_value(normalized):
                    continue
                if normalized == lowered:
                    return True
                literal = _literal_syntax_prefix(syntax).casefold()
                # A complete fixed prefix can still be followed by a documented
                # value (for example ``stp mode mstp``).  A one-word root is
                # deliberately insufficient here.
                if len(literal.split()) >= 2 and _starts_with_prefix(lowered, literal):
                    return True
                # A fully literal enumeration such as ``stp { enable |
                # disable }`` documents an entire short command even though
                # its leading literal prefix has only one token.  It is safe
                # to inject only when the generated line is exactly that
                # prefix plus one documented alternative; parameterised forms
                # such as ``ip address ip-address mask`` do not qualify.
                for match in re.finditer(r"\{\s*([^{}]+?)\s*\}", normalized):
                    before = re.sub(r"[\[\]{}|]", " ", normalized[: match.start()])
                    prefix_tokens = [token for token in before.split() if token]
                    if not prefix_tokens:
                        continue
                    for option in match.group(1).split("|"):
                        option_tokens = [
                            token for token in re.sub(r"[\[\]{}]", " ", option).split() if token
                        ]
                        variable_markers = ("-id", "address", "mask", "number", "name", "value", "portnum")
                        if not option_tokens or any(
                            marker in token for token in option_tokens for marker in variable_markers
                        ):
                            continue
                        if lowered.split() == [*prefix_tokens, *option_tokens]:
                            return True
            return False

        matches = [item for item in matches if is_complete_handbook_form(item)]
        global_matches = [
            item
            for item in matches
            if "接口视图" not in " ".join(str(view) for view in item.get("views", [])).casefold()
            and "interface view" not in " ".join(str(view) for view in item.get("views", [])).casefold()
        ]
        resolved = _resolve_evidence_binding(
            cli,
            global_matches,
            current_physical_port=None,
            prior_commands=commands,
        )
        if not resolved:
            continue
        commands.append(cli)
        present.add(lowered)
        command_id = str(resolved.get("command_id") or "")
        if command_id:
            evidence_ids.append(command_id)
            resolved_evidence_bindings.append(
                {
                    "cli": cli,
                    "provided_command_id": "__retrieval_followup__",
                    "resolved_command_id": command_id,
                }
            )


def _compile_generic_command_plan(
    plan: LlmCommandPlan,
    *,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    device_scope: dict[str, Any] | None,
    dialect: CliDialect,
) -> tuple[list[str], dict[str, Any]]:
    """Compile a generic, evidence-bound CLI draft without a feature allow-list.

    This deliberately validates provenance and topology scope instead of trying
    to reverse-engineer every vendor grammar.  Dedicated plugins may still
    replace this path with stronger semantic renderers for well-known features.
    """

    by_id = {str(item.get("command_id")): item for item in evidence if item.get("command_id")}
    allowed_ports = {_topology_port_key(port): port for port in topology_ports}
    protected = {_topology_port_key(str(port)) for port in (device_scope or {}).get("protected_ports", [])}
    commands: list[str] = []
    current_physical_port: str | None = None
    current_l3_context: str | None = None
    in_interface_view = False
    converted_l3_contexts: set[str] = set()
    resolved_evidence_bindings: list[dict[str, str]] = []
    evidence_ids: list[str] = []
    automatic_prerequisites: list[str] = []
    errors: list[str] = []
    inline_validation_commands: list[str] = []
    vlan_l2_roles = dict((device_scope or {}).get("vlan_l2_roles") or {})
    access_vlan_by_port = {
        _topology_port_key(str(item.get("port"))): str(item.get("vlan_id"))
        for item in vlan_l2_roles.get("access_ports", [])
        if isinstance(item, dict) and item.get("port") and item.get("vlan_id") is not None
    }
    trunk_port_keys = {
        _topology_port_key(str(port)) for port in vlan_l2_roles.get("trunk_ports", []) if str(port).strip()
    }
    required_trunk_vlans = [str(value) for value in vlan_l2_roles.get("vlan_ids", [])]
    explicit_l3_keys = {
        _topology_port_key(str(port))
        for port in (device_scope or {}).get("explicit_l3_ports", [])
        if str(port).strip()
    }
    for operation in plan.operations:
        for invocation in operation.invocations:
            raw_cli = invocation.cli
            if not raw_cli:
                errors.append(f"通用计划缺少 CLI：{operation.purpose}")
                continue
            cli = _normalize_huawei_logical_interface(_normalize_cli(raw_cli), dialect)
            if "\n" in raw_cli or not cli:
                errors.append("每个通用计划 invocation 必须且只能包含一条非空 CLI。")
                continue
            original_cli = cli
            cli, suffix_normalized_evidence = _normalize_cli_from_evidence_suffix(cli, evidence)
            interface_hint = _context_interface_name(cli)
            physical_hint = _physical_interface_name(interface_hint) if interface_hint else None
            if physical_hint and dialect.preserves_topology_port_spelling:
                expected_port = allowed_ports.get(_topology_port_key(physical_hint))
                if expected_port and _cli_mentions_port(cli, expected_port):
                    cli = f"interface {expected_port}"
            lowered = cli.casefold()
            if any(_starts_with_prefix(lowered, prefix) for prefix in FORBIDDEN_PLAN_PREFIXES):
                errors.append(f"通用计划禁止包含维护或会话控制命令：{cli}")
                continue
            # The LLM occasionally places a handbook ``display``/``show``
            # query inside an operation. It is valuable validation evidence,
            # but must never be sent as part of the configuration command
            # sequence. Preserve it in the dedicated validation list instead.
            if lowered.startswith(dialect.read_only_prefixes):
                inline_validation_commands.append(cli)
                continue
            if invocation.command_id == CONTROL_COMMAND_ID and lowered in dialect.control_commands:
                commands.append(cli)
                current_physical_port = None
                current_l3_context = None
                in_interface_view = False
                continue
            # Weak models occasionally attach ``__control__`` to a normal
            # command while otherwise producing the right CLI.  Only genuine
            # dialect control words are trusted as control commands; every
            # other line falls through to the normal manual-evidence resolver.
            evidence_item = by_id.get(invocation.command_id)
            if not evidence_item or not _matches_evidence_syntax(cli, evidence_item):
                resolved_item = _resolve_evidence_binding(
                    cli,
                    evidence,
                    current_physical_port=current_physical_port,
                    prior_commands=commands,
                )
                if not resolved_item:
                    label = evidence_item.get("canonical_name") if evidence_item else "无效 command_id"
                    errors.append(f"CLI 与引用手册命令前缀不一致：{cli} / {label}")
                    continue
                resolved_evidence_bindings.append(
                    {
                        "cli": cli,
                        "provided_command_id": invocation.command_id,
                        "resolved_command_id": str(resolved_item.get("command_id") or ""),
                    }
                )
                evidence_item = resolved_item
            if suffix_normalized_evidence:
                resolved_evidence_bindings.append(
                    {
                        "cli": cli,
                        "provided_command_id": f"syntax_suffix_normalization:{original_cli}",
                        "resolved_command_id": str(evidence_item.get("command_id") or ""),
                    }
                )
            referenced_port: str | None = None
            if invocation.target_port_ref:
                if not invocation.target_port_ref.startswith("topology:port:"):
                    errors.append(f"端口引用格式错误：{invocation.target_port_ref}")
                    continue
                referenced_port = invocation.target_port_ref.removeprefix("topology:port:")
                reference_key = _topology_port_key(referenced_port)
                expected_port = allowed_ports.get(reference_key)
                if not expected_port:
                    errors.append(f"通用计划引用了拓扑外物理端口：{referenced_port}")
                    continue
                if reference_key in protected:
                    errors.append(f"通用计划尝试修改受保护端口：{referenced_port}")
                    continue
                in_matching_physical_context = current_physical_port == reference_key
                if not _cli_mentions_port(cli, expected_port) and not in_matching_physical_context:
                    errors.append(f"CLI 未包含其声明的拓扑端口：{cli} / {expected_port}")
                    continue

            interface_name = _context_interface_name(cli)
            if interface_name:
                if in_interface_view and dialect.requires_explicit_interface_exit:
                    errors.append(f"进入接口 {interface_name} 前必须先退出当前接口视图：{cli}")
                    continue
                virtual_interface_error = _virtual_interface_example_error(interface_name, evidence)
                if virtual_interface_error:
                    errors.append(virtual_interface_error)
                    continue
                physical_port = _physical_interface_name(interface_name)
                if physical_port:
                    key = _topology_port_key(physical_port)
                    expected_ref = f"topology:port:{allowed_ports.get(key, physical_port)}"
                    if key not in allowed_ports:
                        errors.append(f"通用计划引用了拓扑外物理端口：{interface_name}")
                        continue
                    if invocation.target_port_ref != expected_ref:
                        errors.append(f"物理接口 {interface_name} 缺少或错配拓扑端口引用。")
                        continue
                    if key in protected:
                        errors.append(f"通用计划尝试进入受保护端口：{interface_name}")
                        continue
                    expected_spelling = expected_ref.removeprefix("topology:port:")
                    if dialect.preserves_topology_port_spelling and not _preserves_port_spelling(
                        physical_port, expected_spelling
                    ):
                        errors.append(
                            f"物理接口命令必须保留拓扑端口原始写法：{interface_name} / "
                            f"{expected_spelling}"
                        )
                        continue
                    current_physical_port = key
                    current_l3_context = f"physical:{key}"
                else:
                    current_physical_port = None
                    lowered_interface = interface_name.casefold()
                    current_l3_context = (
                        f"virtual:{lowered_interface}"
                        if any(
                            lowered_interface.startswith(prefix.casefold())
                            for prefix in dialect.l3_virtual_interface_conversion_prefixes
                        )
                        else None
                    )
                in_interface_view = True
            elif referenced_port:
                # Declarative CLIs usually name the physical port on every line.
                # Retaining this scope also protects a hierarchical CLI whose next
                # command follows an interface-context opener.
                current_physical_port = _topology_port_key(referenced_port)
                current_l3_context = f"physical:{current_physical_port}"
            elif any(_cli_mentions_port(cli, port) for port in topology_ports):
                errors.append(f"CLI 引用了拓扑端口但没有 target_port_ref：{cli}")
                continue
            if current_physical_port and current_physical_port in protected:
                errors.append(f"通用计划尝试修改受保护端口：{cli}")
                continue
            if dialect.supports_huawei_vlan_renderer and current_physical_port:
                if current_physical_port in explicit_l3_keys and lowered.startswith("port "):
                    errors.append(
                        f"物理端口 {current_physical_port} 已明确作为三层接口，不能再生成二层端口命令：{cli}"
                    )
                    continue
                if current_physical_port in access_vlan_by_port:
                    expected_vlan = access_vlan_by_port[current_physical_port]
                    if lowered.startswith("port link-type ") and not lowered.startswith(
                        "port link-type access"
                    ):
                        errors.append(f"端口 {current_physical_port} 应为 Access，不能生成：{cli}")
                        continue
                    if lowered.startswith("port default vlan ") and not lowered.endswith(expected_vlan):
                        errors.append(
                            f"端口 {current_physical_port} 应加入 VLAN {expected_vlan}，不能生成：{cli}"
                        )
                        continue
                if current_physical_port in trunk_port_keys:
                    if lowered.startswith("port link-type ") and not lowered.startswith(
                        "port link-type trunk"
                    ):
                        errors.append(f"交换机互联端口 {current_physical_port} 应为 Trunk，不能生成：{cli}")
                        continue
                    if lowered.startswith("port trunk allow-pass vlan "):
                        actual_vlans = re.findall(r"\d+", lowered.split("vlan", 1)[1])
                        if actual_vlans != required_trunk_vlans:
                            errors.append(
                                f"交换机互联端口 {current_physical_port} 必须放行全部 VLAN "
                                f"{', '.join(required_trunk_vlans)}，当前为：{cli}"
                            )
                            continue
            if (
                dialect.l3_physical_interface_conversion_command
                and lowered.startswith("ip address ")
                and not in_interface_view
            ):
                # In Huawei VRP, ``ip address`` is an interface-view command.
                # A global occurrence of the right address text must not
                # satisfy a requirement fact or be exposed as a ready plan.
                errors.append(f"地址配置命令缺少接口视图上下文：{cli}")
                continue
            if (
                dialect.l3_physical_interface_conversion_command
                and lowered == dialect.l3_physical_interface_conversion_command.casefold()
            ):
                expected_evidence = (dialect.l3_physical_interface_conversion_evidence or "").casefold()
                evidence_name = str(evidence_item.get("canonical_name") or "").casefold()
                if not current_l3_context:
                    errors.append(f"三层端口切换命令缺少可切换的接口上下文：{cli}")
                    continue
                if expected_evidence and not evidence_name.startswith(expected_evidence):
                    errors.append(f"三层端口切换命令未绑定对应手册证据：{cli}")
                    continue
                converted_l3_contexts.add(current_l3_context)
            if current_l3_context and lowered.startswith("ip address "):
                if (
                    dialect.l3_physical_interface_conversion_command
                    and current_l3_context not in converted_l3_contexts
                ):
                    conversion_cli = dialect.l3_physical_interface_conversion_command
                    conversion_evidence = _resolve_evidence_binding(
                        conversion_cli,
                        evidence,
                        current_physical_port=current_physical_port,
                        prior_commands=commands,
                    )
                    expected_evidence = (
                        dialect.l3_physical_interface_conversion_evidence or ""
                    ).casefold()
                    evidence_name = str(
                        (conversion_evidence or {}).get("canonical_name") or ""
                    ).casefold()
                    if not conversion_evidence or (
                        expected_evidence and not evidence_name.startswith(expected_evidence)
                    ):
                        display_port = allowed_ports.get(
                            current_physical_port or "",
                            current_physical_port or current_l3_context,
                        )
                        errors.append(
                            f"接口 {display_port} 的地址配置前缺少 "
                            f"{dialect.l3_physical_interface_conversion_command}。"
                        )
                        continue
                    # The model has already selected an evidence-bound address
                    # action in this exact interface view.  Add only the
                    # dialect-declared, handbook-proven mode transition
                    # immediately before it; this is not a feature template.
                    commands.append(conversion_cli)
                    evidence_ids.append(str(conversion_evidence.get("command_id") or ""))
                    resolved_evidence_bindings.append(
                        {
                            "cli": conversion_cli,
                            "provided_command_id": "__dialect_prerequisite__",
                            "resolved_command_id": str(conversion_evidence.get("command_id") or ""),
                        }
                    )
                    automatic_prerequisites.append(
                        f"在 {current_l3_context} 的地址配置前补齐 {conversion_cli}"
                    )
                    converted_l3_contexts.add(current_l3_context)
            commands.append(cli)
            evidence_ids.append(str(evidence_item.get("command_id") or invocation.command_id))
    if errors:
        return [], {
            "status": "blocked",
            "errors": errors,
            # Persist the non-executable JSON for audit and repair prompts; it
            # is not rendered into a device command list.
            "command_plan": plan.model_dump(mode="json"),
        }
    if not commands or not evidence_ids:
        # Session-control entries such as ``quit`` are useful only between
        # actual business commands.  Treating a control-only response as a
        # ready draft hides a weak model failure behind a syntactically valid
        # envelope (``system-view -> quit -> return``).  The condition is
        # capability-neutral: every executable configuration must bind at
        # least one non-control CLI to an imported handbook page.
        return [], {"status": "blocked", "errors": ["通用计划没有可编译的证据绑定业务 CLI。"]}

    _append_exact_global_followup_commands(
        commands,
        evidence_ids,
        resolved_evidence_bindings,
        intent=intent,
        evidence=evidence,
        dialect=dialect,
    )

    # The selected handbook may spell an address operation differently for each
    # vendor.  Therefore this only checks the explicit address value from the
    # requirement, not a vendor-specific command prefix.  It prevents a model
    # from reclassifying a requested interface address as a pre-existing fact.
    rendered = "\n".join(commands).casefold()
    rendered_compact = re.sub(r"\s+", "", rendered)
    for fact in intent.get("required_configuration_facts", []):
        if not isinstance(fact, dict) or fact.get("kind") not in {
            "interface_address",
            "logical_interface_address",
        }:
            continue
        address = str(fact.get("address") or "").strip()
        port = str(fact.get("port") or fact.get("interface") or "").strip()
        if address and address.casefold() not in rendered:
            errors.append(
                f"需求明确要求在接口 {port} 配置地址 {address}/{fact.get('prefix', '')}，"
                "但已编译 CLI 未出现该地址。"
            )
        if fact.get("kind") == "logical_interface_address":
            # The command spelling remains handbook-driven, but the named
            # logical interface itself must be present so an address for a
            # different virtual interface cannot satisfy this requirement.
            compact_port = re.sub(r"\s+", "", port.casefold())
            if compact_port and compact_port not in rendered_compact:
                errors.append(
                    f"需求明确要求在逻辑接口 {port} 配置地址 {address}/{fact.get('prefix', '')}，"
                    "但已编译 CLI 未出现该逻辑接口。"
                )
            continue
        port_key = _topology_port_key(port)
        if (
            dialect.l3_physical_interface_conversion_command
            and _physical_interface_name(port)
            and f"physical:{port_key}" not in converted_l3_contexts
        ):
            errors.append(
                f"物理接口 {port} 的三层地址配置缺少 "
                f"{dialect.l3_physical_interface_conversion_command}。"
            )
    if errors:
        return [], {
            "status": "blocked",
            "errors": errors,
            "command_plan": plan.model_dump(mode="json"),
        }

    validation_commands: list[str] = []
    for value in [*inline_validation_commands, *plan.validation_commands]:
        command = _normalize_cli(value)
        lowered = command.casefold()
        if not command or "\n" in value or not lowered.startswith(dialect.read_only_prefixes):
            readable = "、".join(item.strip() for item in dialect.read_only_prefixes)
            return [], {"status": "blocked", "errors": [f"验证命令只能是 {readable} 开头的单行命令：{value}"]}
        if any(token in lowered for token in FORBIDDEN_PLAN_PREFIXES):
            return [], {"status": "blocked", "errors": [f"验证命令包含禁止关键字：{value}"]}
        if command not in validation_commands:
            validation_commands.append(command)
    return [*dialect.configuration_enter, *commands, *dialect.configuration_exit], {
        "status": "ready",
        "errors": [],
        "source": "generic_evidence_bound_compiler",
        "command_plan": plan.model_dump(mode="json"),
        "evidence_command_ids": list(dict.fromkeys(evidence_ids)),
        "resolved_evidence_bindings": resolved_evidence_bindings,
        "automatic_prerequisites": automatic_prerequisites,
        "checks": [
            "每条业务 CLI 绑定当前手册 command_id 并匹配命令前缀",
            "任何声明的物理接口仅可引用当前设备已连线且未受保护的拓扑端口",
            "方言要求时，物理二层端口在配置显式三层地址前必须完成手册定义的模式切换",
            "会话控制、保存、重启、删除等命令不允许由通用计划生成",
            f"验证命令仅允许 {dialect.label} 的只读/连通性前缀",
        ],
        "validation_commands": validation_commands,
    }


def build_explicit_port_assignment_fallback_plan(
    plan: LlmCommandPlan,
    *,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    dialect: CliDialect = HUAWEI_VRP,
) -> LlmCommandPlan | None:
    """Rebuild explicit port-to-command actions from topology and handbook facts.

    This path is used only after a model plan fails static compilation.  A
    natural-language fact such as ``10GE1/0/1 加入 Stack-Port 1`` is not a
    product template: it names an existing topology port, a handbook command
    family and its requested argument.  The helper therefore requires the
    exact manual grammar to instantiate ``<command> <argument>`` in the
    physical port's interface context.  It can serve iStack, aggregation or
    any other imported command reference with the same explicit fact shape.
    """

    raw_facts = intent.get("required_port_command_facts", [])
    facts = [item for item in raw_facts if isinstance(item, dict)] if isinstance(raw_facts, list) else []
    if not facts:
        return None
    allowed_ports = {_topology_port_key(port): port for port in topology_ports}

    def command_key(value: object) -> str:
        return re.sub(r"[^a-z0-9-]", "", str(value or "").casefold())

    def canonical_root(item: dict[str, Any]) -> str:
        return _normalize_cli(
            str(item.get("canonical_name") or "").split("（", 1)[0].split("(", 1)[0]
        )

    assignment_command_keys = {
        command_key(str(item.get("command_hint") or "")) for item in facts
    }

    def physical_view_matches(item: dict[str, Any], port: str) -> bool:
        family = re.match(r"(?:\d+)?[a-z]+", port.casefold())
        if not family:
            return False
        views = " ".join(str(value) for value in item.get("views", [])).casefold()
        return family.group(0) in views

    def is_negated_global_cli(cli: str) -> bool:
        root = cli.split(maxsplit=1)[0].casefold()
        negative_clauses = re.findall(
            r"(?:不要|禁止|无需|不需要)[^。；;\n]+", str(intent.get("requirement") or "")
        )
        for clause in negative_clauses:
            if root and root in clause.casefold():
                return True
        return False

    operations: list[dict[str, Any]] = []
    used_global_cli: set[str] = set()
    for fact in facts:
        requested_port = str(fact.get("port") or "").strip()
        port = allowed_ports.get(_topology_port_key(requested_port))
        command_hint = str(fact.get("command_hint") or "").strip()
        argument = str(fact.get("argument") or "").strip()
        if not port or not command_hint or not argument:
            return None
        interface_cli = f"interface {port}"
        interface_item = _resolve_evidence_binding(
            interface_cli,
            evidence,
            current_physical_port=None,
            prior_commands=[],
        )
        candidates = [
            item
            for item in evidence
            if command_key(canonical_root(item)) == command_key(command_hint)
        ]
        candidates.sort(
            key=lambda item: (
                0 if physical_view_matches(item, port) else 1,
                len(canonical_root(item).split()),
            )
        )
        action_item = None
        action_cli = ""
        for candidate in candidates:
            candidate_cli = f"{canonical_root(candidate)} {argument}".strip()
            if not _matches_evidence_syntax(candidate_cli, candidate):
                continue
            if not physical_view_matches(candidate, port):
                continue
            action_item = candidate
            action_cli = candidate_cli
            break
        if not interface_item or not action_item:
            return None
        operations.append(
            {
                "purpose": f"按已确认端口事实配置 {port} -> {canonical_root(action_item)} {argument}",
                "invocations": [
                    {
                        "command_id": str(interface_item.get("command_id") or "__unverified_draft__"),
                        "syntax_index": 0,
                        "arguments": {},
                        "target_port_ref": f"topology:port:{port}",
                        "cli": interface_cli,
                    },
                    {
                        "command_id": str(action_item.get("command_id") or "__unverified_draft__"),
                        "syntax_index": 0,
                        "arguments": {},
                        "target_port_ref": None,
                        "cli": action_cli,
                    },
                    {
                        "command_id": CONTROL_COMMAND_ID,
                        "syntax_index": 0,
                        "arguments": {},
                        "target_port_ref": None,
                        "cli": next(iter(dialect.control_commands), "quit"),
                    },
                ],
            }
        )

    global_invocations: list[dict[str, Any]] = []
    for operation in plan.operations:
        for invocation in operation.invocations:
            cli = _normalize_huawei_logical_interface(_normalize_cli(str(invocation.cli or "")), dialect)
            lowered = cli.casefold()
            if (
                not cli
                or lowered in dialect.control_commands
                or lowered.startswith(dialect.read_only_prefixes)
                or _context_interface_name(cli)
                or any(_cli_mentions_port(cli, port) for port in topology_ports)
                or cli.split(maxsplit=1)[0].casefold() in {"port", "interface"}
                or command_key(cli.split(maxsplit=1)[0]) in assignment_command_keys
                or is_negated_global_cli(cli)
                or any(_starts_with_prefix(lowered, prefix) for prefix in FORBIDDEN_PLAN_PREFIXES)
                or lowered in used_global_cli
            ):
                continue
            item = _resolve_evidence_binding(
                cli,
                evidence,
                current_physical_port=None,
                prior_commands=[],
            )
            if not item:
                continue
            global_invocations.append(
                {
                    "command_id": str(item.get("command_id") or "__unverified_draft__"),
                    "syntax_index": 0,
                    "arguments": {},
                    "target_port_ref": None,
                    "cli": cli,
                }
            )
            used_global_cli.add(lowered)
    if global_invocations:
        operations.append(
            {"purpose": "保留模型已生成且手册可验证的全局配置", "invocations": global_invocations}
        )
    try:
        return LlmCommandPlan.model_validate(
            {
                "action": "command_plan",
                "operations": operations,
                "validation_commands": list(plan.validation_commands),
                "assumptions": list(plan.assumptions),
                "risks": [
                    *list(plan.risks),
                    "已用拓扑端口和手册语法重建明确指定的端口动作。",
                ],
            }
        )
    except Exception:
        return None


def compile_command_plan(
    plan: LlmCommandPlan,
    *,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    device_scope: dict[str, Any] | None = None,
    dialect: CliDialect = HUAWEI_VRP,
) -> tuple[list[str], dict[str, Any]]:
    """Compile via a deterministic plugin or the capability-neutral path."""

    vlan_ids = intent.get("vlan_ids", [])
    feature = intent.get("feature")
    if not is_huawei_vlan_renderer(intent, dialect):
        return _compile_generic_command_plan(
            plan,
            intent=intent,
            evidence=evidence,
            topology_ports=topology_ports,
            device_scope=device_scope,
            dialect=dialect,
        )
    if not vlan_ids:
        return [], {"status": "blocked", "errors": ["当前 VLAN 意图缺少 VLAN 编号。"]}
    by_id = {str(item.get("command_id")): item for item in evidence}
    prefixes = (
        VLAN_INTERVLAN_COMMAND_PREFIXES if feature == "multi_vlan_intervlan" else VLAN_ACCESS_COMMAND_PREFIXES
    )
    required = set(prefixes)
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
        name = str(evidence_item.get("matched_command") or evidence_item.get("canonical_name", "")).lower()
        names.append(name)
        syntax = evidence_item.get("syntax") or []
        if invocation.syntax_index >= len(syntax):
            return [], {
                "status": "blocked",
                "errors": [f"LLM 选择的 syntax_index 越界：{invocation.command_id}"],
            }
        expected_prefix = prefixes.get(name)
        chosen_syntax = str(syntax[invocation.syntax_index]).strip().casefold()
        syntax_matches = (
            chosen_syntax == "ip address" or chosen_syntax.startswith("ip address ip-address")
            if name == "ip address"
            else chosen_syntax.startswith(expected_prefix or "")
        )
        if expected_prefix and not syntax_matches:
            return [], {
                "status": "blocked",
                "errors": [f"LLM 选择的手册语法不适用于 VLAN Access：{name}"],
            }
        if feature == "multi_vlan_intervlan":
            # The deterministic renderer owns topology roles and final CLI text.
            # Here the LLM proves it selected only handbook records needed by the
            # immutable device scope.  Invalid output falls back to that renderer.
            continue
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
    if feature == "multi_vlan_intervlan":
        if not required.issubset(set(names)):
            return [], {"status": "blocked", "errors": ["LLM 命令计划未覆盖多 VLAN 方案所需的手册命令。"]}
        from app.planning.service import _candidate_commands

        commands, validation = _candidate_commands(intent, evidence, topology_ports, device_scope)
        validation["source"] = "llm_command_plan_compiled"
        validation["command_plan"] = plan.model_dump(mode="json")
        validation["checks"] = [
            *validation.get("checks", []),
            "LLM 已从手册证据选择多 VLAN 所需命令；最终端口角色和参数由拓扑意图编译",
        ]
        return commands, validation
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
