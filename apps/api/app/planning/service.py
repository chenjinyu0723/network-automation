from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.graph import build_planning_graph
from app.model_resolution import resolve_series_for_model
from app.models import (
    CompatibilityStatus,
    ConfigTask,
    DeviceModel,
    DevicePlan,
    ImportStatus,
    Manual,
    TaskStatus,
    Topology,
    TopologyRevision,
)
from app.planning.llm_command_plan import compile_command_plan, plan_commands_with_llm
from app.planning.llm_command_review import review_commands_with_llm
from app.planning.llm_refinement import refine_intent_with_llm
from app.ports import port_identity
from app.retrieval.hybrid import hybrid_command_search
from app.schemas import ConfigTaskCreate, TopologyDraft

VLAN_RE = re.compile(r"\bVLAN\s*(\d{1,4})\b", re.IGNORECASE)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str) -> Any:
    return json.loads(value) if value else {}


def create_topology(session: Session, payload: TopologyDraft) -> TopologyRevision:
    names: set[str] = set()
    ips: set[str] = set()
    ports_by_switch: dict[str, set[str]] = {}
    for node in payload.nodes:
        if node.name.strip().lower() in names:
            raise ValueError(f"设备名称重复：{node.name}")
        names.add(node.name.strip().lower())
        if node.ip:
            if node.ip in ips:
                raise ValueError(f"拓扑 IP 重复：{node.ip}")
            ips.add(node.ip)
        if node.kind == "switch":
            ports_by_switch[node.id] = set()
    node_ids = {node.id for node in payload.nodes}
    for link in payload.links:
        if link.source not in node_ids or link.target not in node_ids:
            raise ValueError("连线引用了不存在的设备。")
        for switch_id, port in ((link.source, link.source_port), (link.target, link.target_port)):
            if switch_id not in ports_by_switch or port.upper() == "UNMAPPED":
                continue
            normalized = port_identity(port)
            if normalized in ports_by_switch[switch_id]:
                raise ValueError(f"交换机端口重复连线：{switch_id} / {port}")
            ports_by_switch[switch_id].add(normalized)
    topology = Topology(name=payload.name)
    session.add(topology)
    session.flush()
    revision = TopologyRevision(topology_id=topology.id, revision=1, graph_json=_json(payload.model_dump()))
    session.add(revision)
    session.commit()
    session.refresh(revision)
    return revision


def _derive_intent(requirement: str) -> dict[str, Any]:
    """Deterministic minimum IR; LLM refinement cannot expand its write scope.

    This prevents an absent/misconfigured model from turning an opaque requirement into an
    executable action. The UI exposes this IR for edit before any approval.
    """

    vlans = sorted({int(item) for item in VLAN_RE.findall(requirement) if 1 <= int(item) <= 4094})
    feature = "vlan_access" if vlans or "vlan" in requirement.lower() else "unclassified"
    return {
        "source": "deterministic_baseline",
        "feature": feature,
        "vlan_ids": vlans,
        "requirement": requirement,
        "requires_llm_refinement": True,
        "acceptance": ["设备侧 display 验证", "经授权 PC SSH 执行 ping 验收"],
    }


def _manual_series_coverage(session: Session, manual_id: str) -> set[str]:
    from app.models import DeviceModel, ModelLevel

    rows = session.scalars(
        select(DeviceModel).where(
            DeviceModel.source_manual_id == manual_id,
            DeviceModel.level == ModelLevel.series,
        )
    ).all()
    return {row.canonical_name.upper() for row in rows}


