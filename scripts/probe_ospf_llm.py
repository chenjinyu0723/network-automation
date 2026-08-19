"""Run one non-destructive OSPF CommandPlan probe against an injected manual.

Use only with an isolated APP_DATA_DIR.  It reads the selected manual and
provider settings, calls the configured LLM once, and writes no topology,
task, device, or execution record.
"""

from __future__ import annotations

import json

from app.db import SessionLocal, init_database
from app.models import Command, Manual
from app.planning.dialect import resolve_cli_dialect
from app.planning.llm_command_plan import compile_command_plan, plan_commands_with_llm
from app.planning.service import _evidence_from_command
from sqlalchemy import select


def _command_evidence(session, manual_id: str) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    commands = session.scalars(select(Command).where(Command.manual_id == manual_id)).all()

    def pick(label: str, predicate):  # type: ignore[no-untyped-def]
        command = next((item for item in commands if predicate(item)), None)
        if command is None:
            raise RuntimeError(f"手册中未找到 OSPF 探针需要的命令页：{label}")
        return _evidence_from_command(command, expected_name=None, source="scenario_probe", score=1.0)

    return [
        pick("interface", lambda item: item.canonical_name.casefold() == "interface"),
        pick("portswitch", lambda item: item.canonical_name.casefold() == "portswitch"),
        pick(
            "ip address（物理接口视图）",
            lambda item: json.loads(item.syntax_json)[:2] == ["ip address", "ip-address"]
            and "100GE" in item.views_json,
        ),
        pick("ospf", lambda item: item.canonical_name.casefold() == "ospf"),
        pick("area", lambda item: item.canonical_name.casefold() == "area"),
        pick(
            "network（OSPF 区域视图）",
            lambda item: json.loads(item.syntax_json)[:2] == ["network", "address"]
            and "ospf" in item.views_json.casefold(),
        ),
        pick(
            "display ospf peer",
            lambda item: item.canonical_name.casefold() == "display ospf peer",
        ),
        pick(
            "display ospf routing",
            lambda item: item.canonical_name.casefold() == "display ospf routing",
        ),
    ]


def main() -> None:
    init_database()
    with SessionLocal() as session:
        manual = session.scalar(
            select(Manual).where(Manual.command_count > 0).order_by(Manual.command_count.desc()).limit(1)
        )
        if manual is None:
            raise RuntimeError("隔离库没有已注入的命令手册")
        dialect = resolve_cli_dialect(manual.cli_profile, manual.brand)
        evidence = _command_evidence(session, manual.id)
        requirement = (
            "SW1、SW2、SW3 构成三角形三层互联，所有链路启用 OSPF 进程 1、Area 0。"
            "本设备为 SW1：GE0/0/1 接 SW2，配置 10.0.12.1/30；GE0/0/2 接 SW3，"
            "配置 10.0.13.1/30；router-id 为 1.1.1.1。不要配置 VLAN、二层 Trunk、"
            "管理 SSH 或未连线端口。"
        )
        intent = {
            "feature": "l3_ospf_ipv4",
            "renderer_mode": "generic_evidence_bound",
            "confirmed_planning_idea": (
                "将两条互联 GE 端口转为三层口，配置 /30 地址；启动 OSPF Area 0 并发布两条直连网段；"
                "使用 OSPF 邻居和路由表只读验收。"
            ),
        }
        scope = {
            "mode": "generic_topology_scope",
            "device": {"id": "sw1", "name": "SW1"},
            "all_ports": ["GE0/0/1", "GE0/0/2"],
            "protected_ports": [],
        }
        plan, llm = plan_commands_with_llm(
            session,
            requirement=requirement,
            intent=intent,
            evidence=evidence,
            topology_ports=scope["all_ports"],
            device_scope=scope,
            dialect=dialect,
        )
        result: dict[str, object] = {
            "manual": manual.original_filename,
            "dialect": dialect.describe(),
            "llm": llm,
            "evidence": [
                {"command_id": item["command_id"], "name": item["canonical_name"]} for item in evidence
            ],
        }
        if plan is not None:
            commands, validation = compile_command_plan(
                plan,
                intent=intent,
                evidence=evidence,
                topology_ports=scope["all_ports"],
                device_scope=scope,
                dialect=dialect,
            )
            result["command_plan"] = plan.model_dump(mode="json")
            result["commands"] = commands
            result["validation"] = validation
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
