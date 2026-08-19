"""Independently review a saved isolated command-plan probe without device I/O."""

from __future__ import annotations

import argparse
import json
import runpy
from pathlib import Path

from app.db import SessionLocal, init_database
from app.models import Manual
from app.planning.dialect import resolve_cli_dialect
from app.planning.llm_command_plan import _normalize_operation_cardinality, compile_command_plan
from app.planning.llm_command_review import review_commands_with_llm
from app.schemas import LlmCommandPlan
from sqlalchemy import select


def main() -> None:
    parser = argparse.ArgumentParser(description="审阅隔离场景探针的已保存原始命令草案")
    parser.add_argument("scenario")
    parser.add_argument("raw_result", type=Path)
    parser.add_argument("--with-repair-feedback", action="store_true")
    args = parser.parse_args()

    probe = runpy.run_path(str(Path(__file__).with_name("probe_common_scenarios_llm.py")))
    scenarios = probe["SCENARIOS"]
    if args.scenario not in scenarios:
        raise ValueError(f"未知场景：{args.scenario}")
    scenario = scenarios[args.scenario]
    raw = json.loads(args.raw_result.read_text(encoding="utf-8"))["formal_content"]

    init_database()
    with SessionLocal() as session:
        manual = session.scalar(
            select(Manual).where(Manual.command_count > 0).order_by(Manual.command_count.desc()).limit(1)
        )
        if manual is None:
            raise RuntimeError("隔离库没有已注入且含命令页的手册")
        evidence = probe["_pick_evidence"](session, manual.id, scenario.evidence_labels)
        intent = {**scenario.intent, "confirmed_planning_idea": scenario.planning_idea}
        if args.with_repair_feedback:
            intent["command_repair_feedback"] = probe["REPAIR_FEEDBACK"][scenario.key]
        intent["current_device_scope"] = scenario.scope
        plan = LlmCommandPlan.model_validate_json(_normalize_operation_cardinality(raw))
        dialect = resolve_cli_dialect(manual.cli_profile, manual.brand)
        commands, validation = compile_command_plan(
            plan,
            intent=intent,
            evidence=evidence,
            topology_ports=list(scenario.scope["all_ports"]),
            device_scope=scenario.scope,
            dialect=dialect,
        )
        review, audit = review_commands_with_llm(
            session,
            intent=intent,
            command_plan=plan.model_dump(mode="json"),
            commands=commands,
            validation=validation,
            evidence=evidence,
        )
    print(
        json.dumps(
            {
                "commands": commands,
                "validation": validation,
                "llm_command_review": audit,
                "review": review.model_dump(mode="json") if review else {},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