def _compatibility(
    *,
    session: Session,
    manual: Manual,
    detected_model: str | None,
    detected_release: str | None,
    covered_series: set[str],
) -> tuple[CompatibilityStatus, str, str | None]:
    """Allow execution when the reported hardware maps to a covered manual series.

    The user explicitly selected series-level compatibility: a submodel such as
    S5735 may use the reviewed S5700-series command reference.  This does *not*
    infer ports, bypass the protected-port list, or remove command evidence and
    verification gates.  Running release is retained for audit but not used as
    an execution block.
    """

    if not detected_model:
        return CompatibilityStatus.unresolved, "未确认设备具体型号；禁止生成可执行命令。", None
    resolution = resolve_series_for_model(
        session,
        model_name=detected_model,
        brand=manual.brand,
        covered_series=covered_series,
    )
    if resolution is None:
        return (
            CompatibilityStatus.incompatible,
            f"设备型号 {detected_model} 不在手册已证实的系列覆盖范围内。",
            None,
        )
    series = resolution.series
    resolution_detail = (
        f"通过型号库层级 {' → '.join(resolution.path)} 解析。"
        if resolution.source == "model_catalog_tree"
        else "通过已知华为系列前缀解析（尚未找到型号库子型号记录）。"
    )
    return (
        CompatibilityStatus.exact,
        (
            f"设备型号 {detected_model} 已归属 {series}，与当前手册系列覆盖匹配。"
            f"{resolution_detail}"
            f"运行版本 {detected_release or '未读取'} 仅记录审计，不作为阻断条件。"
        ),
        series,
    )


def _find_evidence(session: Session, manual_id: str, intent: dict[str, Any]) -> list[dict[str, Any]]:
    required_terms = (
        ["vlan batch", "port link-type", "port default vlan"] if intent["feature"] == "vlan_access" else []
    )
    llm_terms = [
        str(item).strip()
        for item in intent.get("retrieval_terms", [])
        if isinstance(item, str) and item.strip()
    ]
    # Required command names are always searched first.  LLM-provided terms
    # broaden recall only; they cannot replace the evidence needed by the
    # deterministic VLAN renderer.
    terms = list(dict.fromkeys([*required_terms, *llm_terms]))
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for term in terms:
        for hit in hybrid_command_search(
            session,
            query=term,
            manual_id=manual_id,
            limit=8,
        ):
            command = hit.command
            if command.id in seen:
                continue
            seen.add(command.id)
            evidence.append(
                {
                    "command_id": command.id,
                    "canonical_name": command.canonical_name,
                    "syntax": _load(command.syntax_json),
                    "views": _load(command.views_json),
                    "source_path": _load(command.evidence_json).get("source_path"),
                    "retrieval_score": round(hit.score, 4),
                    "retrieval_sources": list(hit.sources),
                }
            )
    return evidence[:30]


def _candidate_commands(
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
) -> tuple[list[str], dict[str, Any]]:
    if intent["feature"] != "vlan_access" or not intent["vlan_ids"]:
        return [], {"status": "blocked", "errors": ["需求未形成可验证的配置意图。"]}
    if not topology_ports:
        return [], {"status": "blocked", "errors": ["没有已映射的交换机端口；禁止猜测接口。"]}
    evidence_names = {str(item["canonical_name"]).lower() for item in evidence}
    required = {"vlan batch", "port link-type", "port default vlan"}
    missing = sorted(name for name in required if name not in evidence_names)
    if missing:
        return [], {"status": "blocked", "errors": [f"手册证据不完整：缺少 {', '.join(missing)}"]}
    evidence_by_name = {str(item["canonical_name"]).lower(): item for item in evidence}
    port_default_syntax = [str(item).lower() for item in evidence_by_name["port default vlan"]["syntax"]]
    if not any(item.startswith("port default vlan") for item in port_default_syntax):
        return [], {"status": "blocked", "errors": ["未定位到接口视图的 port default vlan 命令格式。"]}
    vlan_args = " ".join(str(item) for item in intent["vlan_ids"])
    commands = ["system-view", f"vlan batch {vlan_args}"]
    for port in topology_ports:
        commands.extend(
            [
                f"interface {port}",
                "port link-type access",
                f"port default vlan {intent['vlan_ids'][0]}",
                "quit",
            ]
        )
    commands.append("return")
    return commands, {
        "status": "ready",
        "errors": [],
        "checks": [
            "VLAN ID 在 1-4094 范围内",
            "先创建 VLAN 再引用端口 PVID",
            "每个接口来自拓扑端口映射，未推测接口",
            "显式设置 Access，避免依赖不同版本默认链路类型",
        ],
        "validation_commands": [
            f"display vlan {vlan_args}",
            *[f"display port vlan {port}" for port in topology_ports],
        ],
    }


