from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from netmiko import ConnectHandler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.readonly import _parse_version
from app.model_resolution import resolve_series_for_model
from app.models import (
    CompatibilityStatus,
    ConfigTask,
    DevicePlan,
    ExecutionCommand,
    ExecutionRun,
    ExecutionStatus,
    Manual,
    ModelLevel,
    TaskStatus,
)
from app.ports import port_appears_in_output, port_identity

ERROR_PATTERNS = (
    re.compile(r"\berror\b", re.IGNORECASE),
    re.compile(r"\bfail(?:ed)?\b", re.IGNORECASE),
    re.compile(r"unrecognized command", re.IGNORECASE),
    re.compile(r"wrong parameter", re.IGNORECASE),
    re.compile(r"incomplete command", re.IGNORECASE),
    re.compile(r"too many parameters", re.IGNORECASE),
)
FORBIDDEN_COMMAND_TOKENS = ("save", "reboot", "reset", "format", "delete", "clear")
SAVE_CONFIRM_RE = re.compile(r"(?:are you sure.*\[Y/N\]|continue\?\s*\[Y/N\])", re.IGNORECASE)
SAVE_FILENAME_RE = re.compile(r"(?:input the file name|\[vrpcfg\.(?:zip|cfg)\])", re.IGNORECASE)
SAVE_SUCCESS_RE = re.compile(
    r"(?:save (?:the )?configuration.*(?:success|succeed)|configuration.*(?:saved|written)|successfully)",
    re.IGNORECASE,
)


def _load(value: str) -> Any:
    return json.loads(value) if value else {}


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _command_failed(output: str) -> bool:
    return any(pattern.search(output) for pattern in ERROR_PATTERNS)


def _save_completed(output: str) -> bool:
    return bool(SAVE_SUCCESS_RE.search(output)) and not _command_failed(output)


def _complete_huawei_save(connection) -> str:  # type: ignore[no-untyped-def]
    """Advance known VRP save prompts and require an explicit completion signal."""

    output = connection.send_command_timing("save", read_timeout=30)
    if SAVE_CONFIRM_RE.search(output):
        output += connection.send_command_timing("y", read_timeout=30)
    if SAVE_FILENAME_RE.search(output):
        output += connection.send_command_timing("\n", read_timeout=30)
    return output


def record_recovered_save(
    session: Session, *, execution_id: str, output: str, success: bool
) -> None:
    """Record a save completed after an older executor stopped at a VRP prompt."""

    execution = session.get(ExecutionRun, execution_id)
    if not execution:
        raise ValueError("????????")
    if execution.status != ExecutionStatus.completed:
        raise ValueError("??????????????????")
    sequence = max((item.sequence for item in execution.commands), default=0) + 1
    session.add(
        ExecutionCommand(
            execution_id=execution.id,
            sequence=sequence,
            phase="save_recovery",
            command="save",
            output=output[-20_000:],
            success=success,
        )
    )
    execution.save_json = _dump(
        {"attempted": True, "success": success, "completion_detected": success, "recovered": True}
    )
    execution.error_message = None if success else "save ??????????"
    execution.finished_at = datetime.utcnow()
    session.commit()


