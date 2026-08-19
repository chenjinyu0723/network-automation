"""Run non-destructive common-networking command-plan probes.

The script only reads an injected manual and the locally configured LLM.  It
does not create a topology/configuration task, connect to an eNSP device, or
send any command.  Always point APP_DATA_DIR at an isolated copy of the app
data directory before running it.

Examples:
    $env:APP_DATA_DIR = 'D:\\network-automation\\data\\ospf-integration-test'
    uv run python scripts/probe_common_scenarios_llm.py static-routing
    uv run python scripts/probe_common_scenarios_llm.py lacp-mstp
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.db import SessionLocal, init_database
from app.llm.client import request_text_result, should_enable_thinking
from app.models import Command, Manual
from app.planning.dialect import resolve_cli_dialect
from app.planning.llm_command_plan import _prompt, _run_async, compile_command_plan, plan_commands_with_llm
from app.planning.llm_command_review import review_commands_with_llm
from app.planning.service import _evidence_from_command
from app.services.settings import get_provider_secret, read_provider_settings
from sqlalchemy import select


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    requirement: str
    planning_idea: str
    intent: dict[str, Any]
    scope: dict[str, Any]
    evidence_labels: tuple[str, ...]


def _syntax(command: Command) -> list[str]:
    return [str(item) for item in json.loads(command.syntax_json)]


def _pick_evidence(session, manual_id: str, labels: tuple[str, ...]) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    commands = session.scalars(select(Command).where(Command.manual_id == manual_id)).all()

    def has_view(item: Command, marker: str) -> bool:
        return marker.casefold() in item.views_json.casefold()

    def has_syntax_prefix(item: Command, prefix: str) -> bool:
        return any(value.casefold().startswith(prefix.casefold()) for value in _syntax(item))

    def pick(label: str, predicate: Callable[[Command], bool]) -> dict[str, Any]:
        command = next((item for item in commands if predicate(item)), None)
        if command is None:
            raise RuntimeError(f"手册中未找到场景探针所需命令页：{label}")
        return _evidence_from_command(command, expected_name=None, source="scenario_probe", score=1.0)

    predicates: dict[str, Callable[[Command], bool]] = {
        "interface": lambda item: item.canonical_name.casefold() == "interface",
        "portswitch": lambda item: item.canonical_name.casefold() == "portswitch",
        "physical_ip_address": lambda item: _syntax(item)[:2] == ["ip address", "ip-address"]
        and "GE" in item.views_json,
        "ip_route_static": lambda item: item.canonical_name.casefold() == "ip route-static",
        "display_ip_routing_table": lambda item: item.canonical_name.casefold() == "display ip routing-table",
        "eth_trunk": lambda item: item.canonical_name.casefold() == "eth-trunk",
        "lacp_mode": lambda item: "lacp-static" in item.syntax_json and "Eth-Trunk" in item.views_json,
        "stp_enable": lambda item: item.canonical_name.casefold() == "stp enable",
        "stp_mode": lambda item: item.canonical_name.casefold() == "stp mode",
        "display_eth_trunk": lambda item: item.canonical_name.casefold() == "display eth-trunk",
        "display_stp": lambda item: item.canonical_name.casefold() == "display stp",
        "vlan_batch": lambda item: item.canonical_name.casefold() == "vlan batch",
        "vlanif_ip_address": lambda item: _syntax(item)[:2] == ["ip address", "ip-address"]
        and item.canonical_name.casefold() == "ip address（接口视图）"
        and has_view(item, "vlanif"),
        "ethtrunk_ip_address": lambda item: _syntax(item)[:2] == ["ip address", "ip-address"]
        and item.canonical_name.casefold() == "ip address（接口视图）"
        and has_view(item, "eth-trunk"),
        "vrrp_vrid": lambda item: item.canonical_name.casefold() == "vrrp vrid",
        "vrrp_priority": lambda item: item.canonical_name.casefold() == "vrrp vrid priority",
        "display_vrrp": lambda item: item.canonical_name.casefold() == "display vrrp",
        "stack": lambda item: item.canonical_name.casefold() == "stack",
        "stack_member": lambda item: item.canonical_name.casefold() == "stack member",
        "stack_port": lambda item: item.canonical_name.casefold() == "stack-port",
        "display_stack": lambda item: item.canonical_name.casefold() == "display stack",
        "display_stack_configuration": lambda item: item.canonical_name.casefold()
        == "display stack configuration",
        "display_stack_topology": lambda item: item.canonical_name.casefold() == "display stack topology",
    }
    return [pick(label, predicates[label]) for label in labels]


SCENARIOS = {
    "static-routing": Scenario(
        key="static-routing",
        title="双站点三层静态路由（当前设备 SW1）",
        requirement=(
            "SW1 与 SW2 通过 GE0/0/1 点对点互联。当前设备 SW1 的 GE0/0/1 配置 "
            "10.0.12.1/30，对端 SW2 为 10.0.12.2/30。SW1 本地已存在 192.168.10.0/24，"
            "SW2 后方已有 192.168.20.0/24；请仅在 SW1 配置到 192.168.20.0/24 的静态路由。"
            "不要配置 VLAN、Trunk、SSH 或未连线端口。"
        ),
        planning_idea=(
            "将 GE0/0/1 切换为三层口并配置点对点 /30 地址；以 SW2 的下一跳地址创建远端站点 "
            "192.168.20.0/24 的静态路由；通过路由表和到对端地址的 ping 进行只读验收。"
        ),
        intent={
            "feature": "static_routing",
            "renderer_mode": "generic_evidence_bound",
        },
        scope={
            "mode": "generic_topology_scope",
            "device": {"id": "sw1", "name": "SW1"},
            "all_ports": ["GE0/0/1"],
            "protected_ports": [],
        },
        evidence_labels=(
            "interface",
            "portswitch",
            "physical_ip_address",
            "ip_route_static",
            "display_ip_routing_table",
        ),
    ),
    "lacp-mstp": Scenario(
        key="lacp-mstp",
        title="双链路聚合与 MSTP（当前设备 SW1）",
        requirement=(
            "SW1 与 SW2 之间使用 GE0/0/1、GE0/0/2 两条物理链路组成 Eth-Trunk 1，"
            "聚合模式为 LACP 静态。当前设备 SW1 需要启用生成树并使用 MSTP 模式。"
            "不要配置 VLAN、三层 IP、SSH 或其他端口。"
        ),
        planning_idea=(
            "先将两条已绘制的交换机互联物理口加入同一个 Eth-Trunk 1，再进入聚合口配置 "
            "LACP 静态模式；在系统视图启用生成树并指定 MSTP；使用聚合状态和生成树状态只读验收。"
        ),
        intent={
            "feature": "link_aggregation_mstp",
            "renderer_mode": "generic_evidence_bound",
        },
        scope={
            "mode": "generic_topology_scope",
            "device": {"id": "sw1", "name": "SW1"},
            "all_ports": ["GE0/0/1", "GE0/0/2"],
            "protected_ports": [],
        },
        evidence_labels=(
            "interface",
            "eth_trunk",
            "lacp_mode",
            "stp_enable",
            "stp_mode",
            "display_eth_trunk",
            "display_stp",
        ),
    ),
    "vrrp-gateway-redundancy": Scenario(
        key="vrrp-gateway-redundancy",
        title="双核心 VRRP 网关冗余（当前设备 Core1）",
        requirement=(
            "Core1 与 Core2 已经完成二层承载，接入网 VLAN 10 的用户网段为 192.168.10.0/24。"
            "当前设备 Core1 需要创建 VLAN 10，配置 Vlanif10 地址 192.168.10.2/24，"
            "并配置 VRRP 组 10 的虚拟网关 192.168.10.1、优先级 120，使其优先成为主网关。"
            "Core2 会单独配置 192.168.10.3/24、同一 VRRP 组和较低优先级。"
            "不要配置物理端口、Trunk、SSH、堆叠或任何路由协议。"
        ),
        planning_idea=(
            "在当前设备创建用户 VLAN 并建立三层 Vlanif10；为 VLANIF 配置本机网关地址；"
            "在同一 VLANIF 内创建 VRRP 组 10，指定统一虚拟 IP 并提高 Core1 优先级。"
            "验收时确认 VRRP 组状态、虚拟 IP 和当前主备角色；两台核心的 VRID 与虚拟 IP 必须一致，"
            "本机实际 IP 与优先级必须不同。"
        ),
        intent={
            "feature": "vrrp_gateway_redundancy",
            "renderer_mode": "generic_evidence_bound",
        },
        scope={
            "mode": "generic_topology_scope",
            "device": {"id": "core1", "name": "Core1"},
            "all_ports": [],
            "protected_ports": [],
            "device_role": "VLAN 10 的 VRRP 主网关候选，Core2 由其独立配置",
        },
        evidence_labels=(
            "vlan_batch",
            "interface",
            "vlanif_ip_address",
            "vrrp_vrid",
            "vrrp_priority",
            "display_vrrp",
        ),
    ),
    "l3-eth-trunk": Scenario(
        key="l3-eth-trunk",
        title="双链路 LACP 三层 Eth-Trunk（当前设备 SW1）",
        requirement=(
            "SW1 与 SW2 的 GE0/0/1、GE0/0/2 是两条已绘制的点对点链路。"
            "当前设备 SW1 需要将两口加入 Eth-Trunk 10，使用 LACP 静态模式；"
            "聚合口作为三层口，配置地址 10.0.12.1/30，对端 SW2 的聚合口地址为 10.0.12.2/30。"
            "不要配置 VLAN、Trunk 二层放通、STP、SSH 或其他端口。"
        ),
        planning_idea=(
            "先创建 Eth-Trunk 10 并启用 LACP 静态，再将两条已绘制物理链路加入该聚合口；"
            "对逻辑 Eth-Trunk 而不是成员口执行二三层切换并配置唯一的 /30 地址。"
            "验收时检查聚合成员、LACP 状态和 Eth-Trunk 三层地址，不能给成员口分别配置 IP。"
        ),
        intent={
            "feature": "l3_link_aggregation",
            "renderer_mode": "generic_evidence_bound",
        },
        scope={
            "mode": "generic_topology_scope",
            "device": {"id": "sw1", "name": "SW1"},
            "all_ports": ["GE0/0/1", "GE0/0/2"],
            "protected_ports": [],
        },
        evidence_labels=(
            "interface",
            "eth_trunk",
            "lacp_mode",
            "portswitch",
            "ethtrunk_ip_address",
            "display_eth_trunk",
            "display_ip_routing_table",
        ),
    ),
    "istack-planning": Scenario(
        key="istack-planning",
        title="双机 iStack 环形堆叠规划（当前设备 Member1）",
        requirement=(
            "两台完全相同、已确认支持 iStack 的 S5700 组成双机环形堆叠。"
            "当前设备 Member1 使用 10GE1/0/1 加入 Stack-Port 1、10GE1/0/2 加入 Stack-Port 2；"
            "将本机成员 1 的堆叠优先级设为 150，使其优先参与主设备选举。"
            "对端 Member2 会独立执行对应配置。不要修改成员 ID 或 Domain ID，不要保存、重启、复位、"
            "配置 VLAN、IP、SSH 或非这两条堆叠链路。"
        ),
        planning_idea=(
            "先进入堆叠管理视图，将当前成员 1 优先级设置为 150；"
            "分别创建 Stack-Port 1 和 2，再将两条同类型 10GE 物理口加入对应 Stack-Port。"
            "堆叠优先级变更需在重启后才生效，但本轮只生成配置草案，绝不包含保存、重启、复位。"
            "验收时只读查看堆叠成员、配置和拓扑。"
        ),
        intent={
            "feature": "istack_planning",
            "renderer_mode": "generic_evidence_bound",
        },
        scope={
            "mode": "generic_topology_scope",
            "device": {"id": "member1", "name": "Member1"},
            "all_ports": ["10GE1/0/1", "10GE1/0/2"],
            "protected_ports": [],
            "device_role": "iStack 成员 1；仅允许两条已绘制 10GE 堆叠链路",
        },
        evidence_labels=(
            "interface",
            "stack",
            "stack_member",
            "stack_port",
            "display_stack",
            "display_stack_configuration",
            "display_stack_topology",
        ),
    ),
}

REPAIR_FEEDBACK = {
    "l3-eth-trunk": {
        "issues": [
            "进入物理成员接口前必须先退出当前 Eth-Trunk 接口视图。",
            "物理接口命令必须保留拓扑端口原始写法 GE0/0/1、GE0/0/2，不能扩写为 GigabitEthernet。",
        ],
        "required_changes": [
            "在 interface Eth-Trunk 10 的地址配置结束后插入 quit，再进入每个成员口。",
            "使用 interface GE0/0/1 和 interface GE0/0/2，并保持每个成员口配置后 quit。",
        ],
    },
    "istack-planning": {
        "issues": ["Stack-Port 逻辑接口只能使用手册示例的整数编号形式，不能使用 1/1 或 1/2。"],
        "required_changes": ["改为 interface stack-port 1 和 interface stack-port 2。"],
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description="无下发的常用组网 LLM 命令探针")
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--skip-review", action="store_true", help="不执行独立 LLM 审阅节点")
    parser.add_argument(
        "--raw-output",
        action="store_true",
        help="仅保存本次隔离探针的原始模型输出，用于诊断 Schema 失败",
    )
    parser.add_argument(
        "--repair-feedback",
        action="store_true",
        help="以本场景预置的静态校验反馈测试一次命令草案重写",
    )
    args = parser.parse_args()
    scenario = SCENARIOS[args.scenario]

    init_database()
    with SessionLocal() as session:
        manual = session.scalar(
            select(Manual).where(Manual.command_count > 0).order_by(Manual.command_count.desc()).limit(1)
        )
        if manual is None:
            raise RuntimeError("隔离库没有已注入且含命令页的手册")
        dialect = resolve_cli_dialect(manual.cli_profile, manual.brand)
        evidence = _pick_evidence(session, manual.id, scenario.evidence_labels)
        intent = {**scenario.intent, "confirmed_planning_idea": scenario.planning_idea}
        if args.repair_feedback:
            feedback = REPAIR_FEEDBACK.get(scenario.key)
            if feedback is None:
                raise RuntimeError(f"场景 {scenario.key} 没有预置修订反馈")
            intent["command_repair_feedback"] = feedback
        if args.raw_output:
            settings = read_provider_settings(session)
            secret = get_provider_secret("llm")
            if not settings.llm_base_url or not settings.llm_model or not secret:
                raise RuntimeError("未配置 LLM，无法采集原始输出")
            raw = _run_async(
                request_text_result(
                    base_url=settings.llm_base_url,
                    api_key=secret,
                    model=settings.llm_model,
                    messages=_prompt(
                        scenario.requirement,
                        intent,
                        evidence,
                        list(scenario.scope["all_ports"]),
                        scenario.scope,
                        dialect,
                    ),
                    temperature=min(settings.llm_temperature, 0.2),
                    thinking=should_enable_thinking(settings.llm_thinking_mode, "command_plan"),
                )
            )
            print(
                json.dumps(
                    {
                        "scenario": scenario.key,
                        "thinking_requested": raw.thinking_requested,
                        "thinking_used": raw.thinking_used,
                        "formal_content": raw.formal_content,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        plan, llm = plan_commands_with_llm(
            session,
            requirement=scenario.requirement,
            intent=intent,
            evidence=evidence,
            topology_ports=list(scenario.scope["all_ports"]),
            device_scope=scenario.scope,
            dialect=dialect,
        )
        result: dict[str, Any] = {
            "scenario": {"key": scenario.key, "title": scenario.title},
            "manual": {"filename": manual.original_filename, "brand": manual.brand},
            "dialect": dialect.describe(),
            "requirement": scenario.requirement,
            "planning_idea": scenario.planning_idea,
            "scope": scenario.scope,
            "llm_command_plan": llm,
            "evidence": [
                {
                    "command_id": item["command_id"],
                    "canonical_name": item["canonical_name"],
                    "syntax": item["syntax"][:3],
                    "views": item["views"][:2],
                }
                for item in evidence
            ],
        }
        if plan is None:
            result["result"] = "no_command_plan"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        commands, validation = compile_command_plan(
            plan,
            intent=intent,
            evidence=evidence,
            topology_ports=list(scenario.scope["all_ports"]),
            device_scope=scenario.scope,
            dialect=dialect,
        )
        result["command_plan"] = plan.model_dump(mode="json")
        result["commands"] = commands
        result["validation"] = validation
        if not args.skip_review and validation.get("status") == "ready":
            review, review_llm = review_commands_with_llm(
                session,
                intent={**intent, "current_device_scope": scenario.scope},
                command_plan=plan.model_dump(mode="json"),
                commands=commands,
                validation=validation,
                evidence=evidence,
            )
            result["llm_command_review"] = review_llm
            result["review"] = review.model_dump(mode="json") if review else {}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
