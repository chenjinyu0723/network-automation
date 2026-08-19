from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from netmiko import ConnectHandler
from sqlalchemy.orm import Session

from app.models import (
    CompatibilityStatus,
    ConfigTask,
    DevicePlan,
    ExecutionCommand,
    ExecutionRun,
    ExecutionStatus,
    TaskStatus,
    new_id,
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


def _send_config_set_once(connection, commands: list[str]) -> str:  # type: ignore[no-untyped-def]
    """Send the complete approved block once and return Netmiko's full echo."""

    sender = getattr(connection, "send_config_set", None)
    if sender is not None:
        return str(
            sender(
                commands,
                enter_config_mode=False,
                exit_config_mode=False,
                read_timeout=30,
            )
            or ""
        )
    # Lightweight test doubles and older adapters may not implement the
    # aggregate API. Production Netmiko always takes the one-shot path above.
    return "\n".join(
        str(connection.send_command_timing(command, read_timeout=30) or "") for command in commands
    )


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
            # Keep Netmiko's complete response. The UI renders this as one
            # ordered terminal stream, so truncating the tail would hide the
            # beginning of a device prompt or command echo.
            output=output,
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
            # Preserve the complete device response. The client presents all
            # records as one ordered terminal stream, without synthetic
            # phase/command labels or tail truncation.
            output=output,
            success=success,
        )
    )
    session.commit()


def _queue_execution(
    session: Session,
    *,
    task: ConfigTask,
    plan: DevicePlan,
    host: str,
    port: int,
    execution_id: str | None,
    operation: str,
) -> ExecutionRun:
    identifier = execution_id or new_id()
    if session.get(ExecutionRun, identifier):
        raise ValueError("执行记录 ID 已存在，请重新提交。")
    execution = ExecutionRun(
        id=identifier,
        task_id=task.id,
        device_plan_id=plan.id,
        target_host=host,
        target_port=port,
        execution_revision=plan.approval_revision,
        operation=operation,
        status=ExecutionStatus.queued,
    )
    session.add(execution)
    session.commit()
    session.refresh(execution)
    return execution


def queue_huawei_device_plan(
    session: Session,
    *,
    task_id: str,
    plan_id: str,
    host: str,
    port: int,
    execution_id: str | None = None,
) -> ExecutionRun:
    task = session.get(ConfigTask, task_id)
    plan = session.get(DevicePlan, plan_id)
    if not task or not plan or plan.task_id != task.id:
        raise ValueError("配置任务或设备计划不存在。")
    return _queue_execution(
        session,
        task=task,
        plan=plan,
        host=host,
        port=port,
        execution_id=execution_id,
        operation="apply",
    )


def queue_huawei_undo_plan(
    session: Session,
    *,
    task_id: str,
    plan_id: str,
    host: str,
    port: int,
    execution_id: str | None = None,
) -> ExecutionRun:
    task = session.get(ConfigTask, task_id)
    plan = session.get(DevicePlan, plan_id)
    if not task or not plan or plan.task_id != task.id:
        raise ValueError("配置任务或设备计划不存在。")
    rollback = _load(plan.rollback_json)
    if rollback.get("level") != "conditional" or not rollback.get("commands"):
        raise ValueError("当前命令集没有可自动执行的受限回滚草案。")
    if not any(
        item.status == ExecutionStatus.completed and item.operation == "apply"
        for item in plan.executions
    ):
        raise ValueError("只有成功下发过当前设备命令集后才可以执行 Undo。")
    return _queue_execution(
        session,
        task=task,
        plan=plan,
        host=host,
        port=port,
        execution_id=execution_id,
        operation="undo",
    )


