from __future__ import annotations

import re
from dataclasses import dataclass

from netmiko import ConnectHandler


@dataclass(frozen=True)
class PingResult:
    command: str
    output: str
    success: bool


def _ping_command(os_family: str, target_ip: str) -> str:
    if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", target_ip):
        raise ValueError("仅允许 IPv4 目标地址。")
    if os_family == "linux":
        return f"ping -c 4 -W 2 {target_ip}"
    if os_family == "windows":
        return f"ping -n 4 -w 2000 {target_ip}"
    raise ValueError("PC SSH 验收仅支持 linux 或 windows；不猜测命令格式。")


def run_pc_ping(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    os_family: str,
    target_ip: str,
) -> PingResult:
    """Run the one allowed PC acceptance command; credentials are transient."""

    command = _ping_command(os_family, target_ip)
    device_type = "linux" if os_family == "linux" else "terminal_server"
    connection = ConnectHandler(
        device_type=device_type,
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
        output = connection.send_command_timing(command, read_timeout=30)
    finally:
        connection.disconnect()
    success = bool(re.search(r"(?:0%\s*packet loss|0%\s*loss|Received = [1-9])", output, re.IGNORECASE))
    return PingResult(command=command, output=output, success=success)