def _llm_command_plan_outcome(
    session: Session,
    *,
    requirement: str,
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
) -> dict[str, Any]:
    plan, llm = plan_commands_with_llm(
        session,
        requirement=requirement,
        intent=intent,
        evidence=evidence,
        topology_ports=topology_ports,
    )
    return {
        "command_plan": plan.model_dump(mode="json") if plan else {},
        "llm": llm,
    }


def _render_command_plan_or_fallback(
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    command_plan: dict[str, Any] | None,
) -> tuple[list[str], dict[str, Any]]:
    if command_plan:
        from app.schemas import LlmCommandPlan

        try:
            parsed = LlmCommandPlan.model_validate(command_plan)
            commands, validation = compile_command_plan(
                parsed,
                intent=intent,
                evidence=evidence,
                topology_ports=topology_ports,
            )
            if validation.get("status") == "ready":
                return commands, validation
            return commands, validation
        except Exception as exc:
            return [], {"status": "blocked", "errors": [f"CommandPlan 编译失败：{str(exc)[:240]}"]}
    # No configured/available LLM: preserve the tested deterministic baseline.
    return _candidate_commands(intent, evidence, topology_ports)


def _llm_command_review_outcome(session: Session, state: dict[str, Any]) -> dict[str, Any]:
    review, llm = review_commands_with_llm(
        session,
        intent=dict(state.get("intent", {})),
        command_plan=dict(state.get("command_plan", {})),
        commands=list(state.get("candidate_commands", [])),
        validation=dict(state.get("validation", {})),
        evidence=list(state.get("evidence", [])),
    )
    return {"review": review.model_dump(mode="json") if review else {}, "llm": llm}


def _rollback_draft(intent: dict[str, Any], topology_ports: list[str]) -> dict[str, Any]:
    """Return a non-executable-until-reviewed rollback draft for the first intent."""

    if intent.get("feature") != "vlan_access" or not intent.get("vlan_ids"):
        return {"level": "manual", "commands": [], "reason": "没有可推导的受限回滚模板。"}
    vlan_id = intent["vlan_ids"][0]
    commands = ["system-view"]
    for port in topology_ports:
        commands.extend([f"interface {port}", f"undo port default vlan {vlan_id}", "quit"])
    commands.extend([f"undo vlan batch {vlan_id}", "return"])
    return {
        "level": "conditional",
        "commands": commands,
        "requires_snapshot_review": True,
        "reason": (
            "仅当执行前快照证明端口原 PVID 为默认值且 VLAN 在下发前不存在时，才可人工审批执行。"
            "共享 VLAN、非默认 PVID、聚合口或业务依赖场景禁止自动回滚。"
        ),
    }


def _switch_ports_from_topology(graph: dict[str, Any], node_id: str) -> tuple[list[str], set[str]]:
    node = next((item for item in graph.get("nodes", []) if item.get("id") == node_id), {})
    protected = {port_identity(str(item)) for item in node.get("protected_ports", [])}
    ports: list[str] = []
    for link in graph.get("links", []):
        if link.get("source") == node_id:
            port = str(link.get("source_port", "")).strip()
        elif link.get("target") == node_id:
            port = str(link.get("target_port", "")).strip()
        else:
            continue
        if port and port.upper() != "UNMAPPED":
            ports.append(port)
    return ports, protected


