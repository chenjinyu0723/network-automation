"""Run eight representative scenarios through the full planning workflow.

This is an isolated, non-executing regression.  Each base topology goes through
``create_config_task -> generate_config_commands`` with the configured local
LLM/Embedding providers, is checked against scenario-specific invariants, then
saved as an actual ``ConfigurationTemplate``.  A different topology of the
same class is subsequently planned with that template reference enabled.

It never opens an SSH session, calls Netmiko, or sends commands.  Run it only
against an isolated APP_DATA_DIR that already contains an injected handbook and
provider settings, for example::

    $env:APP_DATA_DIR = 'D:\\network-automation\\data\\ospf-integration-test'
    D:\\network-automation\\.venv\\Scripts\\python.exe scripts\\rerun_template_regression.py
"""

# Test cases intentionally preserve long natural-language requirements and CLI assertions.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.db import SessionLocal, init_database
from app.models import ConfigTask, Manual
from app.planning.service import create_config_task, create_topology, generate_config_commands
from app.schemas import ConfigTaskCreate, TopologyDraft
from app.template_service import create_template_from_task
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DATA_DIR = PROJECT_ROOT / "data" / "ospf-integration-test"


def _output_root() -> Path:
    """Keep reports with the isolated database selected for this regression."""

    return Path(os.getenv("APP_DATA_DIR", str(DEFAULT_OUTPUT_DATA_DIR))) / "scenario-template-regression"


@dataclass(frozen=True)
class ScenarioCase:
    key: str
    title: str
    requirement: str
    topology: dict[str, Any]
    required_fragments: tuple[str, ...]
    forbidden_fragments: tuple[str, ...]
    expected_switches: int = 1


@dataclass(frozen=True)
class ScenarioPair:
    key: str
    title: str
    base: ScenarioCase
    variant: ScenarioCase


def _switch(
    node_id: str,
    name: str,
    x: int,
    y: int,
    *,
    model: str = "S5700",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "switch",
        "name": name,
        "x": x,
        "y": y,
        "detected_model": model,
    }


def _endpoint(node_id: str, name: str, x: int, y: int) -> dict[str, Any]:
    """A non-managed endpoint keeps this a current-device planning slice."""

    return {"id": node_id, "kind": "pc", "name": name, "x": x, "y": y}