def execute_huawei_device_plan(
    session: Session,
    *,
    task_id: str,
    plan_id: str,
    host: str,
    port: int,
    username: str,
    password: str,
    execution_id: str | None = None,
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
    execution = session.get(ExecutionRun, execution_id) if execution_id else None
    if execution:
        if (
            execution.task_id != task.id
            or execution.device_plan_id != plan.id
            or execution.operation != "apply"
            or execution.status != ExecutionStatus.queued
        ):
            raise ValueError("执行记录状态与当前下发请求不匹配。")
    else:
        execution = _queue_execution(
            session,
            task=task,
            plan=plan,
            host=host,
            port=port,
            execution_id=None,
            operation="apply",
        )

    protected_ports = {port_identity(str(item)) for item in node.get("protected_ports", [])}
    commands, ignored_after_return = _normalize_config_block(_load(plan.commands_json))
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
    if plan.compatibility_status not in {
        CompatibilityStatus.manual_selected,
        CompatibilityStatus.exact,
    }:
        preflight_errors.append(plan.compatibility_reason or "当前计划未绑定可用手册上下文。")
    if not plan.approved_at:
        preflight_errors.append("该设备命令集尚未经过用户确认。")
    preflight_errors.extend(
        _check_write_commands(
            commands,
            protected_ports,
            None,
        )
    )
    execution.preflight_json = _dump(
        {
            "errors": preflight_errors,
            "protected_ports": sorted(protected_ports),
            "ignored_after_return": ignored_after_return,
        }
    )
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
    connection = None
    sequence = 1
    try:
        if ignored_after_return:
            _record(
                session,
                execution,
                sequence=sequence,
                phase="preflight",
                command="配置块边界检查",
                output=(
                    f"检测到 return 后仍有 {len(ignored_after_return)} 条命令，"
                    "已忽略；实际下发块以第一个 return 结束。\n"
                    + "\n".join(ignored_after_return)
                ),
                success=True,
            )
            sequence += 1
        _record(
            session,
            execution,
            sequence=sequence,
            phase="connect",
            command="SSH connect",
            output="正在建立 SSH 连接。",
            success=True,
        )
        sequence += 1
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
        _record(
            session,
            execution,
            sequence=sequence,
            phase="connect",
            command="SSH connect",
            output="SSH 已连接，开始执行设备侧预检。",
            success=True,
        )
        sequence += 1
        # Capture current identity immediately before writing for audit only. The
        # operator-selected manual is the command context; no model-tree or
        # series inference may block an otherwise approved, validated plan.
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
        _record(
            session,
            execution,
            sequence=sequence,
            phase="configure",
            command="Netmiko send_config_set",
            output="正在一次性发送完整配置块，等待设备返回完整回显。",
            success=True,
        )
        sequence += 1
        output = _send_config_set_once(connection, commands)
        success = not _command_failed(output)
        _record(
            session,
            execution,
            sequence=sequence,
            phase="configure",
            command="Netmiko send_config_set 回显",
            output=output,
            success=success,
        )
        sequence += 1
        if not success:
            execution.status = ExecutionStatus.command_failed
            execution.error_message = "设备拒绝配置块"
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
        _record(
            session,
            execution,
            sequence=sequence,
            phase="error",
            command="SSH/执行异常",
            output=str(exc),
            success=False,
        )
        execution.status = ExecutionStatus.failed
        execution.error_message = str(exc)[:4000]
        execution.finished_at = datetime.utcnow()
        session.commit()
        return execution
    finally:
        if connection:
            connection.disconnect()


def _check_undo_commands(
    commands: list[str],
    *,
    protected_ports: set[str],
    allowed_ports: set[str],
) -> list[str]:
    """Only permit the narrow rollback form produced for the VLAN Access feature."""

    errors: list[str] = []
    current_port: str | None = None
    saw_system_view = False
    saw_vlan_undo = False
    for command in commands:
        normalized = " ".join(str(command).strip().split())
        if normalized.lower() == "system-view":
            saw_system_view = True
            continue
        if normalized.lower() in {"quit", "return"}:
            continue
        interface_match = re.fullmatch(r"interface\s+(.+)", normalized, re.IGNORECASE)
        if interface_match:
            current_port = port_identity(interface_match.group(1))
            if current_port in protected_ports:
                errors.append(f"Undo 尝试进入受保护端口 {current_port}")
            elif current_port not in allowed_ports:
                errors.append(f"Undo 尝试进入当前设备 PC 链路范围外端口 {current_port}")
            continue
        if re.fullmatch(r"undo\s+port\s+default\s+vlan\s+[1-9]\d{0,3}", normalized, re.IGNORECASE):
            if not current_port:
                errors.append("Undo 端口命令缺少 interface 上下文")
            continue
        if re.fullmatch(r"undo\s+vlan\s+batch\s+[1-9]\d{0,3}", normalized, re.IGNORECASE):
            saw_vlan_undo = True
            continue
        errors.append(f"Undo 命令不在受限回滚语法范围内：{normalized}")
    if not saw_system_view:
        errors.append("Undo 缺少 system-view")
    if not saw_vlan_undo:
        errors.append("Undo 缺少 undo vlan batch")
    return errors


def _normalize_config_block(commands: list[str]) -> tuple[list[str], list[str]]:
    """Stop a submitted Huawei command block at its first standalone ``return``.

    The command plan is editable text.  If a stale fragment remains below the
    final ``return``, Netmiko must not silently send it as part of the same
    batch.  The omitted tail is preserved in the execution preflight record
    and terminal stream so the operator can see exactly what was skipped.
    """

    normalized = [str(item).strip() for item in commands if str(item).strip()]
    for index, command in enumerate(normalized):
        if command.casefold() == "return":
            return normalized[: index + 1], normalized[index + 1 :]
    return normalized, []


def execute_huawei_undo_plan(
    session: Session,
    *,
    task_id: str,
    plan_id: str,
    host: str,
    port: int,
    username: str,
    password: str,
    execution_id: str,
) -> ExecutionRun:
    """Apply the approved narrow rollback draft for one already-successful device run."""

    task = session.get(ConfigTask, task_id)
    plan = session.get(DevicePlan, plan_id)
    execution = session.get(ExecutionRun, execution_id)
    if not task or not plan or plan.task_id != task.id or not execution:
        raise ValueError("配置任务、设备计划或 Undo 执行记录不存在。")
    if (
        execution.task_id != task.id
        or execution.device_plan_id != plan.id
        or execution.operation != "undo"
        or execution.status != ExecutionStatus.queued
    ):
        raise ValueError("Undo 执行记录状态与当前请求不匹配。")
    _ = list(plan.executions)
    if not any(
        item.status == ExecutionStatus.completed and item.operation == "apply"
        for item in plan.executions
    ):
        raise ValueError("没有可回滚的成功下发记录。")

    rollback = _load(plan.rollback_json)
    commands, ignored_after_return = _normalize_config_block(
        [str(item).strip() for item in rollback.get("commands", []) if str(item).strip()]
    )
    node = _topology_node(task, plan.device_node_id)
    graph = _load(task.topology_revision.graph_json)
    nodes_by_id = {str(item.get("id")): item for item in graph.get("nodes", [])}
    pc_ports: set[str] = set()
    for link in graph.get("links", []):
        if link.get("source") == plan.device_node_id:
            peer = nodes_by_id.get(str(link.get("target")), {})
            candidate = str(link.get("source_port", "")).strip()
        elif link.get("target") == plan.device_node_id:
            peer = nodes_by_id.get(str(link.get("source")), {})
            candidate = str(link.get("target_port", "")).strip()
        else:
            continue
        if peer.get("kind") == "pc" and candidate and candidate.upper() != "UNMAPPED":
            pc_ports.add(port_identity(candidate))
    protected_ports = {port_identity(str(item)) for item in node.get("protected_ports", [])}
    preflight_errors: list[str] = []
    if rollback.get("level") != "conditional":
        preflight_errors.append("当前计划没有可执行的受限回滚级别。")
    preflight_errors.extend(
        _check_undo_commands(
            commands,
            protected_ports=protected_ports,
            allowed_ports=pc_ports,
        )
    )
    execution.preflight_json = _dump(
        {
            "errors": preflight_errors,
            "protected_ports": sorted(protected_ports),
            "allowed_undo_ports": sorted(pc_ports),
            "rollback_reason": rollback.get("reason"),
            "ignored_after_return": ignored_after_return,
        }
    )
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
    connection = None
    sequence = 1
    try:
        if ignored_after_return:
            _record(
                session,
                execution,
                sequence=sequence,
                phase="preflight",
                command="Undo 配置块边界检查",
                output=(
                    f"检测到 return 后仍有 {len(ignored_after_return)} 条 Undo 命令，"
                    "已忽略；实际下发块以第一个 return 结束。\n"
                    + "\n".join(ignored_after_return)
                ),
                success=True,
            )
            sequence += 1
        _record(
            session,
            execution,
            sequence=sequence,
            phase="connect",
            command="SSH connect",
            output="正在建立 SSH 连接，准备执行 Undo。",
            success=True,
        )
        sequence += 1
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
        _record(
            session,
            execution,
            sequence=sequence,
            phase="connect",
            command="SSH connect",
            output="SSH 已连接，开始执行受限回滚命令。",
            success=True,
        )
        sequence += 1
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
        _record(
            session,
            execution,
            sequence=sequence,
            phase="undo",
            command="Netmiko send_config_set (Undo)",
            output="正在一次性发送完整 Undo 配置块，等待设备返回完整回显。",
            success=True,
        )
        sequence += 1
        output = _send_config_set_once(connection, commands)
        success = not _command_failed(output)
        _record(
            session,
            execution,
            sequence=sequence,
            phase="undo",
            command="Netmiko send_config_set (Undo) 回显",
            output=output,
            success=success,
        )
        sequence += 1
        if not success:
            execution.status = ExecutionStatus.command_failed
            execution.error_message = "设备拒绝 Undo 配置块"
            execution.finished_at = datetime.utcnow()
            session.commit()
            return execution
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
            {
                "attempted": True,
                "success": save_success,
                "completion_detected": save_success,
                "operation": "undo",
            }
        )
        execution.status = ExecutionStatus.completed if save_success else ExecutionStatus.failed
        execution.error_message = (
            None if save_success else "Undo 命令已执行，但 save 返回错误；请人工检查保存状态。"
        )
        if save_success:
            task.status = TaskStatus.approved
        execution.finished_at = datetime.utcnow()
        session.commit()
        return execution
    except Exception as exc:
        _record(
            session,
            execution,
            sequence=sequence,
            phase="error",
            command="Undo/SSH 异常",
            output=str(exc),
            success=False,
        )
        execution.status = ExecutionStatus.failed
        execution.error_message = str(exc)[:4000]
        execution.finished_at = datetime.utcnow()
        session.commit()
        return execution
    finally:
        if connection:
            connection.disconnect()