def _check_write_commands(
    commands: list[str],
    protected_ports: set[str],
    allowed_ports: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    current_port: str | None = None
    for command in commands:
        normalized = " ".join(command.strip().split())
        lowered = normalized.lower()
        if any(token in lowered for token in FORBIDDEN_COMMAND_TOKENS):
            errors.append(f"普通配置块禁止包含维护命令：{normalized}")
        interface_match = re.fullmatch(r"interface\s+(.+)", normalized, re.IGNORECASE)
        if interface_match:
            current_port = port_identity(interface_match.group(1))
        if interface_match and current_port in protected_ports:
            errors.append(f"命令尝试进入受保护端口 {current_port}")
        if interface_match and allowed_ports is not None and current_port not in allowed_ports:
            errors.append(f"命令尝试进入当前功能范围外端口 {current_port}")
    return errors


def _validate_vlan_access_output(
    output: str,
    *,
    vlan_ids: list[int],
    expected_ports: list[str],
) -> tuple[bool, list[str]]:
    """Small deterministic quality gate for the first supported intent.

    A successful CLI response only means it parsed.  Before save, require the
    expected VLAN number and each mapped port to appear in device validation
    output.  Vendor/feature plugins can add stronger parsers later.
    """

    missing: list[str] = []
    for vlan_id in vlan_ids:
        if not re.search(rf"(?<!\d){vlan_id}(?!\d)", output):
            missing.append(f"VLAN {vlan_id}")
    for port in expected_ports:
        if not port_appears_in_output(output, port):
            missing.append(f"端口 {port}")
    return not missing, missing


def _topology_node(task: ConfigTask, node_id: str) -> dict[str, Any]:
    graph = _load(task.topology_revision.graph_json)
    node = next((item for item in graph.get("nodes", []) if item.get("id") == node_id), None)
    if not node:
        raise ValueError("执行前找不到冻结拓扑中的设备节点。")
    return node


def _manual_series_coverage(session: Session, manual_id: str) -> set[str]:
    from app.models import DeviceModel

    rows = session.scalars(
        select(DeviceModel).where(
            DeviceModel.source_manual_id == manual_id,
            DeviceModel.level == ModelLevel.series,
        )
    ).all()
    return {row.canonical_name.upper() for row in rows}


def _previous_device_is_complete(session: Session, task: ConfigTask, plan: DevicePlan) -> bool:
    """Only the first device, or the one after a completed prior plan, can run."""

    plans = sorted(task.device_plans, key=lambda item: (item.created_at, item.id))
    position = next((index for index, item in enumerate(plans) if item.id == plan.id), None)
    if position in {None, 0}:
        return True
    predecessor = plans[position - 1]
    latest = max(predecessor.executions, key=lambda item: item.created_at, default=None)
    return bool(latest and latest.status == ExecutionStatus.completed)


def _record(
    session: Session,
    execution: ExecutionRun,
    *,
    sequence: int,
    phase: str,
    command: str,
    output: str,
    success: bool,
) -> None:
    session.add(
        ExecutionCommand(
            execution_id=execution.id,
            sequence=sequence,
            phase=phase,
            command=command,
            output=output[-20_000:],
            success=success,
        )
    )
    session.commit()


def execute_huawei_device_plan(
    session: Session,
    *,
    task_id: str,
    plan_id: str,
    host: str,
    port: int,
    username: str,
    password: str,
) -> ExecutionRun:
    """Execute exactly one approved Huawei plan and automatically save only after validation.

    This function receives credentials solely for the active Netmiko connection;
    neither models nor log records have a password field.
    """

    task = session.get(ConfigTask, task_id)
    plan = session.get(DevicePlan, plan_id)
    if not task or not plan or plan.task_id != task.id:
        raise ValueError("配置任务或设备计划不存在。")
    # Eagerly load ORM relationships while the request session is still active;
    # this also makes the single-device ordering decision explicit and auditable.
    _ = list(task.device_plans)
    _ = list(plan.executions)
    for sibling in task.device_plans:
        _ = list(sibling.executions)
    node = _topology_node(task, plan.device_node_id)
    execution = ExecutionRun(
        task_id=task.id,
        device_plan_id=plan.id,
        target_host=host,
        target_port=port,
        execution_revision=plan.approval_revision,
        status=ExecutionStatus.queued,
    )
    session.add(execution)
    session.flush()
    session.commit()

    protected_ports = {port_identity(str(item)) for item in node.get("protected_ports", [])}
    commands = _load(plan.commands_json)
    validation = _load(plan.validation_json)
    intent = _load(plan.intent_json)
    graph = _load(task.topology_revision.graph_json)
    expected_ports = []
    nodes_by_id = {str(item.get("id")): item for item in graph.get("nodes", [])}
    for link in graph.get("links", []):
        if link.get("source") == plan.device_node_id:
            peer = nodes_by_id.get(str(link.get("target")), {})
            if peer.get("kind") == "pc":
                expected_ports.append(str(link.get("source_port", "")).strip())
        elif link.get("target") == plan.device_node_id:
            peer = nodes_by_id.get(str(link.get("source")), {})
            if peer.get("kind") == "pc":
                expected_ports.append(str(link.get("target_port", "")).strip())
    expected_ports = [port for port in expected_ports if port and port.upper() != "UNMAPPED"]
    preflight_errors: list[str] = []
    if plan.compatibility_status != CompatibilityStatus.exact:
        preflight_errors.append(plan.compatibility_reason or "型号或版本门禁未通过。")
    if not plan.approved_at:
        preflight_errors.append("该设备命令集尚未经过用户确认。")
    if validation.get("status") != "ready":
        preflight_errors.append("静态验证未通过。")
    if not _previous_device_is_complete(session, task, plan):
        preflight_errors.append("前一台设备尚未验证完成；禁止并行或跳过逐台确认。")
    if node.get("ssh_host") and node["ssh_host"] != host:
        preflight_errors.append("本次 SSH 地址与冻结拓扑不一致。")
    if node.get("ssh_port") and node["ssh_port"] != port:
        preflight_errors.append("本次 SSH 端口与冻结拓扑不一致。")
    allowed_ports = {port_identity(port) for port in expected_ports}
    if intent.get("feature") == "vlan_access" and not allowed_ports:
        preflight_errors.append("当前 VLAN Access 计划没有直连 PC 的允许端口；禁止写入。")
    preflight_errors.extend(
        _check_write_commands(
            commands,
            protected_ports,
            allowed_ports if intent.get("feature") == "vlan_access" else None,
        )
    )
    execution.preflight_json = _dump({"errors": preflight_errors, "protected_ports": sorted(protected_ports)})
    if preflight_errors:
        execution.status = ExecutionStatus.preflight_blocked
        execution.error_message = "；".join(preflight_errors)
        execution.finished_at = datetime.utcnow()
        session.commit()
        return execution

    execution.status = ExecutionStatus.running
    execution.started_at = datetime.utcnow()
    task.status = TaskStatus.executing
    session.commit()
    connection = ConnectHandler(
        device_type="huawei",
        host=host,
        port=port,
        username=username,
        password=password,
        conn_timeout=15,
        banner_timeout=20,
        auth_timeout=20,
        fast_cli=False,
    )
    try:
        sequence = 1
        # Re-read identity at the instant before a write. Series membership is the
        # confirmed compatibility policy; the release is audit data, not a gate.
        version_output = connection.send_command("display version", read_timeout=30)
        _record(
            session,
            execution,
            sequence=sequence,
            phase="preflight",
            command="display version",
            output=version_output,
            success=not _command_failed(version_output),
        )
        sequence += 1
        current_model, _current_release = _parse_version(version_output)
        manual = session.get(Manual, task.manual_id)
        expected_series = (plan.mapped_series or "").upper()
        current_resolution = resolve_series_for_model(
            session,
            model_name=current_model,
            brand=manual.brand if manual else None,
            covered_series=_manual_series_coverage(session, task.manual_id),
        )
        if not current_resolution or not expected_series or current_resolution.series != expected_series:
            raise ValueError("设备当前型号未归属已审批的手册系列；已停止且未发送配置。")
        for command in commands:
            output = connection.send_command_timing(command, read_timeout=30)
            success = not _command_failed(output)
            _record(
                session,
                execution,
                sequence=sequence,
                phase="configure",
                command=command,
                output=output,
                success=success,
            )
            sequence += 1
            if not success:
                execution.status = ExecutionStatus.command_failed
                execution.error_message = f"设备拒绝命令：{command}"
                execution.finished_at = datetime.utcnow()
                session.commit()
                return execution
        validation_results: list[dict[str, str | bool | list[str]]] = []
        validation_outputs: dict[str, str] = {}
        for command in validation.get("validation_commands", []):
            output = connection.send_command(command, read_timeout=30)
            success = not _command_failed(output)
            validation_outputs[command] = output
            validation_results.append({"command": command, "success": success})
            _record(
                session,
                execution,
                sequence=sequence,
                phase="validate",
                command=command,
                output=output,
                success=success,
            )
            sequence += 1
        if intent.get("feature") == "vlan_access":
            device_ok, missing = _validate_vlan_access_output(
                "\n".join(validation_outputs.values()),
                vlan_ids=[int(item) for item in intent.get("vlan_ids", [])],
                expected_ports=expected_ports,
            )
            validation_results.append(
                {
                    "command": "expected_state_assertion",
                    "success": device_ok,
                    "missing": missing,
                }
            )
        execution.validation_json = _dump({"checks": validation_results})
        if not all(item["success"] for item in validation_results):
            execution.status = ExecutionStatus.validation_failed
            execution.error_message = "设备侧验证命令出现错误；未执行 save。"
            execution.finished_at = datetime.utcnow()
            session.commit()
            return execution
        # User confirmed the whole device command set during approval. Per the
        # accepted policy, save is a separate interaction only after validation.
        save_output = _complete_huawei_save(connection)
        save_success = _save_completed(save_output)
        _record(
            session,
            execution,
            sequence=sequence,
            phase="save",
            command="save",
            output=save_output,
            success=save_success,
        )
        execution.save_json = _dump(
            {"attempted": True, "success": save_success, "completion_detected": save_success}
        )
        if not save_success:
            execution.status = ExecutionStatus.failed
            execution.error_message = "验证通过，但 save 返回错误；请人工检查运行配置与保存状态。"
        else:
            execution.status = ExecutionStatus.completed
            task.status = TaskStatus.completed
        execution.finished_at = datetime.utcnow()
        session.commit()
        return execution
    except Exception as exc:
        execution.status = ExecutionStatus.failed
        execution.error_message = str(exc)[:4000]
        execution.finished_at = datetime.utcnow()
        session.commit()
        return execution
    finally:
        connection.disconnect()
