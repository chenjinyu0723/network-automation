from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import ConfigTask, ConfigurationTemplate, Manual


def _load(value: str) -> Any:
    return json.loads(value) if value else {}


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sanitize_template_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Expose templates as user-facing results, never planning internals."""

    sanitized = dict(snapshot)
    raw_plans = snapshot.get("device_plans")
    if not isinstance(raw_plans, list):
        return sanitized
    sanitized["device_plans"] = [
        {
            "display_name": str(item.get("display_name") or "未命名设备"),
            "device_node_id": str(item.get("device_node_id") or ""),
            "commands": list(item.get("commands") or []),
        }
        for item in raw_plans
        if isinstance(item, dict)
    ]
    return sanitized


def validate_template_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate template-owned facts before storing an editable snapshot.

    A template is independent from later topology revisions, but every command
    block must still belong to a switch in the template's own saved graph.
    """

    topology = snapshot.get("topology")
    if not isinstance(topology, dict):
        raise ValueError("模板必须包含有效拓扑")
    nodes = topology.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("模板拓扑节点格式无效")
    switch_nodes = {
        str(node.get("id") or "")
        for node in nodes
        if isinstance(node, dict) and str(node.get("kind") or "") == "switch"
    }
    plans = snapshot.get("device_plans")
    if not isinstance(plans, list):
        raise ValueError("模板设备命令格式无效")
    seen: set[str] = set()
    normalized_plans: list[dict[str, Any]] = []
    for raw_plan in plans:
        if not isinstance(raw_plan, dict):
            raise ValueError("模板中存在无效的设备命令")
        device_node_id = str(raw_plan.get("device_node_id") or "").strip()
        if device_node_id not in switch_nodes:
            raise ValueError("模板设备命令只能关联拓扑中的交换机")
        if device_node_id in seen:
            raise ValueError("同一台交换机只能保留一组模板命令")
        seen.add(device_node_id)
        commands = raw_plan.get("commands")
        if not isinstance(commands, list) or any(not isinstance(item, str) for item in commands):
            raise ValueError("模板命令必须是文本行列表")
        normalized_plans.append(
            {
                "display_name": str(raw_plan.get("display_name") or device_node_id).strip() or device_node_id,
                "device_node_id": device_node_id,
                "commands": [item.rstrip() for item in commands],
            }
        )
    return {
        "topology": topology,
        "topology_id": str(snapshot.get("topology_id") or "").strip() or None,
        "requirement_text": str(snapshot.get("requirement_text") or ""),
        "planning_idea": str(snapshot.get("planning_idea") or ""),
        "device_plans": normalized_plans,
    }


def template_snapshot(task: ConfigTask) -> dict[str, Any]:
    """Freeze the reviewed task so a template never changes with its source."""

    return validate_template_snapshot({
        "topology": _load(task.topology_revision.graph_json),
        "topology_id": task.topology_revision.topology_id,
        "requirement_text": task.requirement_text,
        "planning_idea": task.planning_idea,
        "device_plans": [
            {
                "display_name": plan.display_name,
                "device_node_id": plan.device_node_id,
                "commands": _load(plan.commands_json),
            }
            for plan in task.device_plans
        ],
    })


def create_template(
    session: Session,
    *,
    title: str,
    description: str,
    snapshot: dict[str, Any],
) -> ConfigurationTemplate:
    template = ConfigurationTemplate(
        title=title.strip(),
        description=description.strip(),
        snapshot_json=_dump(validate_template_snapshot(snapshot)),
    )
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def create_template_from_task(
    session: Session,
    *,
    task_id: str,
    title: str,
    description: str,
) -> ConfigurationTemplate:
    task = session.get(ConfigTask, task_id)
    if not task:
        raise ValueError("配置任务不存在")
    if not task.planning_idea.strip():
        raise ValueError("配置思路为空，不能保存为模板")
    if not task.device_plans:
        raise ValueError("该任务尚未生成设备命令，不能保存为模板")
    manual = session.get(Manual, task.manual_id)
    template = ConfigurationTemplate(
        title=title.strip(),
        description=description.strip(),
        source_task_id=task.id,
        manual_name=manual.original_filename if manual else None,
        snapshot_json=_dump(template_snapshot(task)),
    )
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def update_template(
    session: Session,
    *,
    template_id: str,
    title: str,
    description: str,
    snapshot: dict[str, Any] | None = None,
) -> ConfigurationTemplate:
    template = session.get(ConfigurationTemplate, template_id)
    if not template:
        raise ValueError("配置模板不存在")
    template.title = title.strip()
    template.description = description.strip()
    if snapshot is not None:
        template.snapshot_json = _dump(validate_template_snapshot(snapshot))
    session.commit()
    session.refresh(template)
    return template


def delete_template(session: Session, template_id: str) -> None:
    template = session.get(ConfigurationTemplate, template_id)
    if not template:
        raise ValueError("配置模板不存在")
    session.delete(template)
    session.commit()