def _one_switch_topology(
    name: str,
    ports: list[str],
    *,
    model: str = "S5700",
    switch_name: str = "SW1",
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [_switch("sw1", switch_name, 80, 160, model=model)]
    links: list[dict[str, Any]] = []
    for index, port in enumerate(ports, start=1):
        endpoint_id = f"peer{index}"
        nodes.append(_endpoint(endpoint_id, f"已绘制对端 {index}", 480, index * 120))
        links.append(
            {
                "id": f"link{index}",
                "source": "sw1",
                "source_port": port,
                "target": endpoint_id,
                "target_port": "Ethernet0/0/1",
            }
        )
    return {"name": name, "nodes": nodes, "links": links}


def _vlan_topology(
    *,
    name: str,
    vlan_a: int,
    vlan_b: int,
    subnet_a: str,
    subnet_b: str,
    model: str,
    switch_names: tuple[str, str, str] = ("SW1", "SW2", "SW3"),
) -> dict[str, Any]:
    return {
        "name": name,
        "nodes": [
            _switch("sw1", switch_names[0], 60, 80, model=model),
            _switch("sw2", switch_names[1], 60, 340, model=model),
            _switch("sw3", switch_names[2], 450, 210, model=model),
            {
                **_endpoint("pc1", "PC1", 820, 40),
                "ip": f"{subnet_a}.11",
                "prefix": 24,
                "gateway": f"{subnet_a}.1",
            },
            {
                **_endpoint("pc2", "PC2", 820, 120),
                "ip": f"{subnet_b}.12",
                "prefix": 24,
                "gateway": f"{subnet_b}.1",
            },
            {
                **_endpoint("pc3", "PC3", 820, 300),
                "ip": f"{subnet_a}.13",
                "prefix": 24,
                "gateway": f"{subnet_a}.1",
            },
            {
                **_endpoint("pc4", "PC4", 820, 380),
                "ip": f"{subnet_b}.14",
                "prefix": 24,
                "gateway": f"{subnet_b}.1",
            },
        ],
        "links": [
            {
                "id": "pc1",
                "source": "sw1",
                "source_port": "GE0/0/1",
                "target": "pc1",
                "target_port": "Ethernet0/0/1",
            },
            {
                "id": "pc2",
                "source": "sw1",
                "source_port": "GE0/0/2",
                "target": "pc2",
                "target_port": "Ethernet0/0/1",
            },
            {
                "id": "u1",
                "source": "sw1",
                "source_port": "GE0/0/3",
                "target": "sw3",
                "target_port": "GE0/0/1",
            },
            {
                "id": "pc3",
                "source": "sw2",
                "source_port": "GE0/0/1",
                "target": "pc3",
                "target_port": "Ethernet0/0/1",
            },
            {
                "id": "pc4",
                "source": "sw2",
                "source_port": "GE0/0/2",
                "target": "pc4",
                "target_port": "Ethernet0/0/1",
            },
            {
                "id": "u2",
                "source": "sw2",
                "source_port": "GE0/0/3",
                "target": "sw3",
                "target_port": "GE0/0/2",
            },
        ],
    }


PAIRS: tuple[ScenarioPair, ...] = (
    ScenarioPair(
        key="ospf_triangle",
        title="三角三层 OSPF",
        base=ScenarioCase(
            key="ospf_triangle_base",
            title="三角三层 OSPF / SW1",
            requirement=(
                "SW1、SW2、SW3 构成三角形三层互联，所有链路启用 OSPF 进程 1、Area 0。"
                "当前设备为 SW1：GE0/0/1 接 SW2，配置 10.0.12.1/30；GE0/0/2 接 SW3，"
                "配置 10.0.13.1/30；router-id 为 1.1.1.1。不要配置 VLAN、二层 Trunk、"
                "管理 SSH 或未连线端口。"
            ),
            topology=_one_switch_topology("回归-OSPF-基础", ["GE0/0/1", "GE0/0/2"]),
            required_fragments=(
                "interface GE0/0/1",
                "undo portswitch",
                "ip address 10.0.12.1 255.255.255.252 || ip address 10.0.12.1 30",
                "interface GE0/0/2",
                "ip address 10.0.13.1 255.255.255.252 || ip address 10.0.13.1 30",
                "ospf 1",
                "router-id 1.1.1.1",
                "area 0",
                "network 10.0.12.0 0.0.0.3",
                "network 10.0.13.0 0.0.0.3",
            ),
            forbidden_fragments=("vlan ", "port link-type", "ssh ", "save", "reboot", "reset"),
        ),
        variant=ScenarioCase(
            key="ospf_triangle_variant",
            title="双上联 OSPF / S5735",
            requirement=(
                "汇聚交换机 Agg1 通过两条已绘制三层链路接入 OSPF 进程 10、Area 0。"
                "当前设备为 Agg1（S5735）：GE0/0/5 配置 172.16.15.1/30，GE0/0/6 配置 172.16.16.1/30，"
                "router-id 为 11.11.11.11，并发布两条直连网段。不要配置 VLAN、二层 Trunk、管理 SSH 或其他端口。"
            ),
            topology=_one_switch_topology(
                "回归-OSPF-变体", ["GE0/0/5", "GE0/0/6"], model="S5735", switch_name="Agg1"
            ),
            required_fragments=(
                "interface GE0/0/5",
                "undo portswitch",
                "ip address 172.16.15.1 255.255.255.252 || ip address 172.16.15.1 30",
                "interface GE0/0/6",
                "ip address 172.16.16.1 255.255.255.252 || ip address 172.16.16.1 30",
                "ospf 10",
                "router-id 11.11.11.11",
                "area 0",
                "network 172.16.15.0 0.0.0.3",
                "network 172.16.16.0 0.0.0.3",
            ),
            forbidden_fragments=("vlan ", "port link-type", "ssh ", "save", "reboot", "reset"),
        ),
    ),
    ScenarioPair(
        key="static_routing",
        title="双站点三层静态路由",
        base=ScenarioCase(
            key="static_routing_base",
            title="双站点三层静态路由 / SW1",
            requirement=(
                "SW1 与 SW2 通过 GE0/0/1 点对点互联。当前设备 SW1 的 GE0/0/1 已配置 10.0.12.1/30，"
                "对端 SW2 为 10.0.12.2/30。SW1 本地已存在 192.168.10.0/24，SW2 后方已有 192.168.20.0/24；"
                "请仅在 SW1 配置到 192.168.20.0/24 的静态路由。不要配置 VLAN、Trunk、SSH 或未连线端口。"
            ),
            topology=_one_switch_topology("回归-静态路由-基础", ["GE0/0/1"]),
            required_fragments=(
                "ip route-static 192.168.20.0 255.255.255.0 10.0.12.2 || ip route-static 192.168.20.0 24 10.0.12.2",
            ),
            forbidden_fragments=(
                "interface GE0/0/1",
                "undo portswitch",
                "ip address 10.0.12.1",
                "vlan ",
                "port link-type",
                "ssh ",
                "save",
                "reboot",
                "reset",
            ),
        ),
        variant=ScenarioCase(
            key="static_routing_variant",
            title="双站点备选网段静态路由 / S5755",
            requirement=(
                "Branch1 与 Branch2 通过已绘制的 GE0/0/3 点对点互联。当前设备 Branch1（S5755）在该口配置 "
                "172.20.12.1/30，对端为 172.20.12.2/30；Branch2 后方的 10.88.20.0/24 应经该对端访问。"
                "只配置此三层口和这一条静态路由，不要配置 VLAN、Trunk、SSH 或其他端口。"
            ),
            topology=_one_switch_topology(
                "回归-静态路由-变体", ["GE0/0/3"], model="S5755", switch_name="Branch1"
            ),
            required_fragments=(
                "interface GE0/0/3",
                "undo portswitch",
                "ip address 172.20.12.1 255.255.255.252 || ip address 172.20.12.1 30",
                "ip route-static 10.88.20.0 255.255.255.0 172.20.12.2 || ip route-static 10.88.20.0 24 172.20.12.2",
            ),
            forbidden_fragments=("vlan ", "port link-type", "ssh ", "save", "reboot", "reset"),
        ),
    ),
    ScenarioPair(
        key="lacp_mstp",
        title="双链路 LACP + MSTP",
        base=ScenarioCase(
            key="lacp_mstp_base",
            title="双链路 LACP + MSTP / SW1",
            requirement=(
                "SW1 与 SW2 之间使用 GE0/0/1、GE0/0/2 两条物理链路组成 Eth-Trunk 1，聚合模式为 LACP 静态。"
                "当前设备 SW1 需要启用生成树并使用 MSTP 模式。不要配置 VLAN、三层 IP、SSH 或其他端口。"
            ),
            topology=_one_switch_topology("回归-LACP-MSTP-基础", ["GE0/0/1", "GE0/0/2"]),
            required_fragments=(
                "interface Eth-Trunk 1",
                "mode lacp-static",
                "interface GE0/0/1",
                "eth-trunk 1",
                "interface GE0/0/2",
                "stp enable",
                "stp mode mstp",
            ),
            forbidden_fragments=(
                "vlan ",
                "ip address",
                "ssh ",
                "save",
                "reboot",
                "reset",
                "stp region-configuration",
                "region-name",
                "revision-level",
                "instance ",
            ),
        ),
        variant=ScenarioCase(
            key="lacp_mstp_variant",
            title="接入双链路 LACP + MSTP / S5735",
            requirement=(
                "Access1（S5735）到汇聚的已绘制 GE0/0/3、GE0/0/4 两条二层链路组成 Eth-Trunk 5，使用 LACP 静态。"
                "在当前设备启用生成树并设置为 MSTP 模式。不要配置 VLAN、三层 IP、SSH 或其他端口。"
            ),
            topology=_one_switch_topology(
                "回归-LACP-MSTP-变体", ["GE0/0/3", "GE0/0/4"], model="S5735", switch_name="Access1"
            ),
            required_fragments=(
                "interface Eth-Trunk 5",
                "mode lacp-static",
                "interface GE0/0/3",
                "eth-trunk 5",
                "interface GE0/0/4",
                "stp enable",
                "stp mode mstp",
            ),
            forbidden_fragments=(
                "vlan ",
                "ip address",
                "ssh ",
                "save",
                "reboot",
                "reset",
                "stp region-configuration",
                "region-name",
                "revision-level",
                "instance ",
            ),
        ),
    ),
    ScenarioPair(
        key="single_vlan_access",
        title="单交换机 Access VLAN 接入",
        base=ScenarioCase(
            key="single_vlan_access_base",
            title="单交换机 Access VLAN 接入 / SW1",
            requirement=(
                "PC1 通过已绘制的 GE0/0/1 接入 SW1，PC1 属于 VLAN 100。"
                "当前设备 SW1 仅创建 VLAN 100，并将 GE0/0/1 配置为 Access 口、PVID 为 VLAN 100。"
                "不要配置 Trunk、三层 IP、SSH、保存或其他端口。"
            ),
            topology=_one_switch_topology("回归-单VLAN-基础", ["GE0/0/1"]),
            required_fragments=(
                "vlan batch 100",
                "interface GE0/0/1",
                "port link-type access",
                "port default vlan 100",
            ),
            forbidden_fragments=("trunk", "ip address", "ssh ", "save", "reboot", "reset"),
        ),
        variant=ScenarioCase(
            key="single_vlan_access_variant",
            title="访客 Access VLAN 接入 / S5735",
            requirement=(
                "GuestPC 通过已绘制的 GE0/0/5 接入 Access1（S5735），GuestPC 属于 VLAN 200。"
                "当前设备 Access1 仅创建 VLAN 200，并将 GE0/0/5 配置为 Access 口、PVID 为 VLAN 200。"
                "不要配置 Trunk、三层 IP、SSH、保存或其他端口。"
            ),
            topology=_one_switch_topology(
                "回归-单VLAN-变体", ["GE0/0/5"], model="S5735", switch_name="Access1"
            ),
            required_fragments=(
                "vlan batch 200",
                "interface GE0/0/5",
                "port link-type access",
                "port default vlan 200",
            ),
            forbidden_fragments=("trunk", "ip address", "ssh ", "save", "reboot", "reset"),
        ),
    ),
    ScenarioPair(
        key="multi_vlan_intervlan",
        title="双接入双 VLAN 三层互通",
        base=ScenarioCase(
            key="multi_vlan_intervlan_base",
            title="双接入双 VLAN 三层互通",
            requirement=(
                "PC1 与 PC3 属于 VLAN 10，PC2 与 PC4 属于 VLAN 20。SW1、SW2 与 SW3 的交换机链路承载 VLAN 10 和 VLAN 20。"
                "SW3 是三层核心网关，使 VLAN 10 与 VLAN 20 之间三层互通。"
            ),
            topology=_vlan_topology(
                name="回归-双VLAN-基础",
                vlan_a=10,
                vlan_b=20,
                subnet_a="10.10.10",
                subnet_b="10.20.20",
                model="S5700",
            ),
            required_fragments=(
                "vlan batch 10 20",
                "port link-type access",
                "port default vlan 10",
                "port default vlan 20",
                "port link-type trunk",
                "port trunk allow-pass vlan 10 20",
                "interface Vlanif10",
                "ip address 10.10.10.1 255.255.255.0",
                "interface Vlanif20",
                "ip address 10.20.20.1 255.255.255.0",
            ),
            forbidden_fragments=("ssh ", "save", "reboot", "reset"),
            expected_switches=3,
        ),
        variant=ScenarioCase(
            key="multi_vlan_intervlan_variant",
            title="双接入双 VLAN 三层互通 / S5735",
            requirement=(
                "Office-PC1 与 Office-PC3 属于 VLAN 30，Office-PC2 与 Office-PC4 属于 VLAN 40。"
                "Access1、Access2 与 Core1 的交换机链路承载 VLAN 30 和 VLAN 40；Core1 是三层核心网关，"
                "使两个 VLAN 之间三层互通。"
            ),
            topology=_vlan_topology(
                name="回归-双VLAN-变体",
                vlan_a=30,
                vlan_b=40,
                subnet_a="172.30.30",
                subnet_b="172.40.40",
                model="S5735",
                switch_names=("Access1", "Access2", "Core1"),
            ),
            required_fragments=(
                "vlan batch 30 40",
                "port link-type access",
                "port default vlan 30",
                "port default vlan 40",
                "port link-type trunk",
                "port trunk allow-pass vlan 30 40",
                "interface Vlanif30",
                "ip address 172.30.30.1 255.255.255.0",
                "interface Vlanif40",
                "ip address 172.40.40.1 255.255.255.0",
            ),
            forbidden_fragments=("ssh ", "save", "reboot", "reset"),
            expected_switches=3,
        ),
    ),
    ScenarioPair(
        key="vrrp_gateway_redundancy",
        title="双核心 VRRP 网关冗余",
        base=ScenarioCase(
            key="vrrp_gateway_redundancy_base",
            title="双核心 VRRP 网关冗余 / Core1",
            requirement=(
                "Core1 与 Core2 已完成二层承载，接入网 VLAN 10 的用户网段为 192.168.10.0/24。当前设备 Core1 需要创建 VLAN 10，"
                "配置 Vlanif10 地址 192.168.10.2/24，并配置 VRRP 组 10 的虚拟网关 192.168.10.1、优先级 120，使其优先成为主网关。"
                "Core2 会独立配置 192.168.10.3/24、同一 VRRP 组和较低优先级。不要配置物理端口、Trunk、SSH、堆叠或任何路由协议。"
            ),
            topology=_one_switch_topology("回归-VRRP-基础", [], switch_name="Core1"),
            required_fragments=(
                "vlan batch 10",
                "interface Vlanif",
                "ip address 192.168.10.2 255.255.255.0 || ip address 192.168.10.2 24",
                "vrrp vrid 10 virtual-ip 192.168.10.1",
                "vrrp vrid 10 priority 120",
            ),
            forbidden_fragments=(
                "interface GE",
                "port link-type",
                "ssh ",
                "stack",
                "ospf",
                "save",
                "reboot",
                "reset",
            ),
        ),
        variant=ScenarioCase(
            key="vrrp_gateway_redundancy_variant",
            title="访客网 VRRP 网关冗余 / S5755",
            requirement=(
                "AggA 与 AggB 已完成二层承载。VLAN 30 的用户网段为 172.30.30.0/24。当前设备 AggA（S5755）需要创建 VLAN 30，"
                "配置 Vlanif30 地址 172.30.30.2/24，并配置 VRRP 组 30 的虚拟网关 172.30.30.1、优先级 130。"
                "AggB 由另一台设备计划配置为 172.30.30.3/24 和同一 VRRP 组。不要配置物理端口、Trunk、SSH、堆叠或路由协议。"
            ),
            topology=_one_switch_topology("回归-VRRP-变体", [], model="S5755", switch_name="AggA"),
            required_fragments=(
                "vlan batch 30",
                "interface Vlanif",
                "ip address 172.30.30.2 255.255.255.0 || ip address 172.30.30.2 24",
                "vrrp vrid 30 virtual-ip 172.30.30.1",
                "vrrp vrid 30 priority 130",
            ),
            forbidden_fragments=(
                "interface GE",
                "port link-type",
                "ssh ",
                "stack",
                "ospf",
                "save",
                "reboot",
                "reset",
            ),
        ),
    ),
    ScenarioPair(
        key="l3_eth_trunk",
        title="双链路 LACP 三层 Eth-Trunk",
        base=ScenarioCase(
            key="l3_eth_trunk_base",
            title="双链路 LACP 三层 Eth-Trunk / SW1",
            requirement=(
                "SW1 与 SW2 的 GE0/0/1、GE0/0/2 是两条已绘制的点对点链路。当前设备 SW1 需要将两口加入 Eth-Trunk 10，"
                "使用 LACP 静态模式；聚合口作为三层口，配置地址 10.0.12.1/30，对端 SW2 的聚合口地址为 10.0.12.2/30。"
                "不要配置 VLAN、Trunk 二层放通、STP、SSH 或其他端口。"
            ),
            topology=_one_switch_topology("回归-三层EthTrunk-基础", ["GE0/0/1", "GE0/0/2"]),
            required_fragments=(
                "interface Eth-Trunk 10",
                "mode lacp-static",
                "undo portswitch",
                "ip address 10.0.12.1 255.255.255.252 || ip address 10.0.12.1 30",
                "interface GE0/0/1",
                "eth-trunk 10",
                "interface GE0/0/2",
            ),
            forbidden_fragments=(
                "vlan ",
                "port link-type",
                "stp ",
                "ssh ",
                "save",
                "reboot",
                "reset",
                "GigabitEthernet",
            ),
        ),
        variant=ScenarioCase(
            key="l3_eth_trunk_variant",
            title="汇聚双链路三层 Eth-Trunk / S5735",
            requirement=(
                "Agg1（S5735）到 Agg2 的 GE0/0/5、GE0/0/6 是两条已绘制点对点链路。当前设备将两口加入 Eth-Trunk 20，"
                "使用 LACP 静态；逻辑聚合口切为三层口并配置 172.16.20.1/30，对端逻辑口为 172.16.20.2/30。"
                "不要配置 VLAN、二层 Trunk 放通、STP、SSH 或其他端口。"
            ),
            topology=_one_switch_topology(
                "回归-三层EthTrunk-变体", ["GE0/0/5", "GE0/0/6"], model="S5735", switch_name="Agg1"
            ),
            required_fragments=(
                "interface Eth-Trunk 20",
                "mode lacp-static",
                "undo portswitch",
                "ip address 172.16.20.1 255.255.255.252 || ip address 172.16.20.1 30",
                "interface GE0/0/5",
                "eth-trunk 20",
                "interface GE0/0/6",
            ),
            forbidden_fragments=(
                "vlan ",
                "port link-type",
                "stp ",
                "ssh ",
                "save",
                "reboot",
                "reset",
                "GigabitEthernet",
            ),
        ),
    ),
    ScenarioPair(
        key="istack_planning",
        title="双机 iStack 环形堆叠",
        base=ScenarioCase(
            key="istack_planning_base",
            title="双机 iStack 环形堆叠 / Member1",
            requirement=(
                "两台完全相同、已确认支持 iStack 的 S5700 组成双机环形堆叠。当前设备 Member1 使用 10GE1/0/1 加入 Stack-Port 1、"
                "10GE1/0/2 加入 Stack-Port 2；将本机成员 1 的堆叠优先级设为 150，使其优先参与主设备选举。"
                "对端 Member2 会独立执行对应配置。不要修改成员 ID 或 Domain ID，不要保存、重启、复位、配置 VLAN、IP、SSH 或非这两条堆叠链路。"
            ),
            topology=_one_switch_topology(
                "回归-iStack-基础", ["10GE1/0/1", "10GE1/0/2"], switch_name="Member1"
            ),
            required_fragments=(
                "stack",
                "stack member 1 priority 150",
                "interface 10GE1/0/1",
                "stack-port 1",
                "interface 10GE1/0/2",
                "stack-port 2",
            ),
            forbidden_fragments=(
                "stack-port 1/1",
                "stack-port 1/2",
                "port link-type stack",
                "vlan ",
                "ip address",
                "ssh ",
                "save",
                "reboot",
                "reset",
            ),
        ),
        variant=ScenarioCase(
            key="istack_planning_variant",
            title="双机 iStack 环形堆叠 / S5735",
            requirement=(
                "两台相同、已确认支持 iStack 的 S5735 组成双机环形堆叠。当前设备 MemberA 使用 10GE1/0/3 加入 Stack-Port 1、"
                "10GE1/0/4 加入 Stack-Port 2，并将成员 1 的堆叠优先级设为 160。对端由独立计划处理。"
                "不要修改成员 ID 或 Domain ID，不要保存、重启、复位、配置 VLAN、IP、SSH 或非这两条堆叠链路。"
            ),
            topology=_one_switch_topology(
                "回归-iStack-变体", ["10GE1/0/3", "10GE1/0/4"], model="S5735", switch_name="MemberA"
            ),
            required_fragments=(
                "stack",
                "stack member 1 priority 160",
                "interface 10GE1/0/3",
                "stack-port 1",
                "interface 10GE1/0/4",
                "stack-port 2",
            ),
            forbidden_fragments=(
                "stack-port 1/1",
                "stack-port 1/2",
                "port link-type stack",
                "vlan ",
                "ip address",
                "ssh ",
                "save",
                "reboot",
                "reset",
            ),
        ),
    ),
)


def _loads(raw: str) -> dict[str, Any]:
    return json.loads(raw) if raw else {}


def _task_snapshot(task: ConfigTask) -> dict[str, Any]:
    plans: list[dict[str, Any]] = []
    for plan in task.device_plans:
        intent = _loads(plan.intent_json)
        plans.append(
            {
                "device": plan.display_name,
                "model": plan.detected_model,
                "series": plan.mapped_series,
                "compatibility": plan.compatibility_status.value,
                "commands": json.loads(plan.commands_json),
                "validation": _loads(plan.validation_json),
                "review": dict(intent.get("llm_command_review", {})),
                "retrieval": dict(intent.get("retrieval_audit", {})),
            }
        )
    return {
        "task_id": task.id,
        "status": task.status.value,
        "planning_idea": task.planning_idea,
        "task_intent": _loads(task.intent_json),
        "plans": plans,
    }


def _quality(case: ScenarioCase, task: ConfigTask, *, expects_template: bool) -> dict[str, Any]:
    snapshot = _task_snapshot(task)
    all_commands = "\n".join(command for plan in snapshot["plans"] for command in plan["commands"]).casefold()
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    check(
        "任务状态",
        snapshot["status"] == "needs_review",
        f"实际为 {snapshot['status']}",
    )
    check(
        "设备数量",
        len(snapshot["plans"]) == case.expected_switches,
        f"实际为 {len(snapshot['plans'])}，期望为 {case.expected_switches}",
    )
    for plan in snapshot["plans"]:
        validation = plan["validation"]
        validation_errors = [str(item) for item in validation.get("errors", [])]
        evidence_only_errors = all(
            item.startswith("CLI 与引用手册命令前缀不一致：")
            for item in validation_errors
        )
        reviewable_draft = (
            validation.get("status") == "draft_with_warnings"
            and bool(plan["commands"])
            and bool(validation_errors)
            and evidence_only_errors
        )
        check(
            f"{plan['device']} 静态手册校验",
            (validation.get("status") == "ready" and not validation_errors) or reviewable_draft,
            (
                f"status={validation.get('status')} errors={validation_errors}"
                if not reviewable_draft
                else (
                    "命令草案完整；以下额外 CLI 未能绑定已注入手册页，"
                    "已保留给用户审阅，不作为自动下发或本回归的硬拦截。"
                )
            ),
        )
        review = dict(plan["review"].get("review", {}))
        check(
            f"{plan['device']} 独立 LLM 审阅（仅记录）",
            True,
            (
                f"verdict={review.get('verdict')} issues={review.get('issues', [])}；"
                "审阅为咨询信息，不作为回归质量门或用户命令下发门。"
                if review.get("verdict")
                else "模型未返回可解析审阅 JSON；不作为回归质量门或用户命令下发门。"
            ),
        )
        for command in plan["commands"]:
            normalized = " ".join(str(command).split()).casefold()
            # A standalone root with required arguments is not a configuration
            # command.  Keep this explicit list small and generic enough to
            # cover the command families exercised by this 8x2 suite; the
            # compiler test above protects the underlying automatic path.
            bare_parameterised_roots = {"ip address", "vlan batch", "stack-port"}
            check(
                f"{plan['device']} 非裸参数化命令：{command}",
                normalized not in bare_parameterised_roots,
                "命令参数完整" if normalized not in bare_parameterised_roots else "发现缺少必填参数的裸命令",
            )
    for fragment in case.required_fragments:
        alternatives = [item.strip() for item in fragment.split(" || ") if item.strip()]
        found = any(item.casefold() in all_commands for item in alternatives)
        check(
            f"关键命令：{' 或 '.join(alternatives)}",
            found,
            "已出现" if found else "缺失",
        )
    for fragment in case.forbidden_fragments:
        check(
            f"禁止命令：{fragment}",
            fragment.casefold() not in all_commands,
            "未出现" if fragment.casefold() not in all_commands else "出现了禁止片段",
        )
    template_reference = snapshot["task_intent"].get("template_reference", {})
    if expects_template:
        check(
            "模板参考已传入意图节点",
            bool(template_reference.get("template_id") and template_reference.get("reference_planning_idea")),
            f"template_id={template_reference.get('template_id')}",
        )
    return {
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "snapshot": snapshot,
    }


def _run_case(
    session,
    *,
    manual: Manual,
    case: ScenarioCase,
    template_id: str | None = None,
    stream_events: bool = False,
) -> dict[str, Any]:
    stage_marks: list[dict[str, Any]] = []
    started = time.perf_counter()
    revision = create_topology(session, TopologyDraft.model_validate(case.topology))
    topology_seconds = time.perf_counter() - started
    idea_started = time.perf_counter()
    task = create_config_task(
        session,
        ConfigTaskCreate(
            topology_revision_id=revision.id,
            manual_id=manual.id,
            template_id=template_id,
            requirement_text=case.requirement,
        ),
        event_sink=(
            lambda stage, event_type, content: stage_marks.append(
                {
                    "at_seconds": round(time.perf_counter() - started, 3),
                    "stage": stage,
                    "type": event_type,
                    "content": content[:600],
                }
            )
        )
        if stream_events
        else None,
    )
    idea_seconds = time.perf_counter() - idea_started
    command_started = time.perf_counter()
    task = generate_config_commands(
        session,
        task.id,
        event_sink=(
            lambda stage, event_type, content: stage_marks.append(
                {
                    "at_seconds": round(time.perf_counter() - started, 3),
                    "stage": stage,
                    "type": event_type,
                    "content": content[:600],
                }
            )
        )
        if stream_events
        else None,
    )
    command_seconds = time.perf_counter() - command_started
    quality = _quality(case, task, expects_template=template_id is not None)
    return {
        "case": {"key": case.key, "title": case.title, "requirement": case.requirement},
        "topology_revision_id": revision.id,
        "timing_seconds": {
            "topology_persist": round(topology_seconds, 3),
            "planning_idea": round(idea_seconds, 3),
            "retrieval_command_review": round(command_seconds, 3),
            "total": round(time.perf_counter() - started, 3),
        },
        "events": stage_marks,
        "quality": quality,
    }


def _run_case_safely(
    session,
    *,
    manual: Manual,
    case: ScenarioCase,
    template_id: str | None = None,
    stream_events: bool = False,
) -> dict[str, Any]:
    """Keep the full regression report intact when one provider call fails."""

    started = time.perf_counter()
    try:
        return _run_case(
            session,
            manual=manual,
            case=case,
            template_id=template_id,
            stream_events=stream_events,
        )
    except Exception as exc:  # The report must record, not hide, provider faults.
        elapsed = round(time.perf_counter() - started, 3)
        snapshot = {
            "task_id": "未创建或未完成",
            "status": "failed",
            "planning_idea": "",
            "task_intent": {},
            "plans": [],
        }
        return {
            "case": {"key": case.key, "title": case.title, "requirement": case.requirement},
            "topology_revision_id": "未完成",
            "timing_seconds": {
                "topology_persist": 0.0,
                "planning_idea": 0.0,
                "retrieval_command_review": 0.0,
                "total": elapsed,
            },
            "events": [],
            "quality": {
                "passed": False,
                "checks": [
                    {
                        "name": "全流程执行",
                        "passed": False,
                        "detail": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                ],
                "snapshot": snapshot,
            },
        }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 场景与模板参考回归测试报告",
        "",
        "## 范围与方法",
        "",
        f"- 运行时间：{report['run_at']}",
        f"- 隔离数据目录：`{report['data_dir']}`",
        f"- 手册：`{report['manual']['filename']}`，命令页 {report['manual']['command_count']} 条。",
        "- 每个基础场景均实际经过：生成配置思路 -> 手册检索 -> 命令计划 -> 手册静态编译 -> 独立 LLM 审阅。",
        "- 每个基础场景都会保存为隔离回归模板，随后在同类变体中作为仅供参考的上下文重复上述流程；即使基础质量门未通过，变体仍会执行，以完整记录 8×2 结果。",
        "- 未连接 eNSP、未调用 Netmiko、未下发、未保存任何设备配置。耗时为本机端到端墙钟时间，包含本地 LLM/Embedding API 等待；不包含手册预先导入和 Embedding 预构建时间。",
        "",
        "## 汇总",
        "",
        "| 场景 | 基础 | 基础总耗时 | 模板 | 变体 + 模板参考 | 变体总耗时 | 结论 |",
        "| --- | --- | ---: | --- | --- | ---: | --- |",
    ]
    for row in report["scenarios"]:
        base = row["base"]
        variant = row.get("variant")
        template = row.get("template")
        variant_status = "未运行" if not variant else ("通过" if variant["quality"]["passed"] else "未通过")
        variant_time = "-" if not variant else f"{variant['timing_seconds']['total']:.3f}s"
        conclusion = (
            "通过" if base["quality"]["passed"] and variant and variant["quality"]["passed"] else "需检查"
        )
        lines.append(
            "| {title} | {base_status} | {base_time:.3f}s | {template} | {variant_status} | {variant_time} | {conclusion} |".format(
                title=row["title"],
                base_status="通过" if base["quality"]["passed"] else "未通过",
                base_time=base["timing_seconds"]["total"],
                template=(template or {}).get("title", "未保存"),
                variant_status=variant_status,
                variant_time=variant_time,
                conclusion=conclusion,
            )
        )
    lines.extend(["", "## 逐场景结果", ""])
    for row in report["scenarios"]:
        lines.extend([f"### {row['title']}", ""])
        for label, result in (("基础场景", row["base"]), ("同类变体（模板参考）", row.get("variant"))):
            if not result:
                lines.extend([f"#### {label}", "", "未运行：基础场景未达到质量门，避免保存错误模板。", ""])
                continue
            timing = result["timing_seconds"]
            lines.extend(
                [
                    f"#### {label}",
                    "",
                    f"- 结果：{'通过' if result['quality']['passed'] else '未通过'}",
                    f"- 耗时：拓扑持久化 {timing['topology_persist']:.3f}s；配置思路 {timing['planning_idea']:.3f}s；"
                    f"检索、命令生成与审阅 {timing['retrieval_command_review']:.3f}s；总计 {timing['total']:.3f}s。",
                    f"- 任务 ID：`{result['quality']['snapshot']['task_id']}`；拓扑 Revision ID：`{result['topology_revision_id']}`。",
                    "",
                    "质量检查：",
                    "",
                    "| 检查项 | 结果 | 详情 |",
                    "| --- | --- | --- |",
                ]
            )
            for check in result["quality"]["checks"]:
                lines.append(
                    f"| {check['name']} | {'通过' if check['passed'] else '未通过'} | {check['detail']} |"
                )
            lines.extend(["", "生成的设备命令：", ""])
            for plan in result["quality"]["snapshot"]["plans"]:
                lines.extend([f"**{plan['device']}**", "", "```text", *plan["commands"], "```", ""])
        if row.get("template"):
            lines.extend(
                [
                    "模板记录：",
                    "",
                    f"- 标题：`{row['template']['title']}`",
                    f"- 模板 ID：`{row['template']['id']}`",
                    f"- 来源任务：`{row['template']['source_task_id']}`",
                    "",
                ]
            )
    lines.extend(
        [
            "## 质量门说明",
            "",
            "每次检查都要求任务处于 `needs_review`、设备数量符合该拓扑、必需 CLI 已出现、禁止的维护或越界 CLI 未出现。"
            "每台设备通常必须通过手册静态编译；仅当草案非空且错误全部是“未能绑定已注入手册页”的 CLI 时，"
            "允许保留为明确标识的 `draft_with_warnings`，供用户审阅，不会自动下发。独立 LLM 审阅只记录建议和问题，不会使场景失败，也不会替代"
            "用户对命令草案的最终判断。变体还要求任务意图中带有模板 ID 和模板配置思路快照。",
            "命令正确性是基于导入的华为手册、拓扑端口范围和两次 LLM 推理的离线规划正确性；它不替代对真实设备型号、版本、现网状态和回显的最终验证。",
            "",
            "## 原始记录",
            "",
            f"完整 JSON（包含每阶段事件、检索审计、命令计划和审阅结果）位于 `{report['json_path']}`。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="全流程场景与模板参考回归")
    parser.add_argument(
        "--only",
        help="仅运行逗号分隔的场景键，用于定向复测；默认运行全部。",
    )
    parser.add_argument(
        "--stream-events",
        action="store_true",
        help="同时验证流式事件收集；默认使用非流式完整响应进行场景正确性与性能回归。",
    )
    args = parser.parse_args()
    selected_keys = {item.strip() for item in (args.only or "").split(",") if item.strip()}
    available_keys = {pair.key for pair in PAIRS}
    unknown_keys = selected_keys - available_keys
    if unknown_keys:
        parser.error(f"未知场景键：{', '.join(sorted(unknown_keys))}")
    pairs = tuple(pair for pair in PAIRS if not selected_keys or pair.key in selected_keys)
    init_database()
    output_root = _output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as session:
        manual = session.scalar(
            select(Manual).where(Manual.command_count > 0).order_by(Manual.command_count.desc()).limit(1)
        )
        if manual is None:
            raise RuntimeError("当前隔离数据库中没有已注入且具有命令页的手册")
        report: dict[str, Any] = {
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data_dir": os.getenv("APP_DATA_DIR", "data"),
            "manual": {
                "id": manual.id,
                "filename": manual.original_filename,
                "brand": manual.brand,
                "release": manual.release,
                "command_count": manual.command_count,
            },
            "scenarios": [],
        }
        for index, pair in enumerate(pairs, start=1):
            print(f"[{index}/{len(pairs)}] 基础场景：{pair.title}", flush=True)
            base = _run_case_safely(
                session,
                manual=manual,
                case=pair.base,
                stream_events=args.stream_events,
            )
            row: dict[str, Any] = {"key": pair.key, "title": pair.title, "base": base}
            if not base["quality"]["passed"]:
                print(
                    f"[{index}/{len(pairs)}] 基础场景未通过质量门；仍保存隔离模板并运行变体以完成回归。",
                    flush=True,
                )
            template = create_template_from_task(
                session,
                task_id=base["quality"]["snapshot"]["task_id"],
                title=f"回归模板-{index:02d}-{pair.title}",
                description=(
                    f"{pair.title} 的基础场景回归快照；仅用于同类变体的规划参考。"
                    "质量结果以本报告为准。"
                ),
            )
            row["template"] = {
                "id": template.id,
                "title": template.title,
                "source_task_id": template.source_task_id,
            }
            print(f"[{index}/{len(pairs)}] 变体场景（带模板参考）：{pair.title}", flush=True)
            row["variant"] = _run_case_safely(
                session,
                manual=manual,
                case=pair.variant,
                template_id=template.id,
                stream_events=args.stream_events,
            )
            report["scenarios"].append(row)
        report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        json_path = output_root / f"rerun-{stamp}.json"
        markdown_path = output_root / f"rerun-{stamp}.md"
        report["json_path"] = str(json_path)
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path.write_text(_markdown(report), encoding="utf-8")
        print(f"JSON: {json_path}", flush=True)
        print(f"Markdown: {markdown_path}", flush=True)
        failed = [
            row["title"]
            for row in report["scenarios"]
            if not row["base"]["quality"]["passed"]
            or not row.get("variant", {}).get("quality", {}).get("passed")
        ]
        if failed:
            print(f"FAILED: {', '.join(failed)}", flush=True)
            return 1
        print("ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