def _pc_facing_ports_from_topology(graph: dict[str, Any], node_id: str) -> list[str]:
    """Return only the first feature's safe Access candidates.

    VLAN Access must not infer that an inter-switch, cloud, or unknown link is
    an access port.  The topology itself is the scope proof: this first plugin
    only targets a switch endpoint whose peer is explicitly a PC node.
    """

    nodes_by_id = {str(item.get("id")): item for item in graph.get("nodes", [])}
    ports: list[str] = []
    for link in graph.get("links", []):
        if link.get("source") == node_id:
            peer = nodes_by_id.get(str(link.get("target")), {})
            port = str(link.get("source_port", "")).strip()
        elif link.get("target") == node_id:
            peer = nodes_by_id.get(str(link.get("source")), {})
            port = str(link.get("target_port", "")).strip()
        else:
            continue
        if peer.get("kind") == "pc" and port and port.upper() != "UNMAPPED":
            ports.append(port)
    return ports


def create_config_task(session: Session, payload: ConfigTaskCreate) -> ConfigTask:
    revision = session.get(TopologyRevision, payload.topology_revision_id)
    manual = session.get(Manual, payload.manual_id)
    if not revision:
        raise ValueError("拓扑 revision 不存在")
    if not manual:
        raise ValueError("手册不存在")
    if manual.status not in {ImportStatus.completed, ImportStatus.completed_with_issues}:
        raise ValueError("手册尚未完成抽取，不能创建配置任务")
    baseline_intent = _derive_intent(payload.requirement_text)
    task = ConfigTask(
        topology_revision_id=revision.id,
        manual_id=manual.id,
        requirement_text=payload.requirement_text,
        status=TaskStatus.planning,
        intent_json=_json(baseline_intent),
    )
    session.add(task)
    session.flush()
    graph = _load(revision.graph_json)
    covered_series = _manual_series_coverage(session, manual.id)
    plans: list[DevicePlan] = []
    llm_outcome: dict[str, Any] | None = None

    def refine_once(requirement: str, baseline: dict[str, Any]) -> dict[str, Any]:
        nonlocal llm_outcome
        if llm_outcome is None:
            llm_outcome = refine_intent_with_llm(
                session,
                requirement=requirement,
                baseline=baseline,
            )
        return llm_outcome

    switches = [node for node in graph.get("nodes", []) if node.get("kind") == "switch"]
    if not switches:
        task.status = TaskStatus.blocked
        task.blocking_reason = "拓扑中没有交换机节点。"
    for node in switches:
        selected_model = session.get(DeviceModel, node.get("model_id")) if node.get("model_id") else None
        detected_model = (
            node.get("detected_model")
            or node.get("model_name")
            or (selected_model.canonical_name if selected_model else None)
        )
        detected_release = node.get("detected_release")
        status, reason, series = _compatibility(
            session=session,
            manual=manual,
            detected_model=detected_model,
            detected_release=detected_release,
            covered_series=covered_series,
        )
        topology_ports, protected_ports = _switch_ports_from_topology(graph, str(node["id"]))
        access_ports = _pc_facing_ports_from_topology(graph, str(node["id"]))
        initial_errors: list[str] = []
        if any(port_identity(port) in protected_ports for port in access_ports):
            initial_errors.append("拓扑把受保护端口加入了配置范围；禁止生成命令。")
        elif not topology_ports:
            initial_errors.append("交换机没有带端口名的拓扑连线；禁止猜测接口。")
        elif not access_ports:
            initial_errors.append("当前 VLAN Access 功能只配置直连 PC 的端口；未找到可配置的 PC 链路。")
        if status != CompatibilityStatus.exact:
            initial_errors.append(reason)
        graph_result = build_planning_graph(
            intent_refiner=refine_once,
            evidence_retriever=lambda graph_intent: _find_evidence(session, manual.id, graph_intent),
            command_planner=lambda graph_intent, graph_evidence: _llm_command_plan_outcome(
                session,
                requirement=payload.requirement_text,
                intent=graph_intent,
                evidence=graph_evidence,
                topology_ports=access_ports,
            ),
            command_renderer=lambda graph_intent, graph_evidence, command_plan: (
                _render_command_plan_or_fallback(
                    graph_intent, graph_evidence, access_ports, command_plan
                )
            ),
            command_reviewer=lambda planning_state: _llm_command_review_outcome(session, planning_state),
        ).invoke(
            {
                "task_id": task.id,
                "device_id": str(node["id"]),
                "requirement": payload.requirement_text,
                "intent": baseline_intent,
                "validation_errors": initial_errors,
            }
        )
        intent = dict(graph_result.get("intent", baseline_intent))
        llm_status = dict(graph_result.get("llm", {"status": "not_run"}))
        intent["llm"] = llm_status
        intent["llm_command_plan"] = dict(graph_result.get("command_plan_llm", {"status": "not_run"}))
        intent["llm_command_review"] = dict(graph_result.get("command_review", {"status": "not_run"}))
        evidence = list(graph_result.get("evidence", []))
        commands = list(graph_result.get("candidate_commands", []))
        validation = dict(graph_result.get("validation", {}))
        if graph_result.get("validation_errors"):
            commands = []
            validation = {
                "status": "blocked",
                "errors": graph_result["validation_errors"],
                "langgraph_state": graph_result.get("next_action"),
            }
        intent["topology_scope"] = {
            "all_linked_ports": topology_ports,
            "vlan_access_candidate_ports": access_ports,
            "rule": "仅配置交换机直连 PC 的端口；上联、云和交换机互联端口不纳入 VLAN Access。",
        }
        rollback = _rollback_draft(intent, access_ports)
        plan = DevicePlan(
            task_id=task.id,
            device_node_id=str(node["id"]),
            display_name=str(node.get("name") or node.get("label") or node["id"]),
            detected_model=detected_model,
            detected_release=detected_release,
            mapped_series=series,
            compatibility_status=status,
            compatibility_reason=reason,
            intent_json=_json(intent),
            evidence_json=_json(evidence),
            commands_json=_json(commands),
            validation_json=_json(validation),
            rollback_json=_json(rollback),
        )
        session.add(plan)
        plans.append(plan)
    if llm_outcome:
        task_intent = dict(llm_outcome["intent"])
        task_intent["llm"] = dict(llm_outcome.get("llm", {}))
        task.intent_json = _json(task_intent)
    if switches and all(
        plan.compatibility_status == CompatibilityStatus.exact
        and _load(plan.validation_json).get("status") == "ready"
        for plan in plans
    ):
        task.status = TaskStatus.needs_review
    elif switches:
        task.status = TaskStatus.blocked
        task.blocking_reason = "存在型号/版本不兼容或未确认设备，写执行已阻断。"
    session.commit()
    session.refresh(task)
    return task


