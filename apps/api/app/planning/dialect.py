"""Manual-selected CLI dialects used by the capability-neutral planner.

The selected manual remains the source of command syntax. A dialect defines
session conventions plus a small number of device-CLI interaction semantics;
it must never decide which network feature the planner is allowed to configure.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CliDialect:
    key: str
    label: str
    configuration_enter: tuple[str, ...]
    configuration_exit: tuple[str, ...]
    control_commands: frozenset[str]
    read_only_prefixes: tuple[str, ...]
    supports_huawei_vlan_renderer: bool = False
    netmiko_device_type: str | None = None
    requires_explicit_interface_exit: bool = False
    preserves_topology_port_spelling: bool = False
    l3_physical_interface_conversion_command: str | None = None
    l3_physical_interface_conversion_evidence: str | None = None
    l3_virtual_interface_conversion_prefixes: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "configuration_enter": list(self.configuration_enter),
            "configuration_exit": list(self.configuration_exit),
            "read_only_prefixes": list(self.read_only_prefixes),
            "execution_driver": self.netmiko_device_type,
            "requires_explicit_interface_exit": self.requires_explicit_interface_exit,
            "preserves_topology_port_spelling": self.preserves_topology_port_spelling,
            "l3_physical_interface_conversion_command": self.l3_physical_interface_conversion_command,
            "l3_virtual_interface_conversion_prefixes": list(self.l3_virtual_interface_conversion_prefixes),
            "supports_huawei_vlan_renderer": self.supports_huawei_vlan_renderer,
        }


GENERIC_MANUAL = CliDialect(
    key="generic_manual",
    label="通用手册 CLI（不注入厂商会话命令）",
    configuration_enter=(),
    configuration_exit=(),
    control_commands=frozenset(),
    read_only_prefixes=("show ", "display ", "ping ", "traceroute "),
)

HUAWEI_VRP = CliDialect(
    key="huawei_vrp",
    label="Huawei VRP",
    configuration_enter=("system-view",),
    configuration_exit=("return",),
    control_commands=frozenset({"quit"}),
    read_only_prefixes=("display ", "ping ", "tracert "),
    supports_huawei_vlan_renderer=True,
    netmiko_device_type="huawei",
    requires_explicit_interface_exit=True,
    preserves_topology_port_spelling=True,
    l3_physical_interface_conversion_command="undo portswitch",
    l3_physical_interface_conversion_evidence="portswitch",
    l3_virtual_interface_conversion_prefixes=("eth-trunk ",),
)

H3C_COMWARE = CliDialect(
    key="h3c_comware",
    label="H3C Comware",
    configuration_enter=("system-view",),
    configuration_exit=("return",),
    control_commands=frozenset({"quit"}),
    read_only_prefixes=("display ", "ping ", "tracert "),
    netmiko_device_type="hp_comware",
    requires_explicit_interface_exit=True,
)

CISCO_IOS = CliDialect(
    key="cisco_ios",
    label="Cisco IOS",
    configuration_enter=("configure terminal",),
    configuration_exit=("end",),
    control_commands=frozenset({"exit"}),
    read_only_prefixes=("show ", "ping ", "traceroute "),
    netmiko_device_type="cisco_ios",
)

ARISTA_EOS = CliDialect(
    key="arista_eos",
    label="Arista EOS",
    configuration_enter=("configure terminal",),
    configuration_exit=("end",),
    control_commands=frozenset({"exit"}),
    read_only_prefixes=("show ", "ping ", "traceroute "),
    netmiko_device_type="arista_eos",
)

PROFILES = {
    "generic_manual": GENERIC_MANUAL,
    "huawei_vrp": HUAWEI_VRP,
    "h3c_comware": H3C_COMWARE,
    "cisco_ios": CISCO_IOS,
    "arista_eos": ARISTA_EOS,
}

CLI_PROFILE_CHOICES = tuple(["auto", *PROFILES])


def resolve_cli_dialect(profile: str | None, brand: str | None) -> CliDialect:
    """Resolve an explicit profile first, then apply a deliberately narrow auto rule."""

    normalized_profile = (profile or "auto").strip().casefold()
    if normalized_profile in PROFILES:
        return PROFILES[normalized_profile]

    normalized_brand = (brand or "").strip().casefold()
    if "huawei" in normalized_brand or "华为" in normalized_brand:
        return HUAWEI_VRP
    if "h3c" in normalized_brand or "新华三" in normalized_brand:
        return H3C_COMWARE
    if "cisco" in normalized_brand or "思科" in normalized_brand:
        return CISCO_IOS
    if "arista" in normalized_brand:
        return ARISTA_EOS
    return GENERIC_MANUAL


def is_huawei_vlan_renderer(intent: dict[str, object], dialect: CliDialect) -> bool:
    """Keep the Huawei-specific deterministic renderer an explicit opt-in."""

    return (
        dialect.supports_huawei_vlan_renderer
        and intent.get("renderer_mode", "huawei_vlan") == "huawei_vlan"
        and intent.get("feature") in {"vlan_access", "multi_vlan_intervlan"}
    )
