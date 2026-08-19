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


def template_snapshot(task: ConfigTask) -> dict[str, Any]:
    """Freeze the reviewed task so a template never changes with its source."""

    return sanitize_template_snapshot({
        "topology": _load(task.topology_revision.graph_json),
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
) -> ConfigurationTemplate:
    template = session.get(ConfigurationTemplate, template_id)
    if not template:
        raise ValueError("配置模板不存在")
    template.title = title.strip()
    template.description = description.strip()
    session.commit()
    session.refresh(template)
    return template


def delete_template(session: Session, template_id: str) -> None:
    template = session.get(ConfigurationTemplate, template_id)
    if not template:
        raise ValueError("配置模板不存在")
    session.delete(template)
    session.commit()