def approve_device_plan(
    session: Session,
    plan_id: str,
    approval_revision: int,
    commands: list[str] | None,
) -> DevicePlan:
    plan = session.get(DevicePlan, plan_id)
    if not plan:
        raise ValueError("设备计划不存在")
    if plan.compatibility_status != CompatibilityStatus.exact:
        raise ValueError(f"设备版本/型号未精确匹配：{plan.compatibility_reason}")
    if approval_revision != plan.approval_revision:
        raise ValueError("审批 revision 已过期，请重新审阅当前命令。")
    if commands is not None:
        if not commands:
            raise ValueError("命令覆盖不能为空")
        plan.commands_json = _json(commands)
        plan.approval_revision += 1
        plan.rollback_json = _json(
            {
                "level": "manual_review_required",
                "commands": [],
                "reason": "用户编辑了正向命令；请基于执行前快照重新生成并审批回滚方案。",
            }
        )
    validation = _load(plan.validation_json)
    if validation.get("status") != "ready":
        raise ValueError("静态验证未通过；当前计划不可审批。")
    plan.approved_at = datetime.utcnow()
    task = plan.task
    if all(
        item.approved_at is not None
        and item.compatibility_status == CompatibilityStatus.exact
        and _load(item.validation_json).get("status") == "ready"
        for item in task.device_plans
    ):
        task.status = TaskStatus.approved
    session.commit()
    session.refresh(plan)
    return plan
