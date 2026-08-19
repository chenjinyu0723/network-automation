"""Inspect one existing OSPF task's command-plan response without execution."""

from __future__ import annotations

import json

from app.db import SessionLocal
from app.models import ConfigTask
from app.planning.dialect import HUAWEI_VRP
from app.planning.llm_command_plan import compile_command_plan, plan_commands_with_llm

TASK_ID = "488530aa26344b1b93324adf81af1d16"
PORTS = ["GE0/0/1", "GE0/0/2"]


def main() -> None:
    with SessionLocal() as session:
        task = session.get(ConfigTask, TASK_ID)
        if task is None:
            raise RuntimeError(f"未找到任务 {TASK_ID}")
        device_plan = task.device_plans[0]
        intent = json.loads(device_plan.intent_json)
        evidence = json.loads(device_plan.evidence_json)
        plan, audit = plan_commands_with_llm(
            session,
            requirement=intent["requirement"],
            intent=intent,
            evidence=evidence,
            topology_ports=PORTS,
            device_scope=intent["topology_scope"]["device_scope"],
            dialect=HUAWEI_VRP,
        )
        result: dict[str, object] = {"audit": audit}
        if plan:
            result["plan"] = plan.model_dump(mode="json")
            commands, validation = compile_command_plan(
                plan,
                intent=intent,
                evidence=evidence,
                topology_ports=PORTS,
                device_scope=intent["topology_scope"]["device_scope"],
                dialect=HUAWEI_VRP,
            )
            result["commands"] = commands
            result["validation"] = validation
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
