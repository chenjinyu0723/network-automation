from __future__ import annotations

import re
from dataclasses import dataclass

from netmiko import ConnectHandler

READ_ONLY_COMMANDS = {
    "display version",
    "display interface GigabitEthernet 0/0/1",
    "display current-configuration interface GigabitEthernet 0/0/1",
    "display port vlan GigabitEthernet 0/0/1",
    "display vlan 10",
}
BLOCKED_TOKENS = ("system-view", "save", "reset", "reboot", "undo", "delete", "format", "clear")


@dataclass(frozen=True)
class ProbeResult:
    command: str
    output: str
    detected_model: str | None
    detected_release: str | None
    warnings: list[str]


def _validate_read_only(command: str) -> None:
    normalized = " ".join(command.strip().split())
    if normalized not in READ_ONLY_COMMANDS:
        raise ValueError("只读探测只允许预定义白名单命令。")
    lowered = normalized.lower()
    if any(token in lowered for token in BLOCKED_TOKENS):
        raise ValueError("命令包含被阻断的配置或维护关键字。")


def _parse_version(output: str) -> tuple[str | None, str | None]:
    release_match = re.search(r"(V\d{3}R\d{3}C\d{2})", output, re.IGNORECASE)
    model_patterns = [
        r"(?:HUAWEI\s+)?([A-Z]\d{4}(?:[A-Z0-9-]+)?)\s+(?:uptime|Version|\()",
        r"(?:Device\s+)?(?:Model|Type)\s*[:：]\s*([A-Z]\d{4}[A-Z0-9-]+)",
        r"\b(S(?:17|57|67)\d{2}[A-Z0-9-]+)\b",
    ]
    model = None
    for pattern in model_patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            model = match.group(1).upper()
            break
    return model, release_match.group(1).upper() if release_match else None


def run_huawei_read_only_probe(
    *, host: str, port: int, username: str, password: str, command: str
) -> ProbeResult:
    _validate_read_only(command)
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
        output = connection.send_command(command, read_timeout=30)
    finally:
        connection.disconnect()
    model, release = _parse_version(output)
    warnings = [
        "本次仅执行白名单只读命令；未进入 system-view，未发送配置或 save。",
        "除 display version 外，不会查询或修改任何端口。",
    ]
    return ProbeResult(
        command=command,
        output=output,
        detected_model=model,
        detected_release=release,
        warnings=warnings,
    )
