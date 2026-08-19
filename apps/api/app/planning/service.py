from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable
from datetime import datetime
from threading import Event
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.agents.graph import build_planning_graph
from app.models import (
    Command,
    CompatibilityStatus,
    ConfigTask,
    ConfigurationTemplate,
    DevicePlan,
    ImportStatus,
    Manual,
    TaskStatus,
    Topology,
    TopologyRevision,
)
from app.planning.dialect import HUAWEI_VRP, CliDialect, is_huawei_vlan_renderer, resolve_cli_dialect
from app.planning.llm_command_plan import (
    CONTROL_COMMAND_ID,
    _matches_evidence_syntax,
    _normalize_cli,
    _syntax_forms,
    build_explicit_port_assignment_fallback_plan,
    compile_command_plan,
    complete_command_plan_from_review,
    normalize_huawei_vlan_creation_plan,
    plan_commands_with_llm,
    prune_command_plan_for_incomplete_syntax,
    prune_command_plan_for_known_facts,
    prune_command_plan_for_review_feedback,
)
from app.planning.llm_command_review import review_commands_with_llm
from app.planning.llm_refinement import refine_intent_with_llm
from app.planning.runtime import PlanningCancelled, check_cancel
from app.ports import port_identity
from app.retrieval.active import active_manual_search
from app.retrieval.hybrid import hybrid_command_search
from app.schemas import ConfigTaskCreate, LlmCommandPlan, TopologyDraft

PlanningEventSink = Callable[[str, str, str], None]

# ``\b`` cannot separate a CJK character from ``V`` because both are word
# characters to Python's Unicode regex engine.  Requirements normally contain
# forms such as ``属于VLAN10``; keep ASCII-token protection without losing that
# common Chinese form.
VLAN_RE = re.compile(r"(?<![A-Za-z0-9])VLAN\s*(\d{1,4})(?![A-Za-z0-9])", re.IGNORECASE)
INTERFACE_ADDRESS_ACTION_RE = re.compile(
    r"(?P<port>(?:(?:\d+)?[A-Za-z]+)?\d+(?:/\d+){1,4})\b[^。；;\n]{0,48}?"
    r"(?:需要)?(?:配置|设置|设为|配置为)\s*(?:IP\s*(?:地址)?\s*)?"
    r"(?P<address>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d{1,3})",
    re.IGNORECASE,
)
DEICTIC_INTERFACE_ADDRESS_RE = re.compile(
    r"(?:该|此)(?:接口|端口|口)[^。；;\n]{0,48}?"
    r"(?:需要)?(?:配置|设置|设为|配置为)\s*(?:IP\s*)?(?:地址\s*)?"
    r"(?P<address>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d{1,3})",
    re.IGNORECASE,
)
LOGICAL_INTERFACE_ADDRESS_ACTION_RE = re.compile(
    r"(?P<interface>[A-Za-z][A-Za-z0-9-]*(?:\s+)?\d+(?:/\d+)*)\s*"
    r"(?:需要)?(?:配置|设置|设为|配置为)\s*(?:IP\s*)?(?:地址\s*)?"
    r"(?P<address>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d{1,3})",
    re.IGNORECASE,
)
CONFIGURE_LOGICAL_INTERFACE_ADDRESS_RE = re.compile(
    r"(?:配置|设置|设为|配置为)\s*(?P<interface>[A-Za-z][A-Za-z0-9-]*(?:\s+)?\d+(?:/\d+)*)\s*"
    r"(?:IP\s*)?(?:地址\s*)?(?P<address>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d{1,3})",
    re.IGNORECASE,
)
# Network requirements often state desired SVI/loopback addresses tersely,
# for example ``Core2 的 Vlanif10 地址 10.10.10.3/24``.  Unless the text marks
# it as already configured, this is a configuration target and must not be
# downgraded to an LLM assumption merely because it omits the verb "配置".
LOGICAL_INTERFACE_ADDRESS_VALUE_RE = re.compile(
    r"(?P<interface>[A-Za-z][A-Za-z0-9-]*(?:\s+)?\d+(?:/\d+)*)\s*(?:的)?\s*"
    r"(?:IP\s*)?(?:地址)\s*(?:为)?\s*"
    r"(?P<address>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d{1,3})",
    re.IGNORECASE,
)
# A topology-backed, explicit port action is stronger than a model's choice
# of view syntax.  It stays command-family-neutral: the selected manual must
# still provide the matching command grammar before any CLI can be rendered.
PORT_COMMAND_ASSIGNMENT_RE = re.compile(
    r"(?P<port>(?:(?:\d+)?[A-Za-z]+)?\d+(?:/\d+){1,4})\s*(?:接口|端口)?\s*"
    r"(?:加入|添加到|绑定到|关联到|配置为|设为)\s*"
    r"(?P<command>[A-Za-z][A-Za-z0-9-]*)\s*(?P<argument>\d+(?:/\d+)*)",
    re.IGNORECASE,
)
# A requirement can describe the current device and then document a peer's
# independent work in the next sentence.  The task-level IR must not turn the
# peer's address into an obligation for every DevicePlan.
OTHER_DEVICE_CONFIGURATION_CLAUSE_RE = re.compile(
    r"(?:另一台设备|对端设备|其他设备|其它设备)[^。；;\n]{0,24}"
    r"(?:配置|设置|创建|规划)",
    re.IGNORECASE,
)
INDEPENDENT_CONFIGURATION_RE = re.compile(
    r"(?:独立|另行|单独)[^。；;\n]{0,24}(?:配置|设置|创建|规划)", re.IGNORECASE
)
LEADING_DEVICE_TOKEN_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\b")
PORT_REFERENCE_RE = re.compile(r"\b(?:(?:\d+)?[A-Za-z]+)?\d+(?:/\d+){1,4}\b")
LOGICAL_INTERFACE_REFERENCE_RE = re.compile(r"\b(?P<interface>[A-Za-z][A-Za-z0-9-]*(?:\s+)?\d+(?:/\d+)*)\b")
VLAN_ACCESS_COMMAND_PREFIXES = {
    "vlan batch": "vlan batch",
    "port link-type": "port link-type",
    "port default vlan": "port default vlan",
}
VLAN_INTERVLAN_COMMAND_PREFIXES = {
    **VLAN_ACCESS_COMMAND_PREFIXES,
    "port trunk allow-pass vlan": "port trunk allow-pass vlan",
    "interface": "interface ",
    "ip address": "ip address ",
}
INTERVLAN_KEYWORDS = (
    "三层",
    "互通",
    "互相通信",
    "相互通信",
    "vlan之间",
    "vlan间",
    "跨vlan",
    "vlanif",
    "网关",
    "inter-vlan",
    "intervlan",
    "l3",
    "gateway",
    "routing",
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str) -> Any:
    return json.loads(value) if value else {}


def _emit(event_sink: PlanningEventSink | None, stage: str, event_type: str, content: str) -> None:
    if event_sink:
        event_sink(stage, event_type, content)


VLAN_RENDERER_CAPABILITIES = {
    "vlan_access",
    "vlan_trunk",
    "vlanif_gateway",
    "multi_vlan_intervlan",
}

# The visible ReAct loop owns semantic/hybrid retrieval and has two rounds of
# five queries.  This small lexical bootstrap only supplies exact handbook
# neighbours to the first packet; keeping it to the same five-term budget
# avoids an unbounded hidden scan before the model starts its focused search.
MAX_MANUAL_BOOTSTRAP_TERMS = 5


def _renderer_mode_for_intent(intent: dict[str, Any], dialect: CliDialect) -> str:
    """Keep the deterministic VLAN compiler limited to VLAN-only tasks.

    The compiler is reliable for Huawei VLAN topology roles, but it cannot
    express a second protocol in the same command plan. Composite requests
    therefore use the existing evidence-bound generic compiler; the VLAN-only
    path remains unchanged.
    """

    if not is_huawei_vlan_renderer(intent, dialect):
        return "generic_evidence_bound"
    capabilities = {
        str(item).strip() for item in intent.get("planning_capabilities", []) if str(item).strip()
    }
    if capabilities - VLAN_RENDERER_CAPABILITIES:
        return "generic_evidence_bound"
    return "huawei_vlan"


def _intent_for_device(intent: dict[str, Any], device_node_id: str) -> dict[str, Any]:
    """Restrict explicit device-named facts to the matching topology node.

    Facts without a device binding retain their historical task-wide meaning.
    This matters only when a requirement explicitly starts a clause with a
    topology device name, such as ``SW3 配置 Vlanif10 ...``.
    """

    scoped = dict(intent)
    for field in (
        "required_configuration_facts",
        "required_port_command_facts",
        "existing_configuration_facts",
    ):
        scoped[field] = [
            dict(item)
            for item in intent.get(field, [])
            if isinstance(item, dict)
            and (not item.get("device_node_id") or str(item.get("device_node_id")) == device_node_id)
        ]
    return scoped


def _matches_command_prefix(command: Command, prefix: str) -> bool:
    return any(
        str(syntax).strip().casefold().startswith(prefix.casefold()) for syntax in _load(command.syntax_json)
    )


def _command_prefixes_for_intent(intent: dict[str, Any], dialect: CliDialect = HUAWEI_VRP) -> dict[str, str]:
    if not is_huawei_vlan_renderer(intent, dialect):
        return {}
    if intent.get("feature") == "multi_vlan_intervlan":
        return VLAN_INTERVLAN_COMMAND_PREFIXES
    if intent.get("feature") == "vlan_access":
        return VLAN_ACCESS_COMMAND_PREFIXES
    return {}


def _evidence_matches_command(item: dict[str, Any], name: str, prefix: str) -> bool:
    matched_name = str(item.get("matched_command") or "").casefold()
    if matched_name and matched_name != name:
        return False
    syntax = item.get("syntax", [])
    normalized_syntax = [str(value).strip().casefold() for value in syntax]
    if name == "ip address":
        # Some extracted pages store this grammar as individual tokens
        # (``["ip address", "ip-address", ...]``), whereas derived commands
        # such as ``ip address dhcp-alloc`` stay on one line.
        if not any(
            value == "ip address" or value.startswith("ip address ip-address") for value in normalized_syntax
        ):
            return False
    elif name == "interface":
        if not any(value.startswith("interface {") for value in normalized_syntax):
            return False
    elif not any(value.startswith(prefix.casefold()) for value in normalized_syntax):
        return False
    # ``ip address`` has several command-view variants.  Only the VLANIF-view
    # grammar can configure an SVI, so do not accept an ACL or unrelated view.
    if name == "ip address":
        return any("vlanif" in str(view).casefold() for view in item.get("views", []))
    return True


def _command_matches_required_syntax(command: Command, name: str, prefix: str) -> bool:
    return _evidence_matches_command(
        {
            "syntax": _load(command.syntax_json),
            "views": _load(command.views_json),
        },
        name,
        prefix,
    )


def _validate_topology(payload: TopologyDraft) -> None:
    """Validate a complete topology draft before it becomes a revision."""

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


def create_topology(session: Session, payload: TopologyDraft) -> TopologyRevision:
    _validate_topology(payload)
    topology = Topology(name=payload.name)
    session.add(topology)
    session.flush()
    revision = TopologyRevision(topology_id=topology.id, revision=1, graph_json=_json(payload.model_dump()))
    session.add(revision)
    session.commit()
    session.refresh(revision)
    return revision


def update_topology(session: Session, topology_id: str, payload: TopologyDraft) -> TopologyRevision:
    """Append a revision to an existing saved topology."""

    topology = session.get(Topology, topology_id)
    if not topology:
        raise ValueError("拓扑不存在，可能已被删除。")
    _validate_topology(payload)
    latest_revision = session.scalar(
        select(func.max(TopologyRevision.revision)).where(TopologyRevision.topology_id == topology.id)
    )
    topology.name = payload.name
    topology.updated_at = datetime.utcnow()
    revision = TopologyRevision(
        topology_id=topology.id,
        revision=(latest_revision or 0) + 1,
        graph_json=_json(payload.model_dump()),
    )
    session.add(revision)
    session.commit()
    session.refresh(revision)
    return revision


def get_topology_revision(session: Session, topology_id: str) -> TopologyRevision | None:
    return session.scalar(
        select(TopologyRevision)
        .where(TopologyRevision.topology_id == topology_id)
        .order_by(TopologyRevision.revision.desc())
        .limit(1)
    )


def _pc_vlan_mapping(requirement: str, graph: dict[str, Any]) -> dict[str, int]:
    """Extract explicit ``PC names -> VLAN`` facts without inventing an endpoint VLAN.

    The accepted forms deliberately cover the normal Chinese descriptions such as
    ``PC1 与 PC3 属于 VLAN 10`` and ``PC1、PC3 接入 VLAN10``.  A PC only becomes
    an Access endpoint when its name and VLAN appear in the same sentence.
    """

    pcs = [item for item in graph.get("nodes", []) if item.get("kind") == "pc"]
    mapping: dict[str, int] = {}
    # One Chinese sentence often assigns several PC groups, for example
    # ``PC1 与 PC3 属于 VLAN 10，PC2 与 PC4 属于 VLAN 20``. Split those
    # clauses before looking for their single explicit VLAN assignment.
    for sentence in re.split(r"[。；;.\n，,]", requirement):
        vlan_values = [int(value) for value in VLAN_RE.findall(sentence) if 1 <= int(value) <= 4094]
        if len(vlan_values) != 1:
            continue
        vlan_id = vlan_values[0]
        for pc in pcs:
            name = str(pc.get("name") or pc.get("label") or "").strip()
            if name and re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", sentence, re.I):
                mapping[str(pc.get("id"))] = vlan_id
    return mapping


def _l3_core_node_id(requirement: str, graph: dict[str, Any]) -> str | None:
    """Resolve an explicitly named L3 switch, then use the topology's L3 shape.

    The fallback is intentionally visible in the intent so an operator can review it.
    It selects only a switch connected to other switches and to no PC, which matches
    the core in the supported campus topology pattern.
    """

    switches = [item for item in graph.get("nodes", []) if item.get("kind") == "switch"]
    lowered = requirement.casefold()
    for switch in switches:
        name = str(switch.get("name") or switch.get("label") or "").strip()
        if name and name.casefold() in lowered:
            marker = "(?:三层|互通|vlanif|网关|inter-vlan|intervlan|l3|gateway|routing)"
            escaped = re.escape(name.casefold())
            if re.search(
                rf"{escaped}\s*(?:是|为|作为|承担)?[^。；;\n]{{0,24}}{marker}", lowered
            ) or re.search(rf"{marker}[^。；;\n]{{0,24}}{escaped}", lowered):
                return str(switch.get("id"))

    nodes = {str(item.get("id")): item for item in graph.get("nodes", [])}
    candidates: list[tuple[int, str]] = []
    for switch in switches:
        node_id = str(switch.get("id"))
        switch_peers = 0
        pc_peers = 0
        for link in graph.get("links", []):
            peer_id = None
            if link.get("source") == node_id:
                peer_id = str(link.get("target"))
            elif link.get("target") == node_id:
                peer_id = str(link.get("source"))
            if peer_id and nodes.get(peer_id, {}).get("kind") == "switch":
                switch_peers += 1
            elif peer_id and nodes.get(peer_id, {}).get("kind") == "pc":
                pc_peers += 1
        if switch_peers >= 2 and pc_peers == 0:
            candidates.append((switch_peers, node_id))
    return max(candidates, default=(0, None))[1]


def _vlan_gateway_plan(
    graph: dict[str, Any], pc_vlan_map: dict[str, int]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Use PC address data as the gateway source; fill a deterministic lab default if absent."""

    nodes = {str(item.get("id")): item for item in graph.get("nodes", [])}
    plan: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for vlan_id in sorted(set(pc_vlan_map.values())):
        members = [nodes[node_id] for node_id, value in pc_vlan_map.items() if value == vlan_id]
        gateways = {str(item.get("gateway")).strip() for item in members if item.get("gateway")}
        prefixes = {int(item.get("prefix")) for item in members if item.get("prefix") is not None}
        if len(gateways) == 1 and len(prefixes) == 1:
            gateway = next(iter(gateways))
            prefix = next(iter(prefixes))
            source = "topology_pc_gateway"
        else:
            # This is a transparent lab default, not a hidden device guess.  It is
            # displayed in the intent and the UI so the user can change it before send.
            gateway, prefix, source = f"10.{vlan_id}.0.1", 24, "generated_lab_default"
            warnings.append(f"VLAN {vlan_id} 的 PC 网关/掩码不一致或为空，草案使用 {gateway}/{prefix}。")
        try:
            mask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
            ipaddress.IPv4Address(gateway)
        except ValueError:
            warnings.append(f"VLAN {vlan_id} 的网关 {gateway}/{prefix} 无法解析，未生成 Vlanif 地址。")
            continue
        plan[str(vlan_id)] = {"gateway": gateway, "prefix": prefix, "mask": mask, "source": source}
    return plan, warnings


def _derive_intent(requirement: str, graph: dict[str, Any]) -> dict[str, Any]:
    """Build a topology-grounded IR; LLM refinement may explain, not expand it."""

    vlans = sorted({int(item) for item in VLAN_RE.findall(requirement) if 1 <= int(item) <= 4094})
    pc_vlan_map = _pc_vlan_mapping(requirement, graph)
    core_id = _l3_core_node_id(requirement, graph)
    is_intervlan = (
        len(vlans) >= 2
        and bool(pc_vlan_map)
        and any(keyword in requirement.casefold() for keyword in INTERVLAN_KEYWORDS)
    )
    # A feature is only routed to the deterministic VLAN plugin when the
    # requirement contains an explicit positive VLAN fact.  Text such as
    # "不使用 VLAN/Vlanif" must remain a generic L3 request, not be mistaken
    # for an Access configuration merely because it contains that word.
    # Seeing a VLAN number alone is not enough to infer that a physical Access
    # port must be touched. VRRP/VLANIF-only requirements must stay on the
    # evidence-bound generic path, where the LLM can plan virtual interfaces.
    access_keyword = re.search(r"\baccess\b|接入口|接入端口|端口配置为", requirement, re.IGNORECASE)
    access_forbidden = re.search(
        r"(?:不要|禁止|无需|不需要|不)\s*(?:配置)?[^。；;\n]{0,16}"
        r"(?:\baccess\b|接入口|接入端口)",
        requirement,
        re.IGNORECASE,
    )
    has_access_request = bool(pc_vlan_map) or bool(access_keyword and not access_forbidden)
    feature = (
        "multi_vlan_intervlan"
        if is_intervlan
        else ("vlan_access" if vlans and has_access_request else "generic")
    )
    node_kinds = {str(item.get("id")): item.get("kind") for item in graph.get("nodes", [])}
    has_switch_interconnect = any(
        node_kinds.get(str(link.get("source"))) == "switch"
        and node_kinds.get(str(link.get("target"))) == "switch"
        for link in graph.get("links", [])
    )
    topology_capabilities = (
        ["vlan_access", "vlan_trunk", "vlanif_gateway"]
        if feature == "multi_vlan_intervlan"
        else (["vlan_access"] if feature == "vlan_access" else [])
    )
    # A VLAN that spans switch-to-switch links needs an explicit Trunk review,
    # even when the request is primarily about MSTP or another non-VLAN
    # protocol. This is only a planning fact; it does not emit or alter CLI.
    if vlans and has_switch_interconnect and "vlan_trunk" not in topology_capabilities:
        topology_capabilities.append("vlan_trunk")
    gateways, address_warnings = _vlan_gateway_plan(graph, pc_vlan_map) if is_intervlan else ({}, [])
    required_configuration_facts: list[dict[str, str]] = []
    required_port_command_facts: list[dict[str, str]] = []
    existing_configuration_facts: list[dict[str, str]] = []
    current_device_names = {
        matched.group(1).casefold()
        for matched in re.finditer(r"当前设备\s+([A-Za-z][A-Za-z0-9_-]*)\b", requirement, re.IGNORECASE)
    }

    # Resolve a Chinese deictic reference ("该口配置 ...") only when the
    # topology makes it unambiguous.  This prevents the common point-to-point
    # shorthand from silently dropping an explicit address action, while never
    # guessing in a multi-port topology.
    topology_ports = list(
        dict.fromkeys(
            str(port).strip()
            for link in graph.get("links", [])
            for port in (link.get("source_port"), link.get("target_port"))
            if isinstance(port, str) and port.strip() and port.upper() != "UNMAPPED"
        )
    )
    # A deictic phrase such as "该口配置 10.0.0.1/30" should resolve against
    # the switch end of drawn links, not a peer PC's Ethernet label.  This is
    # topology semantics, independent of a particular port naming scheme.
    nodes_by_id = {str(item.get("id")): item for item in graph.get("nodes", [])}
    device_id_by_reference: dict[str, str] = {}
    for node_id, node in nodes_by_id.items():
        if node.get("kind") != "switch":
            continue
        for reference in (node_id, node.get("name"), node.get("label")):
            normalized = str(reference or "").strip().casefold()
            if normalized:
                device_id_by_reference.setdefault(normalized, node_id)
    switch_topology_ports = list(
        dict.fromkeys(
            str(port).strip()
            for link in graph.get("links", [])
            for node_id, port in (
                (str(link.get("source")), link.get("source_port")),
                (str(link.get("target")), link.get("target_port")),
            )
            if nodes_by_id.get(node_id, {}).get("kind") == "switch"
            and isinstance(port, str)
            and port.strip()
            and port.upper() != "UNMAPPED"
        )
    )
    # A natural requirement often places several device clauses in one
    # sentence: ``SW1 的 GE... 配置 ...，SW2 的 GE... 配置 ...``.  Sentence
    # level ownership would incorrectly attach both addresses to SW1. Resolve
    # the closest preceding topology device for every matched action instead.
    # This is based solely on node ids/names supplied by the topology, so it
    # remains independent of vendor CLI or a particular network feature.
    device_reference_pattern = (
        re.compile(
            r"(?<![A-Za-z0-9_-])(?P<reference>"
            + "|".join(
                re.escape(reference) for reference in sorted(device_id_by_reference, key=len, reverse=True)
            )
            + r")(?![A-Za-z0-9_-])",
            re.IGNORECASE,
        )
        if device_id_by_reference
        else None
    )

    def device_for_match(sentence: str, offset: int, fallback: str | None) -> str | None:
        if not device_reference_pattern:
            return fallback
        nearest: str | None = None
        for match in device_reference_pattern.finditer(sentence[:offset]):
            # ``当前设备 Core1`` is intentionally task-scoped in the legacy
            # workflow: it describes the device being planned rather than a
            # topology-wide named-device clause. Keep that behaviour while
            # still scoping ordinary ``Core1 配置 ...，Core2 配置 ...`` text.
            leading = sentence[max(0, match.start() - 12) : match.start()]
            if re.search(r"当前设备\s*$", leading, re.IGNORECASE):
                continue
            nearest = device_id_by_reference.get(match.group("reference").casefold(), nearest)
        return nearest or fallback

    def add_address_fact(
        kind: str,
        interface: str,
        address: str,
        prefix: int,
        device_node_id: str | None = None,
    ) -> None:
        if not 0 <= prefix <= 128:
            return
        key = "port" if kind == "interface_address" else "interface"
        fact = {"kind": kind, key: interface, "address": address, "prefix": str(prefix)}
        if device_node_id:
            fact["device_node_id"] = device_node_id
        if fact not in required_configuration_facts:
            required_configuration_facts.append(fact)

    def add_existing_address_fact(
        port: str, address: str, prefix: int, device_node_id: str | None = None
    ) -> None:
        """Record an explicit current-state fact without turning it into work.

        This distinction is vendor-neutral: an address stated as already
        configured is evidence for a route/protocol plan, not an instruction to
        reconfigure the port.  The command compiler later uses this narrow fact
        only to remove an exact duplicate from a weak-model fallback draft.
        """

        topology_port = topology_port_by_identity.get(port_identity(port))
        if not topology_port or not 0 <= prefix <= 128:
            return
        fact = {
            "kind": "existing_interface_address",
            "port": topology_port,
            "address": address,
            "prefix": str(prefix),
        }
        if device_node_id:
            fact["device_node_id"] = device_node_id
        if fact not in existing_configuration_facts:
            existing_configuration_facts.append(fact)

    def logical_interface_before(sentence: str, offset: int) -> str | None:
        # A logical interface can be named before an address clause, for
        # example "Eth-Trunk 10 ... 聚合口作为三层口，配置地址 ...".  The
        # parser stores the actual name as a fact but does not prescribe its
        # vendor CLI spelling.
        candidates: list[str] = []
        for match in LOGICAL_INTERFACE_REFERENCE_RE.finditer(sentence[:offset]):
            candidate = match.group("interface").strip()
            leading = sentence[max(0, match.start() - 16) : match.start()]
            # A bare ``SW2`` / ``Branch1`` is normally a device name, not an
            # interface.  Retain names with an interface-shaped separator,
            # established virtual-interface stems, or an explicit create/
            # interface context. This still leaves vendor-specific names open.
            interface_context = bool(
                re.search(r"(?:创建|建立|接口|interface|create)\s*$", leading, re.IGNORECASE)
            )
            interface_stem = candidate.casefold().startswith(
                ("vlanif", "loopback", "svi", "irb", "bdi", "vbdif")
            )
            if not PORT_REFERENCE_RE.fullmatch(candidate) and (
                "-" in candidate or interface_context or interface_stem
            ):
                candidates.append(candidate)
        return candidates[-1] if candidates else None

    def is_logical_interface_name(candidate: str) -> bool:
        compact = candidate.strip()
        return "-" in compact or compact.casefold().startswith(
            ("vlanif", "loopback", "svi", "irb", "bdi", "vbdif")
        )

    topology_port_by_identity = {port_identity(port): port for port in switch_topology_ports}

    def add_port_command_fact(
        port: str,
        command_hint: str,
        argument: str,
        device_node_id: str | None = None,
    ) -> None:
        topology_port = topology_port_by_identity.get(port_identity(port))
        if not topology_port:
            return
        fact = {
            "kind": "port_command_assignment",
            "port": topology_port,
            "command_hint": command_hint,
            "argument": argument,
        }
        if device_node_id:
            fact["device_node_id"] = device_node_id
        if fact not in required_port_command_facts:
            required_port_command_facts.append(fact)

    # This is a capability-neutral requirement fact, not a Huawei command
    # template.  A generic planner may use whatever CLI spelling the selected
    # handbook defines, but it cannot silently turn an explicit address action
    # into an "already configured" assumption.
    clause_cursor = 0
    for sentence in re.split(r"[。；;\n]", requirement):
        clause_start = requirement.find(sentence, clause_cursor)
        if clause_start < 0:
            clause_start = clause_cursor
        clause_cursor = clause_start + len(sentence)
        leading_device = LEADING_DEVICE_TOKEN_RE.search(sentence)
        target_device_id = (
            device_id_by_reference.get(leading_device.group(1).casefold()) if leading_device else None
        )
        if re.search(r"(?:已配置|已经|当前已|现有|已存在)", sentence, re.IGNORECASE):
            # Existing physical-interface addresses are intentionally kept in
            # a separate immutable fact list.  ``INTERFACE_ADDRESS_ACTION_RE``
            # handles pending work only, so a sentence such as "GE0/0/1 已配置
            # 10.0.12.1/30" cannot later be mistaken for an address action.
            for matched in re.finditer(
                r"(?P<port>(?:(?:\d+)?[A-Za-z]+)?\d+(?:/\d+){1,4})\b"
                r"[^。；;\n]{0,48}?(?:已配置|已经|当前已|现有|已存在)"
                r"[^。；;\n]{0,32}?"
                r"(?P<address>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d{1,3})",
                sentence,
                re.IGNORECASE,
            ):
                add_existing_address_fact(
                    matched.group("port"),
                    matched.group("address"),
                    int(matched.group("prefix")),
                    target_device_id,
                )
            continue
        is_named_independent_peer = bool(
            leading_device
            and current_device_names
            and leading_device.group(1).casefold() not in current_device_names
            and INDEPENDENT_CONFIGURATION_RE.search(sentence)
        )
        if OTHER_DEVICE_CONFIGURATION_CLAUSE_RE.search(sentence) or is_named_independent_peer:
            # The peer's concrete address remains in the human-readable
            # requirement and planning idea, but it is not a current-device
            # configuration fact.  This is scope parsing, not a protocol rule.
            continue
        for matched in PORT_COMMAND_ASSIGNMENT_RE.finditer(sentence):
            add_port_command_fact(
                matched.group("port"),
                matched.group("command"),
                matched.group("argument"),
                device_for_match(sentence, matched.start(), target_device_id),
            )
        for matched in INTERFACE_ADDRESS_ACTION_RE.finditer(sentence):
            prefix = int(matched.group("prefix"))
            add_address_fact(
                "interface_address",
                matched.group("port"),
                matched.group("address"),
                prefix,
                device_for_match(sentence, matched.start(), target_device_id),
            )
        for matched in DEICTIC_INTERFACE_ADDRESS_RE.finditer(sentence):
            local_ports = list(dict.fromkeys(PORT_REFERENCE_RE.findall(sentence[: matched.start()])))
            # Prefer an explicit port earlier in the same sentence.  If the
            # sentence only says "该口", a one-port topology is equally
            # unambiguous.  Otherwise leave the action to the LLM and expose
            # the ambiguity in the planning idea instead of choosing a port.
            candidate_ports = local_ports or switch_topology_ports or topology_ports
            if len(candidate_ports) == 1:
                add_address_fact(
                    "interface_address",
                    candidate_ports[0],
                    matched.group("address"),
                    int(matched.group("prefix")),
                    device_for_match(sentence, matched.start(), target_device_id),
                )
        # Accept both natural Chinese orders: ``Vlanif10 配置地址`` and
        # ``配置 Vlanif10 地址``.  The latter has no interface token before
        # the address verb, so it needs its own explicit scan.  Names remain
        # capability-neutral and are accepted only when they look like a
        # logical interface rather than a device label such as ``Core1``.
        for matched in LOGICAL_INTERFACE_ADDRESS_ACTION_RE.finditer(sentence):
            logical = matched.group("interface").strip()
            if is_logical_interface_name(logical):
                add_address_fact(
                    "logical_interface_address",
                    logical,
                    matched.group("address"),
                    int(matched.group("prefix")),
                    device_for_match(sentence, matched.start(), target_device_id),
                )
        for matched in CONFIGURE_LOGICAL_INTERFACE_ADDRESS_RE.finditer(sentence):
            logical = matched.group("interface").strip()
            if is_logical_interface_name(logical):
                add_address_fact(
                    "logical_interface_address",
                    logical,
                    matched.group("address"),
                    int(matched.group("prefix")),
                    device_for_match(sentence, matched.start(), target_device_id),
                )
        for matched in LOGICAL_INTERFACE_ADDRESS_VALUE_RE.finditer(sentence):
            logical = matched.group("interface").strip()
            if is_logical_interface_name(logical):
                add_address_fact(
                    "logical_interface_address",
                    logical,
                    matched.group("address"),
                    int(matched.group("prefix")),
                    device_for_match(sentence, matched.start(), target_device_id),
                )
        # Address actions on logical interfaces do not carry a topology-port
        # reference. They remain capability-neutral requirements so the
        # command planner cannot omit an explicitly requested aggregation/SVI
        # address just because it is not a physical cable endpoint.
        for matched in re.finditer(
            r"(?:配置|设置|设为|配置为)\s*(?:IP\s*)?(?:地址\s*)?"
            r"(?P<address>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d{1,3})",
            sentence,
            re.IGNORECASE,
        ):
            # Semicolons often separate the logical interface declaration
            # from its address action (``Eth-Trunk 10；聚合口...配置地址``).
            # Look back through the same requirement, not only this clause,
            # while retaining the strict interface-name filter above.
            preceding_requirement = requirement[: clause_start + matched.start()]
            logical = logical_interface_before(preceding_requirement, len(preceding_requirement))
            if logical:
                add_address_fact(
                    "logical_interface_address",
                    logical,
                    matched.group("address"),
                    int(matched.group("prefix")),
                    device_for_match(sentence, matched.start(), target_device_id),
                )
    return {
        "source": "topology_grounded_baseline",
        "feature": feature,
        "topology_capabilities": topology_capabilities,
        "planning_capabilities": topology_capabilities,
        "vlan_ids": vlans,
        "pc_vlan_map": pc_vlan_map,
        "l3_core_node_id": core_id,
        "vlan_gateways": gateways,
        "planning_warnings": address_warnings,
        "requirement": requirement,
        "requires_llm_refinement": True,
        "planning_steps": [],
        "retrieval_terms": [],
        "acceptance": ["设备侧 display/只读验证"],
        "required_configuration_facts": required_configuration_facts,
        "required_port_command_facts": required_port_command_facts,
        "existing_configuration_facts": existing_configuration_facts,
    }


def _manual_selection_context(
    manual: Manual,
    detected_model: str | None,
    detected_release: str | None,
) -> tuple[CompatibilityStatus, str, str | None]:
    """Describe the explicit-manual policy without inferring a model family.

    A manual is selected for the whole task by the operator.  A `display version`
    result remains useful audit evidence, but it is intentionally not a command
    generation or execution gate: the selected manual, command evidence, topology
    scope, static checks, approval, and device validation form those gates.
    """

    identity = f"现场只读信息：{detected_model or '未查询型号'} / {detected_release or '未查询版本'}。"
    return (
        CompatibilityStatus.manual_selected,
        f"用户已选择已完成抽取的手册《{manual.original_filename}》作为本任务的命令上下文。{identity}"
        "现场型号仅用于审计，不参与型号库或系列匹配门禁。",
        None,
    )


def _manual_context_is_approved(status: CompatibilityStatus) -> bool:
    """Accept historic exact plans while new plans use manual_selected."""

    return status in {CompatibilityStatus.manual_selected, CompatibilityStatus.exact}


def _evidence_from_command(
    command: Command, *, expected_name: str | None, source: str, score: float | None
) -> dict[str, Any]:
    return {
        "command_id": command.id,
        "document_id": command.document_id,
        "canonical_name": command.canonical_name,
        "matched_command": expected_name,
        "syntax": _load(command.syntax_json),
        "views": _load(command.views_json),
        "parameters": _load(command.parameters_json),
        "preconditions": _load(command.preconditions_json),
        "constraints": _load(command.constraints_json),
        "examples": _load(command.examples_json),
        "source_path": _load(command.evidence_json).get("source_path"),
        "retrieval_score": score,
        "retrieval_sources": [source],
    }


def _generic_retrieval_terms(intent: dict[str, Any], dialect: CliDialect = HUAWEI_VRP) -> list[str]:
    terms = [
        str(item).strip()
        for item in intent.get("retrieval_terms", [])
        if isinstance(item, str) and item.strip()
    ]
    explicit_port_action_terms = [
        str(item.get("command_hint") or "").strip()
        for item in intent.get("required_port_command_facts", [])
        if isinstance(item, dict) and str(item.get("command_hint") or "").strip()
    ]
    requirement = str(intent.get("requirement") or "").strip()
    # A weak intent-extraction model can omit the one protocol/family token
    # that makes the imported command catalogue navigable (for example VRRP,
    # iStack or Stack-Port).  Preserve explicit command-shaped tokens from the
    # user's own requirement before broad interface/address helpers.  These
    # are only retrieval anchors: the selected manual decides whether a title
    # actually exists and what syntax it permits.
    requirement_command_terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", requirement):
        if len(token) < 3:
            continue
        is_protocol_or_family = (
            token.isupper() or "-" in token or bool(re.search(r"[a-z][A-Z]|[A-Z][a-z]", token))
        )
        if not is_protocol_or_family:
            continue
        requirement_command_terms.append(token)
        requirement_command_terms.extend(part for part in re.split(r"[-_]", token) if len(part) >= 3)
    # A complete requirement is a useful semantic/FTS seed when a small model
    # did not provide terms. It does not encode a product-specific capability.
    address_required = any(
        item.get("kind") in {"interface_address", "logical_interface_address"}
        for item in intent.get("required_configuration_facts", [])
        if isinstance(item, dict)
    )
    # Logical interfaces are not topology ports, but a request such as
    # ``Vlanif10 地址`` still needs the handbook's interface-entry and address
    # grammar. This remains a retrieval hint; it does not prescribe a vendor CLI.
    logical_address_required = bool(
        re.search(
            r"(?:interface|vlanif|loopback|svi)[^。；;\n]{0,32}(?:ip\s*)?地址",
            requirement,
            re.IGNORECASE,
        )
    )
    address_terms = ["interface", "ip address"] if address_required or logical_address_required else []
    conversion_term = (
        [dialect.l3_physical_interface_conversion_evidence]
        if address_required and dialect.l3_physical_interface_conversion_evidence
        else []
    )
    # This is a search seed rather than a renderer rule. A Huawei manual can
    # contain several unrelated ``vlan`` pages; its system-view ``vlan batch``
    # page is the relevant evidence when a generic plan explicitly creates VLANs.
    vlan_creation_terms = (
        ["vlan batch"] if intent.get("vlan_ids") and dialect.supports_huawei_vlan_renderer else []
    )
    # An active-search round has a deliberately bounded candidate budget.  A
    # hardware/CLI precondition required by the selected dialect must therefore
    # precede broad LLM-proposed nouns.  Otherwise a long protocol description
    # can use all slots and leave the command planner without evidence for a
    # required prerequisite such as turning a physical switch port into a
    # routed port.  This is a dialect capability, not a feature allow-list.
    # Preserve literal command words embedded in an LLM retrieval phrase. A
    # phrase such as ``OSPF area`` is useful semantically, but a handbook may
    # index the actual page simply as ``area``. This expansion is derived from
    # the current intent text rather than a vendor/feature command list.
    literal_terms: list[str] = []
    for term in terms[:MAX_MANUAL_BOOTSTRAP_TERMS]:
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", term):
            if len(token) >= 3:
                literal_terms.append(token)
            # Handbook families are commonly written as compound labels such
            # as ``iStack`` or ``Stack-Port`` while their actionable page is
            # titled ``stack member``. Preserve the literal but also expose
            # lexical components for catalogue navigation; this is format-
            # neutral and does not prescribe a vendor command.
            literal_terms.extend(
                part
                for part in re.split(r"[-_]", token)
                for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", part)
                if len(part) >= 3
            )
    return list(
        dict.fromkeys(
            [
                *requirement_command_terms,
                *address_terms,
                *conversion_term,
                *vlan_creation_terms,
                *explicit_port_action_terms,
                *terms,
                *literal_terms,
                *([requirement] if requirement else []),
            ]
        )
    )[:18]


def _find_evidence(
    session: Session,
    manual_id: str,
    intent: dict[str, Any],
    dialect: CliDialect = HUAWEI_VRP,
) -> list[dict[str, Any]]:
    command_prefixes = _command_prefixes_for_intent(intent, dialect)
    required_terms = list(command_prefixes)
    llm_terms = _generic_retrieval_terms(intent, dialect)
    # Required command names are always searched first.  LLM-provided terms
    # broaden recall only; they cannot replace the evidence needed by the
    # deterministic VLAN renderer.
    terms = list(dict.fromkeys([*required_terms, *llm_terms]))
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for term in terms:
        expected_name = term.casefold() if term.casefold() in command_prefixes else None
        candidates = [
            hit.command
            for hit in hybrid_command_search(
                session,
                query=term,
                manual_id=manual_id,
                limit=8,
                # Bootstrap terms are literal handbook navigation (command
                # names, view names, and explicit requirement nouns). Running
                # a separate embedding request for every one wastes most of
                # the planning time. The active recovery below embeds the
                # complete requirement once in a small batch when lexical and
                # catalogue evidence leave a gap.
                use_semantic=False,
            )
        ]
        # Page titles are not a stable command identifier in vendor manuals.
        # A local syntax-prefix lookup recovers commands such as the VLANIF-view
        # ``ip address`` page whose title contains a Chinese view suffix.
        if expected_name:
            prefix = command_prefixes[expected_name]
            syntax_marker = '"ip address"' if expected_name == "ip address" else f'"{prefix}'
            candidates.extend(
                session.scalars(
                    select(Command)
                    .where(Command.manual_id == manual_id)
                    .where(Command.syntax_json.ilike(f"%{syntax_marker}%"))
                    .limit(32)
                ).all()
            )
        for command in candidates:
            expected_prefix = command_prefixes.get(expected_name or "")
            if (
                expected_name
                and expected_prefix
                and not _command_matches_required_syntax(command, expected_name, expected_prefix)
            ):
                # The same command title can occur in a different product or
                # management context. VLAN Access needs the interface CLI form.
                continue
            if command.id in seen:
                continue
            seen.add(command.id)
            evidence.append(
                _evidence_from_command(
                    command,
                    expected_name=expected_name,
                    source="syntax_prefix" if expected_name else "hybrid",
                    score=1.0 if expected_name else None,
                )
            )
    # Retain enough per-term candidates for the generic active-retrieval path
    # to recover exact catalog anchors that appear after broad, same-named
    # pages. The downstream generic packet is still capped to a small set.
    return evidence[:120]


def _required_command_names(intent: dict[str, Any], dialect: CliDialect = HUAWEI_VRP) -> set[str]:
    return set(_command_prefixes_for_intent(intent, dialect))


def _active_evidence_recovery(
    session: Session,
    *,
    manual_id: str,
    requirement: str,
    intent: dict[str, Any],
    event_sink: PlanningEventSink | None = None,
    cancel_event: Event | None = None,
    dialect: CliDialect = HUAWEI_VRP,
) -> dict[str, Any]:
    """Retry missing template evidence through the evidence-only LLM search loop."""

    evidence = _find_evidence(session, manual_id, intent, dialect)
    required = _required_command_names(intent, dialect)
    present = {
        name
        for name, prefix in _command_prefixes_for_intent(intent, dialect).items()
        if any(_evidence_matches_command(item, name, prefix) for item in evidence)
    }
    missing = sorted(required - present)
    if not required:
        # Every unrecognised capability uses the same evidence-only ReAct loop.
        # The selected candidates are prioritised, but neighbouring command pages
        # are retained as context so a later per-device plan can enter/leave the
        # required configuration views without a product-specific allow-list.
        retrieval_requirement = requirement
        confirmed_idea = str(intent.get("confirmed_planning_idea") or "").strip()
        if confirmed_idea:
            retrieval_requirement += f"\n已确认配置思路（同样需要命令证据）：{confirmed_idea}"
        seed_queries = _generic_retrieval_terms(intent, dialect)
        outcome = active_manual_search(
            session,
            manual_id=manual_id,
            requirement=retrieval_requirement,
            seed_queries=seed_queries,
            topology_context=dict(intent.get("topology_context") or {}),
            confirmed_idea=str(intent.get("confirmed_planning_idea") or ""),
            known_actions=[*seed_queries, *[str(item) for item in intent.get("planning_capabilities", [])]],
            on_progress=event_sink,
            cancel_event=cancel_event,
        )
        initial_evidence = list(evidence)

        # A command reference often indexes a configuration action under a
        # generic page name.  For example, an ``Eth-Trunk`` requirement needs
        # the page titled ``mode（Eth-Trunk接口视图）`` because its syntax holds
        # ``lacp-static``.  FTS alone can rank display commands ahead of that
        # page.  Expand only literal protocol/capability words already present
        # in the request or the LLM's retrieval terms, and prefer writable
        # handbook pages.  This is catalogue navigation over the selected
        # manual, not a vendor or scenario allow-list, and makes the first
        # retrieval round materially more likely to be sufficient.
        literal_candidates: list[str] = []
        for term in intent.get("retrieval_terms", []):
            if not isinstance(term, str):
                continue
            literal_candidates.extend(
                token
                for token in re.findall(r"[A-Za-z][A-Za-z0-9-]*", term)
                if len(token) >= 3 and not any(char.isdigit() for char in token)
            )
        literal_candidates.extend(
            token
            for token in re.findall(r"[A-Za-z][A-Za-z0-9-]*", requirement)
            if len(token) >= 3
            and not any(char.isdigit() for char in token)
            and any(char.isupper() for char in token)
        )

        def configuration_rank(command: Command, term: str) -> tuple[int, int, int, str]:
            name = str(command.canonical_name or "").casefold()
            syntax = str(command.syntax_json or "").casefold()
            writable = not name.startswith(("display ", "reset ", "undo "))
            exact_name = name == term.casefold()
            name_match = term.casefold() in name
            syntax_match = term.casefold() in syntax
            return (
                0 if writable else 1,
                0 if exact_name else (1 if name_match else 2),
                0 if syntax_match else 1,
                name,
            )

        for literal in list(dict.fromkeys(literal_candidates))[:10]:
            catalog_matches = session.scalars(
                select(Command)
                .where(Command.manual_id == manual_id)
                .where(
                    or_(
                        Command.canonical_name.ilike(f"%{literal}%"),
                        Command.syntax_json.ilike(f"%{literal}%"),
                        Command.feature.ilike(f"%{literal}%"),
                    )
                )
                .limit(48)
            ).all()
            catalog_matches.sort(key=lambda command: configuration_rank(command, literal))
            for command in catalog_matches[:3]:
                if any(item.get("command_id") == command.id for item in initial_evidence):
                    continue
                initial_evidence.append(
                    _evidence_from_command(
                        command,
                        expected_name=None,
                        source="manual_literal_catalog",
                        score=1.0,
                    )
                )
        evidence = []
        seen: set[str] = set()

        # Interface-type words that appear in the confirmed requirement are
        # useful evidence discriminators.  For example, a generic ``ip
        # address`` query may return both an ACL-view page and a VLANIF-view
        # page; the latter is the only one whose documented view can support
        # ``Vlanif10``.  These hints are extracted from the operator's text,
        # not from a vendor or feature allow-list, so another manual can use a
        # different interface type such as Loopback or irb.
        interface_view_hints = {
            value.casefold()
            for value in re.findall(
                r"\b([A-Za-z][A-Za-z-]*)(?=\d+(?:/\d+)*\b)",
                requirement,
            )
            if len(value) >= 2
        }

        def context_sort_key(item: dict[str, Any], name_key: str) -> tuple[int, int, str]:
            views = " ".join(str(view) for view in item.get("views", [])).casefold()
            view_matches_requirement = bool(
                interface_view_hints and any(hint in views for hint in interface_view_hints)
            )
            syntax_has_bare_command = any(
                re.sub(r"\s+", " ", str(syntax)).strip().casefold() == name_key
                for syntax in item.get("syntax", [])
            )
            return (
                0 if view_matches_requirement else 1,
                0 if syntax_has_bare_command else 1,
                str(item.get("canonical_name") or ""),
            )

        def normalized_command_name(value: object) -> str:
            return str(value or "").split("（", 1)[0].split("(", 1)[0].strip().casefold()

        # A topology requirement for a routed physical port has three
        # mechanical prerequisites: entering the interface view, any selected
        # dialect's L2/L3 conversion command, and the address command itself.
        # They are derived from the topology fact rather than an application
        # feature allow-list, so active-search neighbours cannot displace them.
        required_context_names = ["interface"]
        context_seed_names = {term.casefold() for term in _generic_retrieval_terms(intent, dialect)}
        if (
            any(
                isinstance(item, dict)
                and item.get("kind") in {"interface_address", "logical_interface_address"}
                for item in intent.get("required_configuration_facts", [])
            )
            or "ip address" in context_seed_names
        ):
            required_context_names.extend(
                [
                    "ip address",
                ]
            )
        if any(
            isinstance(item, dict) and item.get("kind") in {"interface_address", "logical_interface_address"}
            for item in intent.get("required_configuration_facts", [])
        ):
            required_context_names.append(
                str(dialect.l3_physical_interface_conversion_evidence or "").strip()
            )

        def add_context_evidence(item: dict[str, Any]) -> None:
            command_id = str(item.get("command_id") or "")
            if command_id and command_id not in seen:
                evidence.append(item)
                seen.add(command_id)

        for context_name in dict.fromkeys(name for name in required_context_names if name):
            name_key = context_name.casefold()
            matching_candidates = [
                item
                for item in initial_evidence
                if normalized_command_name(item.get("canonical_name")) == name_key
                or (
                    name_key == "ip address"
                    and normalized_command_name(item.get("canonical_name")).startswith(name_key)
                )
            ]
            matching = min(
                matching_candidates,
                key=lambda item: context_sort_key(item, name_key),
                default=None,
            )
            if matching:
                add_context_evidence(matching)
                continue
            catalog_matches = session.scalars(
                select(Command)
                .where(Command.manual_id == manual_id)
                .where(Command.canonical_name.ilike(f"{context_name}%"))
                .limit(16)
            ).all()
            for command in catalog_matches:
                item = _evidence_from_command(
                    command,
                    expected_name=None,
                    source="topology_required_context",
                    score=1.0,
                )
                add_context_evidence(item)
                break

        # Preserve literal handbook titles from the requirement/intent before
        # the ReAct model's broad candidate list.  The model may fail to emit
        # its retrieval JSON, but an exact local catalogue hit such as
        # ``stack member`` must still reach command composition rather than be
        # crowded out by neighbouring pages.  This is title matching over the
        # selected manual, not a protocol-specific fallback.
        anchor_terms = [
            term.casefold()
            for term in seed_queries
            if len(term) <= 80 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*", term)
        ]
        for anchor in dict.fromkeys(anchor_terms):
            for item in initial_evidence:
                command_id = str(item.get("command_id") or "")
                if (
                    not command_id
                    or command_id in seen
                    or normalized_command_name(item.get("canonical_name")) != anchor
                ):
                    continue
                add_context_evidence(item)
                break

        # The final ReAct decision can name a precise missing action such as
        # ``mode lacp-static`` or ``stp enable``. Hybrid ranking keeps useful
        # neighbours, but a broad protocol page or a display command can still
        # outrank that exact grammar on a heterogeneous CHM extraction. Resolve
        # those already-issued queries directly against the selected manual's
        # command catalogue before adding fuzzy candidates. This is local SQL
        # lookup only: it adds no Embedding request, no vendor allow-list, and
        # works for any imported manual whose syntax records command words.
        exact_query_terms: list[str] = []
        for round_audit in outcome.get("rounds", []):
            if not isinstance(round_audit, dict):
                continue
            exact_query_terms.extend(
                str(item).strip()
                for item in [
                    *round_audit.get("tail_queries", []),
                    *round_audit.get("unresolved_queries", []),
                ]
                if isinstance(item, str) and item.strip()
            )
        exact_query_terms.extend(str(item).strip() for item in seed_queries if str(item).strip())

        def exact_query_rank(command: Command, tokens: list[str]) -> tuple[int, int, int, str]:
            name = normalized_command_name(command.canonical_name)
            writable = not name.startswith(("display ", "show ", "reset ", "undo "))
            canonical_hits = sum(token in name for token in tokens)
            literal_syntax = any(
                re.sub(r"\s+", " ", str(value)).strip().casefold().startswith(" ".join(tokens))
                for value in _load(command.syntax_json)
            )
            return (
                0 if writable else 1,
                0 if literal_syntax else 1,
                -canonical_hits,
                name,
            )

        for query in list(dict.fromkeys(exact_query_terms))[:12]:
            tokens = [
                token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", query) if len(token) >= 2
            ][:4]
            if len(tokens) < 2:
                continue
            token_filters = [
                or_(
                    Command.canonical_name.ilike(f"%{token}%"),
                    Command.syntax_json.ilike(f"%{token}%"),
                )
                for token in tokens
            ]
            catalog_matches = session.scalars(
                select(Command)
                .where(Command.manual_id == manual_id)
                .where(*token_filters)
                # Apply the relevance sort below before taking the small
                # evidence slice. A CHM can have hundreds of unrelated pages
                # sharing the first word (for example ``ip`` or ``mode``);
                # an early SQL LIMIT previously hid the exact grammar page.
                .limit(240)
            ).all()
            catalog_matches.sort(key=lambda command: exact_query_rank(command, tokens))
            for command in catalog_matches[:2]:
                direct_item = _evidence_from_command(
                    command,
                    expected_name=None,
                    source="manual_exact_query_anchor",
                    score=1.0,
                )
                direct_item["active_retrieval_priority"] = -2
                add_context_evidence(direct_item)

        # Active retrieval records formal command titles recovered from the
        # selected manual's own catalogue.  Promote multi-word roots (such as
        # a protocol's base command) into the evidence packet before fuzzy
        # candidates. This keeps a broad capability query from crowding out
        # its actual command family under a finite context budget.
        for anchor in outcome.get("catalog_anchors", []):
            anchor_text = str(anchor).strip()
            if len(anchor_text.split()) < 2:
                continue
            command = session.scalar(
                select(Command)
                .where(Command.manual_id == manual_id)
                .where(Command.canonical_name.ilike(anchor_text))
                .limit(1)
            )
            if command:
                add_context_evidence(
                    _evidence_from_command(
                        command,
                        expected_name=None,
                        source="manual_catalog_anchor",
                        score=1.0,
                    )
                )

        # ``active_manual_search`` orders final follow-up queries first.  Those
        # are the explicit missing actions discovered by the ReAct loop and
        # must reach the command planner before merely high-scoring neighbouring
        # pages.  The selected IDs remain in the list; ordering alone never
        # grants a command permission.
        candidates = list(outcome.get("candidates", []))
        for candidate in candidates:
            command_id = str(candidate.get("command_id") or "")
            if not command_id or command_id in seen:
                continue
            evidence.append(
                {
                    "command_id": command_id,
                    "document_id": candidate.get("document_id"),
                    "canonical_name": candidate.get("canonical_name"),
                    "matched_command": None,
                    "syntax": candidate.get("syntax", []),
                    "views": candidate.get("views", []),
                    "parameters": candidate.get("parameters", []),
                    "preconditions": candidate.get("preconditions", []),
                    "constraints": candidate.get("constraints", []),
                    "examples": candidate.get("examples", []),
                    "source_path": candidate.get("source_path"),
                    "retrieval_score": candidate.get("score"),
                    "retrieval_sources": candidate.get("retrieval_sources", []),
                    "active_retrieval_priority": candidate.get("active_retrieval_priority"),
                }
            )
            seen.add(command_id)
            if len(evidence) >= 18:
                break
        # Keep hybrid hits as useful neighbouring context, but never let them
        # displace a command page explicitly selected by the retrieval node.
        for item in initial_evidence:
            command_id = str(item.get("command_id") or "")
            if not command_id or command_id in seen:
                continue
            evidence.append(item)
            seen.add(command_id)
            if len(evidence) >= 18:
                break

        # Command manuals commonly split a feature into a base page and
        # parameter variants (for example ``vrrp vrid`` and ``vrrp vrid
        # priority``). Expand only families already recovered from the manual;
        # this is catalogue navigation, not a protocol-specific command map.
        requirement_token_counts: dict[str, int] = {}
        for term in [*seed_queries, requirement]:
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", term):
                if len(token) >= 3:
                    normalized_token = token.casefold()
                    requirement_token_counts[normalized_token] = (
                        requirement_token_counts.get(normalized_token, 0) + 1
                    )
        requirement_tokens = set(requirement_token_counts)
        retrieval_phrases = {
            phrase.casefold()
            for term in seed_queries
            for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", term)
            if len(phrase) >= 2
        }

        def manual_relevance(command: Command) -> int:
            material = " ".join(
                [
                    normalized_command_name(command.canonical_name),
                    str(command.feature or ""),
                    str(command.document.text_content or "")[:6_000],
                ]
            ).casefold()
            return sum(
                requirement_token_counts.get(token, 0) for token in requirement_tokens if token in material
            ) + sum(1 for phrase in retrieval_phrases if phrase in material)

        family_names = [
            normalized_command_name(item.get("canonical_name"))
            for item in evidence
            if (
                len(normalized_command_name(item.get("canonical_name")).split()) >= 2
                # A one-word writable root (``stack``, ``ospf``, ``vrrp``)
                # is often the handbook's entry point for a family whose
                # parameter actions have more specific titles. Do not expand
                # display/show roots: they add read-only noise without helping
                # command composition.
                or (
                    len(normalized_command_name(item.get("canonical_name")).split()) == 1
                    and not normalized_command_name(item.get("canonical_name")).startswith(
                        ("display", "show", "reset", "undo")
                    )
                )
            )
        ]
        for family_name in sorted(
            dict.fromkeys(family_names),
            key=lambda name: (
                -max(
                    (
                        manual_relevance(command)
                        for command in session.scalars(
                            select(Command)
                            .where(Command.manual_id == manual_id)
                            .where(Command.canonical_name.ilike(f"{name}%"))
                            .limit(24)
                        ).all()
                    ),
                    default=0,
                ),
                len(name.split()),
                name,
            ),
        ):
            family_commands = session.scalars(
                select(Command)
                .where(Command.manual_id == manual_id)
                .where(Command.canonical_name.ilike(f"{family_name} %"))
                .limit(24)
            ).all()
            family_commands.sort(
                key=lambda command: (
                    -manual_relevance(command),
                    len(normalized_command_name(command.canonical_name).split()),
                    normalized_command_name(command.canonical_name),
                )
            )
            for command in family_commands[:6]:
                family_item = _evidence_from_command(
                    command,
                    expected_name=None,
                    source="manual_command_family",
                    score=1.0,
                )
                family_item["active_retrieval_priority"] = 0
                family_item["manual_requirement_relevance"] = manual_relevance(command)
                add_context_evidence(family_item)
                if len(evidence) >= 120:
                    break
            if len(evidence) >= 120:
                break
        return {
            "evidence": evidence[:120],
            "audit": {
                "status": outcome.get("status"),
                "mode": "generic_active_retrieval",
                "selected_command_ids": list(outcome.get("selected_command_ids", [])),
                "rounds": outcome.get("rounds", []),
                # These are precise actions the retrieval node said were
                # missing.  They remain auditable input to the command
                # compiler, which may only use a term when it maps to a
                # complete global CLI on an imported manual page.
                "followup_terms": list(
                    dict.fromkeys(
                        str(term).strip()
                        for round_audit in outcome.get("rounds", [])
                        if isinstance(round_audit, dict)
                        for term in [
                            *round_audit.get("tail_queries", []),
                            *round_audit.get("unresolved_queries", []),
                        ]
                        if isinstance(term, str) and term.strip()
                    )
                ),
            },
        }
    outcome = active_manual_search(
        session,
        manual_id=manual_id,
        requirement=requirement,
        seed_queries=[*missing, *_generic_retrieval_terms(intent, dialect)],
        topology_context=dict(intent.get("topology_context") or {}),
        confirmed_idea=str(intent.get("confirmed_planning_idea") or ""),
        known_actions=[*missing, *_generic_retrieval_terms(intent, dialect)],
        on_progress=event_sink,
        cancel_event=cancel_event,
    )
    selected = set(outcome.get("selected_command_ids", []))
    seen = {str(item.get("command_id")) for item in evidence}
    for candidate in outcome.get("candidates", []):
        command_id = str(candidate.get("command_id") or "")
        name = str(candidate.get("canonical_name") or "").casefold()
        prefix = _command_prefixes_for_intent(intent, dialect).get(name)
        if (
            not command_id
            or command_id not in selected
            or command_id in seen
            or not prefix
            or not _evidence_matches_command(candidate, name, prefix)
        ):
            continue
        evidence.append(
            {
                "command_id": command_id,
                "document_id": candidate.get("document_id"),
                "canonical_name": candidate["canonical_name"],
                "matched_command": name,
                "syntax": candidate.get("syntax", []),
                "views": candidate.get("views", []),
                "parameters": candidate.get("parameters", []),
                "preconditions": candidate.get("preconditions", []),
                "constraints": candidate.get("constraints", []),
                "examples": candidate.get("examples", []),
                "source_path": candidate.get("source_path"),
                "retrieval_score": candidate.get("score"),
                "retrieval_sources": candidate.get("retrieval_sources", []),
            }
        )
        seen.add(command_id)
    return {
        "evidence": evidence[:40],
        "audit": {
            "status": outcome.get("status"),
            "missing_before_recovery": missing,
            "selected_command_ids": list(outcome.get("selected_command_ids", [])),
            "rounds": outcome.get("rounds", []),
        },
    }


def _multi_vlan_device_scope(
    graph: dict[str, Any],
    node_id: str,
    intent: dict[str, Any],
    protected_ports: set[str],
) -> dict[str, Any]:
    """Turn the drawn links into Access, Trunk and VLANIF roles for one switch."""

    nodes = {str(item.get("id")): item for item in graph.get("nodes", [])}
    vlan_ids = [int(value) for value in intent.get("vlan_ids", [])]
    pc_vlan_map = {str(key): int(value) for key, value in dict(intent.get("pc_vlan_map", {})).items()}
    access_ports: list[dict[str, Any]] = []
    trunk_ports: list[str] = []
    warnings: list[str] = []
    for link in graph.get("links", []):
        if link.get("source") == node_id:
            port = str(link.get("source_port", "")).strip()
            peer_id = str(link.get("target"))
        elif link.get("target") == node_id:
            port = str(link.get("target_port", "")).strip()
            peer_id = str(link.get("source"))
        else:
            continue
        if not port or port.upper() == "UNMAPPED":
            warnings.append("存在未填写接口名的连线，未为该连线生成命令。")
            continue
        if port_identity(port) in protected_ports:
            warnings.append(f"端口 {port} 被用户标为受保护端口，未自动生成配置。")
            continue
        peer = nodes.get(peer_id, {})
        if peer.get("kind") == "pc":
            vlan_id = pc_vlan_map.get(peer_id)
            if vlan_id:
                access_ports.append({"port": port, "vlan_id": vlan_id, "peer": peer.get("name", peer_id)})
            else:
                warnings.append(f"PC {peer.get('name', peer_id)} 未从需求解析到 VLAN，未配置端口 {port}。")
        elif peer.get("kind") == "switch":
            trunk_ports.append(port)

    vlanifs: list[dict[str, Any]] = []
    if str(intent.get("l3_core_node_id") or "") == node_id:
        for vlan_id in vlan_ids:
            gateway = dict(intent.get("vlan_gateways", {})).get(str(vlan_id))
            if gateway:
                vlanifs.append({"vlan_id": vlan_id, **gateway})
            else:
                warnings.append(f"VLAN {vlan_id} 缺少可用网关地址，未生成 Vlanif{vlan_id}。")
    return {
        "access_ports": access_ports,
        "trunk_ports": list(dict.fromkeys(trunk_ports)),
        "vlanifs": vlanifs,
        "all_ports": [*[item["port"] for item in access_ports], *trunk_ports],
        "warnings": warnings,
    }


def _generic_device_scope(
    graph: dict[str, Any],
    node_id: str,
    protected_ports: set[str],
    intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose only current-device topology facts to the universal planner."""

    nodes = {str(item.get("id")): item for item in graph.get("nodes", [])}
    current = nodes.get(node_id, {})
    explicit_l3_ports = [
        str(item.get("port") or item.get("interface") or "").strip()
        for item in (intent or {}).get("required_configuration_facts", [])
        if isinstance(item, dict)
        and item.get("kind") == "interface_address"
        and str(item.get("port") or "").strip()
    ]
    explicit_l3_keys = {port_identity(port) for port in explicit_l3_ports}
    links: list[dict[str, Any]] = []
    all_ports: list[str] = []
    warnings: list[str] = []
    for link in graph.get("links", []):
        if link.get("source") == node_id:
            local_port = str(link.get("source_port") or "").strip()
            peer_id = str(link.get("target"))
            peer_port = str(link.get("target_port") or "").strip()
        elif link.get("target") == node_id:
            local_port = str(link.get("target_port") or "").strip()
            peer_id = str(link.get("source"))
            peer_port = str(link.get("source_port") or "").strip()
        else:
            continue
        if not local_port or local_port.upper() == "UNMAPPED":
            warnings.append("存在未填写本端接口名的连线；该连线不能自动生成物理接口命令。")
            continue
        peer = nodes.get(peer_id, {})
        protected = port_identity(local_port) in protected_ports
        links.append(
            {
                "local_port": local_port,
                "peer_id": peer_id,
                "peer_name": peer.get("name") or peer.get("label") or peer_id,
                "peer_kind": peer.get("kind"),
                "peer_port": peer_port or None,
                "peer_ip": peer.get("ip"),
                "peer_prefix": peer.get("prefix"),
                "peer_gateway": peer.get("gateway"),
                "protected": protected,
            }
        )
        if protected:
            warnings.append(f"端口 {local_port} 被用户标为受保护端口；通用计划不得进入该接口。")
        else:
            all_ports.append(local_port)
    vlan_l2_roles: dict[str, Any] = {}
    vlan_ids = [int(value) for value in (intent or {}).get("vlan_ids", []) if str(value).isdigit()]
    pc_vlan_map = {
        str(key): int(value)
        for key, value in dict((intent or {}).get("pc_vlan_map", {})).items()
        if str(value).isdigit()
    }
    if vlan_ids and pc_vlan_map:
        access_roles: list[dict[str, Any]] = []
        trunk_ports: list[str] = []
        for link in links:
            local_port = str(link.get("local_port") or "")
            local_key = port_identity(local_port)
            if link.get("protected") or local_key in explicit_l3_keys:
                continue
            peer_id = str(link.get("peer_id") or "")
            if link.get("peer_kind") == "pc" and peer_id in pc_vlan_map:
                access_roles.append(
                    {
                        "port": local_port,
                        "vlan_id": pc_vlan_map[peer_id],
                        "peer_id": peer_id,
                    }
                )
            elif link.get("peer_kind") == "switch":
                trunk_ports.append(local_port)
        vlan_l2_roles = {
            "vlan_ids": list(dict.fromkeys(vlan_ids)),
            "access_ports": access_roles,
            "trunk_ports": list(dict.fromkeys(trunk_ports)),
        }
    return {
        "mode": "generic_topology_scope",
        "device": {
            "id": node_id,
            "name": current.get("name") or current.get("label") or node_id,
            "model": current.get("detected_model") or current.get("model_name"),
        },
        "links": links,
        "all_ports": list(dict.fromkeys(all_ports)),
        "explicit_l3_ports": list(dict.fromkeys(explicit_l3_ports)),
        "vlan_l2_roles": vlan_l2_roles,
        "protected_ports": sorted(protected_ports),
        "warnings": warnings,
    }


def _topology_context_for_llm(graph: dict[str, Any]) -> dict[str, Any]:
    """Project every saved topology fact into a prompt-safe, readable shape.

    Credentials are intentionally absent.  The same projection is reused by
    the idea, retrieval and command nodes so a model cannot see a partial view
    of the network in one stage and invent a different connection later.
    """

    def present(value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return "未提供"
        return value

    def name_for(node: dict[str, Any], fallback: str) -> str:
        return str(node.get("name") or node.get("label") or fallback)

    nodes = {str(item.get("id")): item for item in graph.get("nodes", [])}
    devices: list[dict[str, Any]] = []
    connections: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in nodes}
    links: list[dict[str, Any]] = []
    switch_to_switch = 0
    switch_to_pc = 0
    other_links = 0

    for link in graph.get("links", []):
        source_id = str(link.get("source") or "")
        target_id = str(link.get("target") or "")
        source = nodes.get(source_id, {})
        target = nodes.get(target_id, {})
        source_kind = str(source.get("kind") or "unknown")
        target_kind = str(target.get("kind") or "unknown")
        if source_kind == target_kind == "switch":
            link_type = "switch_to_switch"
            switch_to_switch += 1
        elif {source_kind, target_kind} == {"switch", "pc"}:
            link_type = "switch_to_pc"
            switch_to_pc += 1
        else:
            link_type = "other"
            other_links += 1
        source_port = present(link.get("source_port"))
        target_port = present(link.get("target_port"))
        if str(source_port).strip().upper() == "UNMAPPED":
            source_port = "未提供"
        if str(target_port).strip().upper() == "UNMAPPED":
            target_port = "未提供"
        link_entry = {
            "link_id": str(link.get("id") or "未提供"),
            "link_type": link_type,
            "source": {"device": name_for(source, source_id), "kind": source_kind, "port": source_port},
            "target": {"device": name_for(target, target_id), "kind": target_kind, "port": target_port},
        }
        links.append(link_entry)
        if source_id in connections:
            connections[source_id].append(
                {
                    "link_id": link_entry["link_id"],
                    "link_type": link_type,
                    "peer_name": name_for(target, target_id),
                    "peer_kind": target_kind,
                    "local_port": source_port,
                    "peer_port": target_port,
                }
            )
        if target_id in connections:
            connections[target_id].append(
                {
                    "link_id": link_entry["link_id"],
                    "link_type": link_type,
                    "peer_name": name_for(source, source_id),
                    "peer_kind": source_kind,
                    "local_port": target_port,
                    "peer_port": source_port,
                }
            )

    device_field_status: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        field_values = {
            "ip": present(node.get("ip")),
            "prefix": present(node.get("prefix")),
            "gateway": present(node.get("gateway")),
        }
        devices.append(
            {
                "id": node_id,
                "name": name_for(node, node_id),
                "kind": str(node.get("kind") or "unknown"),
                "model": present(
                    node.get("detected_model") or node.get("model_name") or node.get("model_id")
                ),
                **field_values,
                "connections": connections.get(node_id, []),
            }
        )
        device_field_status.append(
            {
                "device": name_for(node, node_id),
                "kind": str(node.get("kind") or "unknown"),
                "ip": field_values["ip"],
                "prefix": field_values["prefix"],
                "gateway": field_values["gateway"],
                "connection_count": len(connections.get(node_id, [])),
            }
        )

    missing_link_endpoints = [
        {
            "link_id": item["link_id"],
            "source": item["source"],
            "target": item["target"],
        }
        for item in links
        if item["source"]["port"] == "未提供" or item["target"]["port"] == "未提供"
    ]
    unconnected_devices = [
        item["name"]
        for item in devices
        if not connections.get(str(item["id"]), [])
    ]
    topology_input_warnings: list[str] = []
    if missing_link_endpoints:
        topology_input_warnings.append("存在未填写完整真实接口名的链路；模型不能据此确定端口命令。")
    if unconnected_devices:
        topology_input_warnings.append(
            "存在未接入任何链路的设备：" + ", ".join(unconnected_devices) + "。请确认是否为有意保留。"
        )
    if other_links:
        topology_input_warnings.append(
            f"存在 {other_links} 条非交换机-交换机或 PC-交换机链路；已完整传入模型，但请确认其业务含义。"
        )
    topology_input_status = (
        "complete"
        if not missing_link_endpoints and not unconnected_devices and not other_links
        else "partial"
    )
    return {
        "devices": devices,
        "links": links,
        "device_field_status": device_field_status,
        "coverage": {
            "device_count": len(devices),
            "link_count": len(links),
            "switch_to_switch_links": switch_to_switch,
            "switch_to_pc_links": switch_to_pc,
            "other_links": other_links,
            "all_saved_links_included": True,
            "topology_input_status": topology_input_status,
            "missing_link_endpoint_count": len(missing_link_endpoints),
            "unconnected_device_count": len(unconnected_devices),
            "missing_link_endpoints": missing_link_endpoints,
            "unconnected_devices": unconnected_devices,
            "warnings": topology_input_warnings,
        },
    }


def _topology_device_context(topology_context: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Return the prompt-safe device record for the current per-device plan."""

    return next(
        (
            dict(item)
            for item in topology_context.get("devices", [])
            if isinstance(item, dict) and str(item.get("id")) == node_id
        ),
        {"id": node_id, "name": node_id, "kind": "switch", "model": "未提供", "connections": []},
    )


def _candidate_multi_vlan_intervlan_commands(
    intent: dict[str, Any], evidence: list[dict[str, Any]], scope: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    required = _required_command_names(intent)
    found = {
        name
        for name, prefix in _command_prefixes_for_intent(intent).items()
        if any(_evidence_matches_command(item, name, prefix) for item in evidence)
    }
    missing = sorted(required - found)
    vlan_ids = [int(value) for value in intent.get("vlan_ids", [])]
    if not vlan_ids:
        return [], {"status": "draft_with_warnings", "errors": [], "warnings": ["需求没有有效 VLAN ID。"]}
    commands = ["system-view", f"vlan batch {' '.join(str(value) for value in vlan_ids)}"]
    for item in scope.get("access_ports", []):
        commands.extend(
            [
                f"interface {item['port']}",
                "port link-type access",
                f"port default vlan {item['vlan_id']}",
                "quit",
            ]
        )
    for port in scope.get("trunk_ports", []):
        commands.extend(
            [
                f"interface {port}",
                "port link-type trunk",
                f"port trunk allow-pass vlan {' '.join(str(value) for value in vlan_ids)}",
                "quit",
            ]
        )
    for svi in scope.get("vlanifs", []):
        commands.extend(
            [
                f"interface Vlanif{svi['vlan_id']}",
                f"ip address {svi['gateway']} {svi['mask']}",
                "quit",
            ]
        )
    commands.append("return")
    if len(commands) == 3:
        return [], {
            "status": "draft_with_warnings",
            "errors": [],
            "warnings": ["此设备在当前拓扑中没有可生成的 Access、Trunk 或 VLANIF 配置。"],
        }
    return commands, {
        # Topology determines the device-local Access/Trunk/VLANIF slice.  A
        # missing manual page should make the result an editable draft, not
        # erase that useful per-device command proposal.
        "status": "ready" if not missing else "draft_with_warnings",
        "errors": [],
        "warnings": [
            *([f"手册中未检索到适用于当前视图的命令证据：{', '.join(missing)}。"] if missing else []),
            *list(scope.get("warnings", [])),
        ],
        "checks": [
            "Access 端口仅来自直连 PC 的已解析 VLAN 映射",
            "交换机互连端口使用 Trunk 并放通全部业务 VLAN",
            "Vlanif 仅生成在需求指定或拓扑识别的三层交换机",
            "Vlanif 地址来自 PC 网关字段或明确标注的实验默认地址",
        ],
        "validation_commands": [
            f"display vlan {' '.join(str(value) for value in vlan_ids)}",
            *[f"display port vlan {item['port']}" for item in scope.get("access_ports", [])],
            *[f"display port vlan {port}" for port in scope.get("trunk_ports", [])],
            *[f"display interface Vlanif{svi['vlan_id']}" for svi in scope.get("vlanifs", [])],
        ],
    }


def _candidate_commands(
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    device_scope: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    if intent.get("feature") == "multi_vlan_intervlan":
        return _candidate_multi_vlan_intervlan_commands(intent, evidence, device_scope or {})
    if intent["feature"] != "vlan_access" or not intent["vlan_ids"]:
        return [], {"status": "blocked", "errors": ["需求未形成可验证的配置意图。"]}
    if not topology_ports:
        return [], {"status": "blocked", "errors": ["没有已映射的交换机端口；禁止猜测接口。"]}
    evidence_by_name: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name, prefix in VLAN_ACCESS_COMMAND_PREFIXES.items():
        matching = next(
            (item for item in evidence if _evidence_matches_command(item, name, prefix)),
            None,
        )
        if matching:
            evidence_by_name[name] = matching
        else:
            missing.append(name)
    if missing:
        return [], {"status": "blocked", "errors": [f"手册证据不完整：缺少 {', '.join(sorted(missing))}"]}
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
    device_scope: dict[str, Any] | None = None,
    event_sink: PlanningEventSink | None = None,
    cancel_event: Event | None = None,
    dialect: CliDialect = HUAWEI_VRP,
) -> dict[str, Any]:
    _emit(event_sink, "命令计划", "stage", "LLM 正在选择手册证据和命令参数。")
    plan, llm = plan_commands_with_llm(
        session,
        requirement=requirement,
        intent=intent,
        evidence=evidence,
        topology_ports=topology_ports,
        device_scope=device_scope,
        on_event=event_sink,
        cancel_event=cancel_event,
        dialect=dialect,
    )
    if cancel_event and cancel_event.is_set():
        raise PlanningCancelled("用户已停止配置规划")
    _emit(event_sink, "命令计划", "output", _json({"llm": llm, "has_plan": bool(plan)}))
    return {
        "command_plan": plan.model_dump(mode="json") if plan else {},
        "llm": llm,
    }


def _recover_local_syntax_evidence_for_plan(
    session: Session,
    *,
    manual_id: str,
    evidence: list[dict[str, Any]],
    command_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Bind generated CLI to exact pages in the already selected manual.

    The initial retrieval packet is intentionally compact.  After the command
    planner has named concrete CLI, a local-only syntax pass can recover pages
    such as ``mode（Eth-Trunk接口视图）`` and ``stp enable`` that were not in
    that compact packet.  It never creates a command, calls Embedding, or
    makes another LLM request; it only adds handbook pages whose extracted
    grammar accepts an existing model-generated line.
    """

    if not command_plan:
        return evidence
    try:
        from app.schemas import LlmCommandPlan

        parsed = LlmCommandPlan.model_validate(command_plan)
    except Exception:
        return evidence

    recovered = list(evidence)
    known_ids = {str(item.get("command_id") or "") for item in recovered}
    candidates = session.scalars(select(Command).where(Command.manual_id == manual_id)).all()
    item_cache: dict[str, dict[str, Any]] = {}

    def as_evidence(command: Command) -> dict[str, Any]:
        item = item_cache.get(command.id)
        if item is None:
            item = _evidence_from_command(
                command,
                expected_name=None,
                source="post_plan_local_syntax_recovery",
                score=1.0,
            )
            item_cache[command.id] = item
        return item

    def relevance(item: dict[str, Any], cli: str) -> tuple[int, int, str]:
        canonical = _normalize_cli(
            str(item.get("canonical_name") or "").split("（", 1)[0].split("(", 1)[0]
        ).casefold()
        normalized_cli = _normalize_cli(cli).casefold()
        if canonical == normalized_cli:
            return (0, 0, canonical)
        exact_syntax = any(_normalize_cli(form).casefold() == normalized_cli for form in _syntax_forms(item))
        if exact_syntax:
            return (0, 1, canonical)
        return (1, -len(canonical.split()), canonical)

    for operation in parsed.operations:
        for invocation in operation.invocations:
            cli = _normalize_cli(str(invocation.cli or ""))
            if not cli:
                continue
            lowered = cli.casefold()
            if invocation.command_id == CONTROL_COMMAND_ID and lowered in {
                "quit",
                "return",
                "end",
                "exit",
            }:
                continue
            root = lowered.split(maxsplit=1)[0]
            matching: list[dict[str, Any]] = []
            for command in candidates:
                # Avoid JSON parsing for pages that cannot possibly contain the
                # generated command root.  This is a local catalogue filter,
                # not a second retrieval pass.
                material = f"{command.canonical_name} {command.syntax_json}".casefold()
                if root not in material:
                    continue
                item = as_evidence(command)
                if _matches_evidence_syntax(cli, item):
                    matching.append(item)
            for item in sorted(matching, key=lambda value: relevance(value, cli))[:3]:
                command_id = str(item.get("command_id") or "")
                if not command_id or command_id in known_ids:
                    continue
                recovered.append(item)
                known_ids.add(command_id)
    return recovered


def _render_command_plan_or_fallback(
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    command_plan: dict[str, Any] | None,
    device_scope: dict[str, Any] | None = None,
    dialect: CliDialect = HUAWEI_VRP,
) -> tuple[list[str], dict[str, Any]]:
    def manual_reference_draft(
        *,
        warnings: list[str],
        source: str,
    ) -> tuple[list[str], dict[str, Any]]:
        """Expose the best handbook material when an LLM plan is unavailable.

        This is deliberately a *reference* fallback, not a compiler success.
        It gives the operator a non-empty, editable command panel when an
        overloaded or incompatible model supplied no usable CLI.  Handbook
        examples are never copied in as executable commands: examples often
        contain another network's addresses, ports, or mutually exclusive
        alternatives.
        """

        reference_lines: list[str] = []
        seen: set[str] = set()

        for item in evidence:
            name = " ".join(str(item.get("canonical_name") or "未知命令").split())
            syntax = " | ".join(
                " ".join(str(value).split()) for value in item.get("syntax", []) or [] if str(value).strip()
            )
            line = f"# 手册参考：{name}{'；语法：' + syntax if syntax else ''}"
            normalized = line.casefold()
            if normalized not in seen:
                seen.add(normalized)
                reference_lines.append(line)
            if len(reference_lines) >= 8:
                break

        if reference_lines:
            body = reference_lines[:8]
            warning = (
                "LLM 未返回可编译的 CLI；以下仅为手册语法参考，未将任何手册示例当作可执行命令。"
                "请补充命令草案并逐条核对后再决定是否下发。"
            )
            non_executable_reference = True
        elif evidence:
            names = [str(item.get("canonical_name") or "未知命令") for item in evidence[:6]]
            body = [f"# 未实例化手册命令参考：{', '.join(names)}"]
            warning = "已找到手册页面，但其中没有可直接复用的完整 CLI 示例；请根据手册语法和需求补充此草案。"
            non_executable_reference = True
        else:
            body = ["# 未检索到手册命令或 LLM CLI；请补充需求、手册内容或手动填写命令草案。"]
            warning = "未能取得直接手册证据或 LLM CLI。已保留可编辑占位草案，不能将其视为可执行配置。"
            non_executable_reference = True

        return (
            [*dialect.configuration_enter, *body, *dialect.configuration_exit],
            {
                "status": "draft_with_warnings",
                "errors": [],
                "warnings": [*warnings, warning],
                "unverified_draft": True,
                "non_executable_reference": non_executable_reference,
                "source": source,
            },
        )

    def unverified_draft(parsed_plan: Any, validation: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        """Keep a model's non-empty CLI draft visible when provenance cannot compile.

        Planning never sends this path automatically. It gives the operator an
        editable draft plus the exact evidence/compiler warnings, instead of
        replacing a possibly useful answer with an empty command panel.
        """

        body: list[str] = []
        for operation in getattr(parsed_plan, "operations", []):
            for invocation in getattr(operation, "invocations", []):
                cli = " ".join(str(getattr(invocation, "cli", "") or "").split())
                # This is a display-only draft. Preserve each non-empty LLM
                # proposal, including lines that failed handbook/topology
                # validation, so the operator gets an editable command set
                # rather than an empty panel. Compiler errors remain visible;
                # execution retains its separate approval/preflight checks.
                if cli:
                    body.append(cli)
        business = [item for item in body if item.casefold() not in dialect.control_commands]
        if not business:
            return [], validation
        result = [*dialect.configuration_enter, *body, *dialect.configuration_exit]
        validation["status"] = "draft_with_warnings"
        validation["warnings"] = [
            *validation.get("warnings", []),
            "以下为 LLM 已生成但未完成手册静态校验的命令草案；请逐条人工核对后再决定是否下发。",
        ]
        validation["unverified_draft"] = True
        validation.setdefault("source", "unverified_llm_command_draft")
        return result, validation

    if command_plan:
        from app.schemas import LlmCommandPlan

        try:
            parsed = LlmCommandPlan.model_validate(command_plan)
            parsed, vlan_syntax_rewrites = normalize_huawei_vlan_creation_plan(
                parsed,
                intent=intent,
                evidence=evidence,
                dialect=dialect,
            )
            commands, validation = compile_command_plan(
                parsed,
                intent=intent,
                evidence=evidence,
                topology_ports=topology_ports,
                device_scope=device_scope,
                dialect=dialect,
            )
            if vlan_syntax_rewrites:
                validation["vlan_syntax_rewrites"] = vlan_syntax_rewrites
            if validation.get("status") == "ready":
                return commands, validation

            if not is_huawei_vlan_renderer(intent, dialect):
                # If the requirement itself bound a drawn physical port to a
                # handbook command family and argument, rebuild that narrow
                # slice before exposing a raw model fallback. This is generic
                # evidence compilation, not an iStack/Eth-Trunk renderer.
                explicit_fallback = build_explicit_port_assignment_fallback_plan(
                    parsed,
                    intent=intent,
                    evidence=evidence,
                    topology_ports=topology_ports,
                    dialect=dialect,
                )
                if explicit_fallback:
                    explicit_commands, explicit_validation = compile_command_plan(
                        explicit_fallback,
                        intent=intent,
                        evidence=evidence,
                        topology_ports=topology_ports,
                        device_scope=device_scope,
                        dialect=dialect,
                    )
                    if explicit_validation.get("status") == "ready":
                        explicit_validation["source"] = "explicit_topology_manual_fallback"
                        explicit_validation["llm_plan_warning"] = validation.get("errors", [])
                        return explicit_commands, explicit_validation
                draft_commands, draft_validation = unverified_draft(parsed, validation)
                if draft_commands:
                    return draft_commands, draft_validation
            # An unreliable LLM must never remove a command set that can be
            # rendered from explicit topology facts and handbook evidence.
            fallback_commands, fallback_validation = (
                _candidate_commands(intent, evidence, topology_ports, device_scope)
                if is_huawei_vlan_renderer(intent, dialect)
                else (
                    [],
                    {
                        "status": "draft_with_warnings",
                        "errors": [],
                        "warnings": ["通用手册计划未通过证据或端口校验，未生成可用命令草案。"],
                    },
                )
            )
            fallback_validation["llm_plan_warning"] = validation.get("errors", [])
            fallback_validation["source"] = "deterministic_fallback_after_llm_plan"
            # Generic capabilities have no safe deterministic renderer. Keep
            # the compiler errors visible, but never leave the operator with
            # an empty command panel after all LLM fallbacks have been tried.
            if not fallback_commands:
                return manual_reference_draft(
                    warnings=[
                        "通用手册计划未通过证据或端口校验。",
                        *[str(item) for item in validation.get("errors", [])],
                    ],
                    source="manual_reference_after_llm_plan",
                )
            return fallback_commands, fallback_validation
        except Exception as exc:
            if is_huawei_vlan_renderer(intent, dialect):
                commands, validation = _candidate_commands(intent, evidence, topology_ports, device_scope)
            else:
                return manual_reference_draft(
                    warnings=[f"通用手册命令计划解析/编译失败：{str(exc)[:240]}"],
                    source="manual_reference_after_llm_exception",
                )
            validation["llm_plan_warning"] = [f"CommandPlan 编译失败：{str(exc)[:240]}"]
            validation["source"] = "deterministic_fallback_after_llm_exception"
            return commands, validation
    # No configured/available LLM: preserve the tested deterministic baseline.
    if is_huawei_vlan_renderer(intent, dialect):
        return _candidate_commands(intent, evidence, topology_ports, device_scope)
    return manual_reference_draft(
        warnings=["未配置 LLM、LLM 不可用或 LLM 未返回可用结构化命令计划。"],
        source="manual_reference_without_llm_plan",
    )


def _render_relaxed_command_plan(
    intent: dict[str, Any],
    evidence: list[dict[str, Any]],
    topology_ports: list[str],
    command_plan: dict[str, Any] | None,
    device_scope: dict[str, Any] | None = None,
    dialect: CliDialect = HUAWEI_VRP,
) -> tuple[list[str], dict[str, Any]]:
    """Render a best-effort LLM CLI draft without an evidence/reviewer gate."""

    lines: list[str] = []
    fallback_validation: dict[str, Any] = {}
    is_multi_vlan_topology_renderer = (
        is_huawei_vlan_renderer(intent, dialect)
        and intent.get("feature") == "multi_vlan_intervlan"
        and intent.get("renderer_mode") == "huawei_vlan"
    )
    if is_multi_vlan_topology_renderer:
        # The LLM plan and manual evidence can be shared by all devices in a
        # task, but its literal CLI belongs only to the first device that
        # produced it. Recompile from this device's drawn ports every time.
        lines, fallback_validation = _candidate_commands(intent, evidence, topology_ports, device_scope)
    else:
        for operation in list((command_plan or {}).get("operations") or []):
            if not isinstance(operation, dict):
                continue
            for invocation in list(operation.get("invocations") or []):
                if not isinstance(invocation, dict):
                    continue
                cli = str(invocation.get("cli") or "").strip()
                if cli:
                    lines.extend(item.strip() for item in cli.splitlines() if item.strip())
    if not lines and is_huawei_vlan_renderer(intent, dialect):
        # Keep the established Huawei VLAN renderer as the no-LLM fallback;
        # when the model does return a plan, its CLI above remains untouched.
        lines, fallback_validation = _candidate_commands(intent, evidence, topology_ports, device_scope)
    if not lines:
        for item in evidence:
            for syntax in list(item.get("syntax") or []):
                value = " ".join(str(syntax).split()).strip()
                if value and value not in lines:
                    lines.append(value)
                if len(lines) >= 24:
                    break
            if len(lines) >= 24:
                break
    if not lines:
        lines = ["# LLM 未返回可直接执行的 CLI；请根据检索到的手册内容补充命令"]
    commands = list(lines)
    enter_commands = {item.casefold() for item in dialect.configuration_enter}
    exit_commands = {item.casefold() for item in dialect.configuration_exit}
    if dialect.configuration_enter and (not commands or commands[0].casefold() not in enter_commands):
        commands = [*dialect.configuration_enter, *commands]
    if dialect.configuration_exit and (not commands or commands[-1].casefold() not in exit_commands):
        commands = [*commands, *dialect.configuration_exit]
    return commands, {
        **fallback_validation,
        "status": "relaxed_draft",
        "source": "llm_relaxed_command_plan",
        "errors": [],
        "warnings": [
            *list(fallback_validation.get("warnings", [])),
            "命令为 LLM + 手册检索生成的可编辑草案，请由用户自行审阅和修改。",
        ],
        "topology_ports": list(topology_ports),
    }


def _llm_command_review_outcome(
    session: Session,
    state: dict[str, Any],
    device_scope: dict[str, Any] | None = None,
    event_sink: PlanningEventSink | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    review_intent = dict(state.get("intent", {}))
    if device_scope is not None:
        review_intent["current_device_scope"] = device_scope
    reviewed_command_plan = dict(state.get("command_plan", {}))
    if (
        review_intent.get("feature") == "multi_vlan_intervlan"
        and review_intent.get("renderer_mode") == "huawei_vlan"
    ):
        # The model's evidence-selection plan is shared by all switches in a
        # multi-device task, while the deterministic renderer intentionally
        # creates a different command slice for each device. Passing SW1's
        # shared plan to the SW2/SW3 reviewer made it audit the wrong scope.
        # Keep the actual CLI and per-device scope authoritative here.
        reviewed_command_plan = {
            "action": "deterministic_topology_renderer",
            "note": "命令由当前设备的 Access/Trunk/VLANIF 拓扑范围确定性编译；请仅审阅本设备 CLI。",
        }
    _emit(event_sink, "命令审阅", "stage", "LLM 正在审阅当前设备命令草案。")
    review, llm = review_commands_with_llm(
        session,
        intent=review_intent,
        command_plan=reviewed_command_plan,
        commands=list(state.get("candidate_commands", [])),
        validation=dict(state.get("validation", {})),
        evidence=list(state.get("evidence", [])),
        on_event=event_sink,
        cancel_event=cancel_event,
    )
    if cancel_event and cancel_event.is_set():
        raise PlanningCancelled("用户已停止配置规划")
    _emit(
        event_sink,
        "命令审阅",
        "output",
        _json({"llm": llm, "review": review.model_dump(mode="json") if review else {}}),
    )
    return {"review": review.model_dump(mode="json") if review else {}, "llm": llm}


def _rollback_draft(intent: dict[str, Any], topology_ports: list[str]) -> dict[str, Any]:
    """Return a non-executable-until-reviewed rollback draft for the first intent."""

    if (
        intent.get("renderer_mode", "huawei_vlan") != "huawei_vlan"
        or intent.get("feature") != "vlan_access"
        or not intent.get("vlan_ids")
    ):
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


def _planning_idea_text(requirement: str, graph: dict[str, Any], intent: dict[str, Any]) -> str:
    """Build a readable, editable first-stage plan from topology facts and LLM summary."""

    model_idea = str(intent.get("llm_planning_idea") or "").strip()
    if model_idea:
        gaps = [str(item).strip() for item in intent.get("requirement_gaps", []) if str(item).strip()]
        # The editor already retains the original requirement separately. Show
        # the model's proposal directly so the operator can revise it without
        # reading framework or handbook-mechanism boilerplate first.
        sections = [model_idea]
        if gaps:
            sections.extend(["", "建议补充或确认", *[f"- {item}" for item in gaps]])
        return "\n".join(sections).strip()

    nodes = {str(item.get("id")): item for item in graph.get("nodes", [])}
    feature = str(intent.get("feature") or "generic")
    uses_huawei_vlan_renderer = intent.get("renderer_mode", "huawei_vlan") == "huawei_vlan" and feature in {
        "vlan_access",
        "multi_vlan_intervlan",
    }
    lines = ["一、目标", requirement.strip()]
    summary = str(intent.get("planning_summary") or "").strip()
    if summary:
        lines.extend(["", "二、LLM 规划说明", summary])
    capability_labels = {
        "vlan_access": "VLAN 接入口",
        "vlan_trunk": "VLAN Trunk 承载",
        "vlanif_gateway": "VLANIF 三层网关",
        "multi_vlan_intervlan": "跨 VLAN 三层互通",
        "l3_ospf_ipv4": "OSPF IPv4 动态路由",
        "static_routing": "静态路由",
        "link_aggregation": "链路聚合",
        "stp": "STP/MSTP 二层冗余",
        "vrrp": "VRRP 网关冗余",
        "acl": "IPv4 ACL 访问控制",
    }
    topology_capabilities = {
        str(item).strip() for item in intent.get("topology_capabilities", []) if str(item).strip()
    }
    planning_capabilities = list(
        dict.fromkeys(
            str(item).strip()
            for item in intent.get("planning_capabilities", [])
            if str(item).strip() and str(item).strip() not in {"generic", "unclassified"}
        )
    )
    has_capability_section = bool(planning_capabilities)
    has_non_vlan_capability = any(
        item not in {"vlan_access", "vlan_trunk", "vlanif_gateway", "multi_vlan_intervlan"}
        for item in planning_capabilities
    )
    composite_steps = [str(item).strip() for item in intent.get("planning_steps", []) if str(item).strip()]
    if planning_capabilities:
        lines.extend(
            [
                "",
                "三、能力组合（可编辑）",
                *[
                    f"- {capability_labels.get(item, item.replace('_', ' '))}"
                    f"（{'拓扑识别' if item in topology_capabilities else 'LLM 规划'}）"
                    for item in planning_capabilities
                ],
            ]
        )
    pc_vlan_map = {str(key): int(value) for key, value in dict(intent.get("pc_vlan_map", {})).items()}
    if pc_vlan_map:
        members: dict[int, list[str]] = {}
        for node_id, vlan_id in pc_vlan_map.items():
            members.setdefault(vlan_id, []).append(str(nodes.get(node_id, {}).get("name") or node_id))
        lines.extend(
            [
                "",
                "四、VLAN 划分" if has_capability_section else "三、VLAN 划分",
                *[f"- VLAN {vlan_id}：{'、'.join(names)}" for vlan_id, names in sorted(members.items())],
            ]
        )
    elif intent.get("vlan_ids"):
        lines.extend(
            [
                "",
                "四、VLAN 划分" if has_capability_section else "三、VLAN 划分",
                f"- 需要创建 VLAN：{', '.join(str(item) for item in intent['vlan_ids'])}",
            ]
        )

    lines.extend(["", "五、设备角色与实施范围" if has_capability_section else "四、设备角色与实施范围"])
    switches = [item for item in graph.get("nodes", []) if item.get("kind") == "switch"]
    for switch in switches:
        node_id = str(switch.get("id"))
        name = str(switch.get("name") or switch.get("label") or node_id)
        _all_ports, protected_ports = _switch_ports_from_topology(graph, node_id)
        if uses_huawei_vlan_renderer and feature == "multi_vlan_intervlan":
            scope = _multi_vlan_device_scope(graph, node_id, intent, protected_ports)
            access = [f"{item['port']}→VLAN {item['vlan_id']}" for item in scope["access_ports"]]
            trunk = list(scope["trunk_ports"])
            vlanifs = list(scope["vlanifs"])
            details: list[str] = []
            if access:
                details.append(f"接入口 {', '.join(access)}")
            if trunk:
                vlan_text = ", ".join(str(item) for item in intent.get("vlan_ids", []))
                details.append(f"上联 Trunk {', '.join(trunk)} 放通 VLAN {vlan_text}")
            if vlanifs:
                details.append(
                    "三层网关 "
                    + ", ".join(
                        f"Vlanif{item['vlan_id']}={item['gateway']}/{item['prefix']}" for item in vlanifs
                    )
                )
            lines.append(
                f"- {name}：{'；'.join(details) if details else '未从当前拓扑推导出需要自动配置的接口'}。"
            )
        elif uses_huawei_vlan_renderer and feature == "vlan_access":
            access_ports = [
                port
                for port in _pc_facing_ports_from_topology(graph, node_id)
                if port_identity(port) not in protected_ports
            ]
            vlan_text = ", ".join(str(item) for item in intent.get("vlan_ids", [])) or "待确认"
            details = (
                f"接入口 {', '.join(access_ports)} 配置为 Access VLAN {vlan_text}"
                if access_ports
                else "未从当前拓扑推导出接入口"
            )
            lines.append(f"- {name}：{details}。")
        else:
            scope = _generic_device_scope(graph, node_id, protected_ports)
            ports = [
                f"{item['local_port']}→{item['peer_name']}"
                for item in scope["links"]
                if not item["protected"]
            ]
            protected = [item["local_port"] for item in scope["links"] if item["protected"]]
            details = f"可规划物理链路：{'、'.join(ports)}" if ports else "未从当前拓扑推导出可写物理接口"
            if protected:
                details += f"；受保护端口：{'、'.join(protected)}"
            lines.append(f"- {name}：{details}。")

    if has_non_vlan_capability and composite_steps:
        lines.extend(
            [
                "",
                "六、实施顺序",
                *[f"{index}. {step}" for index, step in enumerate(composite_steps, start=1)],
                "",
                "七、可编辑说明",
                "这是包含多个能力的 LLM 规划草案。可直接调整能力范围、实施顺序、角色说明、"
                "地址和约束；确认后再进入命令生成，命令不会自动下发。",
            ]
        )
        return "\n".join(lines).strip()

    if not uses_huawei_vlan_renderer:
        steps = [str(item).strip() for item in intent.get("planning_steps", []) if str(item).strip()]
        if not steps:
            steps = [
                "核对当前拓扑中的设备、链路、地址、受保护端口和需求约束。",
                "按需检索已选择手册，确认配置命令、视图、前置条件和适用限制。",
                "为每台设备生成仅引用当前手册证据和当前拓扑端口的命令草案。",
                "查看设备侧只读验证命令与 PC 连通性验收项，再决定是否逐台下发。",
            ]
        lines.extend(
            [
                "",
                "六、实施顺序" if has_capability_section else "五、实施顺序",
                *[f"{index}. {step}" for index, step in enumerate(steps, start=1)],
                "",
                "七、可编辑说明" if has_capability_section else "六、可编辑说明",
                "这是 LLM 生成的可编辑规划草案。可直接修改目标、设备角色、地址、实施顺序和约束；"
                "设备、物理端口与地址等结构化事实变更时，请同时更新拓扑或需求。"
                "确认后，系统会让 LLM 结合所选手册和当前拓扑生成命令草案；"
                "命令不会自动下发，由用户审阅和决定。",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "六、实施顺序" if has_capability_section else "五、实施顺序",
                "1. 在相关交换机创建 VLAN。",
                "2. 配置 PC 直连端口为 Access；配置交换机互联端口为 Trunk。",
                "3. 若需要 VLAN 间互通，在三层核心创建 VLANIF 网关地址。",
                "4. 生成命令后逐台查看并决定是否下发；验收 VLAN、端口状态和 PC 连通性。",
                "",
                "七、可编辑说明" if has_capability_section else "六、可编辑说明",
                "可在此修改实施顺序、角色说明和约束。若修改 VLAN、设备、端口或 IP 等结构化事实，"
                "请同时更新需求或拓扑；最终 CLI 仍以确认后的思路、需求、拓扑和所选手册共同生成。",
            ]
        )
    return "\n".join(lines).strip()


def create_config_task_record(session: Session, payload: ConfigTaskCreate) -> ConfigTask:
    """Persist a lightweight task before the potentially slow idea LLM call."""

    if payload.task_id and session.get(ConfigTask, payload.task_id):
        raise ValueError("规划任务 ID 已存在")
    revision = session.get(TopologyRevision, payload.topology_revision_id)
    manual = session.get(Manual, payload.manual_id)
    if not revision:
        raise ValueError("拓扑 revision 不存在")
    if not manual:
        raise ValueError("手册不存在")
    if manual.status not in {ImportStatus.completed, ImportStatus.completed_with_issues}:
        raise ValueError("手册尚未完成抽取，不能创建配置任务")
    graph = _load(revision.graph_json)
    baseline_intent = _derive_intent(payload.requirement_text, graph)
    baseline_intent["topology_context"] = _topology_context_for_llm(graph)
    dialect = resolve_cli_dialect(manual.cli_profile, manual.brand)
    # The VLAN compiler is an implementation detail of the Huawei VRP profile,
    # never a universal interpretation of a VLAN requirement.
    baseline_intent["renderer_mode"] = _renderer_mode_for_intent(baseline_intent, dialect)
    baseline_intent["cli_dialect"] = dialect.describe()
    # Command planning is intentionally a human-editable best-effort draft.
    # Retrieval remains in the graph, while evidence/reviewer gates stay out of
    # the way of novel vendor commands.
    baseline_intent["relaxed_command_mode"] = True
    # Backward-compatible archive metadata only. It is deliberately excluded
    # by all LLM prompts and does not influence planning or command generation.
    if payload.template_id:
        template = session.get(ConfigurationTemplate, payload.template_id)
        if not template:
            raise ValueError("配置模板不存在")
        snapshot = _load(template.snapshot_json)
        baseline_intent["template_reference"] = {
            "template_id": template.id,
            "title": template.title,
            "description": template.description,
            "reference_requirement": str(snapshot.get("requirement_text") or "")[:2_000],
            "reference_planning_idea": str(snapshot.get("planning_idea") or "")[:4_000],
            "reference_device_commands": [
                {
                    "device": item.get("display_name"),
                    "commands": list(item.get("commands") or [])[:40],
                }
                for item in list(snapshot.get("device_plans") or [])[:12]
            ],
            "ignored_by_generation": True,
        }
    task = ConfigTask(
        id=payload.task_id or None,
        topology_revision_id=revision.id,
        manual_id=manual.id,
        requirement_text=payload.requirement_text,
        status=TaskStatus.planning,
        intent_json=_json(baseline_intent),
    )
    session.add(task)
    session.flush()
    # Keep SQLite free while a provider is thinking so the task can be stopped
    # or restarted immediately from the UI.
    session.commit()
    session.refresh(task)
    return task


def generate_planning_idea(
    session: Session,
    task_id: str,
    *,
    event_sink: PlanningEventSink | None = None,
    cancel_event: Event | None = None,
) -> ConfigTask:
    """Generate a human-editable idea for an already persisted task."""

    task = session.get(ConfigTask, task_id)
    if not task:
        raise ValueError("配置任务不存在")
    revision = session.get(TopologyRevision, task.topology_revision_id)
    manual = session.get(Manual, task.manual_id)
    if not revision or not manual:
        raise ValueError("配置任务关联的拓扑或手册不存在")
    graph = _load(revision.graph_json)
    baseline_intent = dict(_load(task.intent_json))
    if not baseline_intent:
        baseline_intent = _derive_intent(task.requirement_text, graph)
        baseline_intent["topology_context"] = _topology_context_for_llm(graph)
    dialect = resolve_cli_dialect(manual.cli_profile, manual.brand)

    _emit(event_sink, "任务创建", "stage", "任务已创建，开始理解拓扑和配置需求。")
    check_cancel(cancel_event.is_set if cancel_event else None)

    _emit(event_sink, "意图理解", "stage", "LLM 正在起草可编辑的配置思路。")
    refinement = refine_intent_with_llm(
        session,
        requirement=task.requirement_text,
        baseline=baseline_intent,
        on_event=event_sink,
        cancel_event=cancel_event,
    )
    check_cancel(cancel_event.is_set if cancel_event else None)
    refined_intent = dict(refinement.get("intent", baseline_intent))
    # The LLM is allowed to add capability labels but not topology facts. Its
    # labels nevertheless decide whether a VLAN-only compiler could omit part
    # of a composite requirement, so select the renderer after refinement.
    refined_intent["renderer_mode"] = _renderer_mode_for_intent(refined_intent, dialect)
    refined_intent["planning_idea_llm"] = dict(refinement.get("llm", {}))
    # The first stage is an LLM-authored, human-editable proposal.  Do not
    # replace it with a fixed capability/template outline; the deterministic
    # topology facts remain in intent_json for the later command stage.
    task.planning_idea = str(refined_intent.get("llm_planning_idea") or "").strip()
    if not task.planning_idea:
        task.planning_idea = _planning_idea_text(task.requirement_text, graph, refined_intent)
    # Preserve the exact generated wording. A later command-generation run can
    # distinguish it from an operator's deliberate scope edit without schema
    # changes or trusting an LLM-created implementation step as authorization.
    refined_intent["generated_planning_idea"] = task.planning_idea
    task.intent_json = _json(refined_intent)
    task.planning_idea_revision = 1
    task.planning_idea_confirmed_at = None
    _emit(event_sink, "配置思路", "stage", "正在整理模型方案和待补充事项，准备交给用户审阅。")
    task.status = TaskStatus.idea_ready
    task.blocking_reason = None
    task.cancel_requested = False
    task.cancel_reason = None
    session.commit()
    session.refresh(task)
    _emit(event_sink, "完成", "done", "配置思路已生成，等待用户审阅和确认。")
    return task


def create_config_task(
    session: Session,
    payload: ConfigTaskCreate,
    *,
    event_sink: PlanningEventSink | None = None,
    cancel_event: Event | None = None,
) -> ConfigTask:
    """Synchronous compatibility wrapper used by service-level callers/tests."""

    task = create_config_task_record(session, payload)
    return generate_planning_idea(
        session,
        task.id,
        event_sink=event_sink,
        cancel_event=cancel_event,
    )


def update_planning_idea(session: Session, task_id: str, planning_idea: str) -> ConfigTask:
    """Persist an operator edit and invalidate unexecuted command drafts."""

    task = session.get(ConfigTask, task_id)
    if not task:
        raise ValueError("配置任务不存在")
    if any(plan.executions for plan in task.device_plans):
        raise ValueError("已有设备计划已执行，不能修改配置思路")
    for plan in list(task.device_plans):
        session.delete(plan)
    task.planning_idea = planning_idea.strip()
    task.planning_idea_revision += 1
    task.planning_idea_confirmed_at = None
    task.status = TaskStatus.idea_ready
    task.blocking_reason = None
    session.commit()
    session.refresh(task)
    return task


def _operator_edited_planning_idea(intent: dict[str, Any], confirmed_idea: str) -> bool:
    """Tell a deliberate operator edit from the initial LLM review text."""

    generated_idea = str(intent.get("generated_planning_idea") or "").strip()
    # Keep legacy tasks compatible: no stored original means the existing
    # planning-idea text remains an explicit user-provided scope input.
    return not generated_idea or confirmed_idea.strip() != generated_idea


def cancel_config_task(session: Session, task_id: str) -> ConfigTask:
    """Mark an active planning task as cancelled; workers stop at the next stream chunk/node."""

    task = session.get(ConfigTask, task_id)
    if not task:
        raise ValueError("配置任务不存在")
    if task.status != TaskStatus.planning:
        raise ValueError("当前任务没有正在运行的规划流程")
    task.cancel_requested = True
    task.cancel_reason = "用户停止了配置规划"
    task.status = TaskStatus.cancelled
    task.blocking_reason = task.cancel_reason
    session.commit()
    session.refresh(task)
    return task


def generate_config_commands(
    session: Session,
    task_id: str,
    *,
    event_sink: PlanningEventSink | None = None,
    cancel_event: Event | None = None,
) -> ConfigTask:
    """Generate per-device commands only after a non-empty idea is confirmed."""

    task = session.get(ConfigTask, task_id)
    if not task:
        raise ValueError("配置任务不存在")
    confirmed_idea = task.planning_idea.strip()
    if not confirmed_idea:
        raise ValueError("配置思路为空；请先生成或填写配置思路，再生成命令")
    if any(plan.executions for plan in task.device_plans):
        raise ValueError("已有设备计划已执行，不能重新生成命令")
    for plan in list(task.device_plans):
        session.delete(plan)
    revision = session.get(TopologyRevision, task.topology_revision_id)
    manual = session.get(Manual, task.manual_id)
    if not revision or not manual:
        raise ValueError("配置任务关联的拓扑或手册不存在")
    if manual.status not in {ImportStatus.completed, ImportStatus.completed_with_issues}:
        raise ValueError("手册尚未完成抽取，不能生成命令")
    dialect = resolve_cli_dialect(manual.cli_profile, manual.brand)
    graph = _load(revision.graph_json)
    baseline_intent = dict(_load(task.intent_json))
    if not baseline_intent:
        baseline_intent = _derive_intent(task.requirement_text, graph)
    baseline_intent["renderer_mode"] = _renderer_mode_for_intent(baseline_intent, dialect)
    baseline_intent["cli_dialect"] = dialect.describe()
    # The user can edit the idea freely.  The command stage receives exactly
    # that text as context and produces an editable best-effort draft.
    baseline_intent["confirmed_planning_idea"] = confirmed_idea
    baseline_intent["planning_steps"] = []
    baseline_intent["planning_idea_scope"] = "operator_confirmed"
    task.status = TaskStatus.planning
    task.cancel_requested = False
    task.cancel_reason = None
    task.planning_idea_confirmed_at = datetime.utcnow()
    task.blocking_reason = None
    session.commit()
    session.refresh(task)
    _emit(event_sink, "任务准备", "stage", "配置思路已确认，开始检索手册并生成逐设备命令。")
    check_cancel(cancel_event.is_set if cancel_event else None)

    plans: list[DevicePlan] = []
    llm_outcome: dict[str, Any] | None = None
    retrieval_cache: dict[str, dict[str, Any]] = {}
    command_plan_cache: dict[str, dict[str, Any]] = {}
    current_device_event_sink: PlanningEventSink | None = event_sink

    def emit_current(stage: str, event_type: str, content: str) -> None:
        _emit(current_device_event_sink, stage, event_type, content)

    def refine_once(requirement: str, baseline: dict[str, Any]) -> dict[str, Any]:
        nonlocal llm_outcome
        if llm_outcome is None:
            llm_outcome = {
                "intent": baseline,
                "llm": {
                    "status": "reused_from_confirmed_idea",
                    "node": "intent_refinement",
                    "planning_idea_revision": task.planning_idea_revision,
                },
            }
        return llm_outcome

    def retrieve_once(graph_intent: dict[str, Any]) -> dict[str, Any]:
        # All device plans in one task share the selected manual and intent.
        # Reusing the evidence avoids repeated embedding requests and gives each
        # switch the same command-source basis.
        cache_key = _json(graph_intent)
        if cache_key not in retrieval_cache:
            check_cancel(cancel_event.is_set if cancel_event else None)
            emit_current("手册检索", "stage", "正在检索已选择手册中的命令证据。")
            retrieval_cache[cache_key] = _active_evidence_recovery(
                session,
                manual_id=manual.id,
                requirement=task.requirement_text,
                intent=graph_intent,
                event_sink=emit_current,
                cancel_event=cancel_event,
                dialect=dialect,
            )
            _emit(
                current_device_event_sink,
                "手册检索",
                "output",
                _json({"evidence_count": len(retrieval_cache[cache_key].get("evidence", []))}),
            )
        return retrieval_cache[cache_key]

    def plan_commands_once(
        graph_intent: dict[str, Any], graph_evidence: list[dict[str, Any]], device_scope: dict[str, Any]
    ) -> dict[str, Any]:
        def attach_post_plan_evidence(outcome: dict[str, Any]) -> dict[str, Any]:
            # Use generated CLI as a precise local catalogue key.  This makes
            # the initial retrieval packet small, while the compiler still
            # sees exact pages required by this device's final command plan.
            return {
                **outcome,
                "evidence": _recover_local_syntax_evidence_for_plan(
                    session,
                    manual_id=manual.id,
                    evidence=graph_evidence,
                    command_plan=dict(outcome.get("command_plan") or {}) or None,
                ),
            }

        # For a multi-VLAN task the LLM selects the shared handbook records;
        # the deterministic compiler applies each switch's different topology
        # role.  One selection is enough and avoids three identical large calls.
        if not (
            is_huawei_vlan_renderer(graph_intent, dialect)
            and graph_intent.get("feature") == "multi_vlan_intervlan"
        ):
            return attach_post_plan_evidence(
                _llm_command_plan_outcome(
                    session,
                    requirement=task.requirement_text,
                    intent=graph_intent,
                    evidence=graph_evidence,
                    topology_ports=list(device_scope.get("all_ports", [])),
                    device_scope=device_scope,
                    event_sink=emit_current,
                    cancel_event=cancel_event,
                    dialect=dialect,
                )
            )
        key = _json(
            {
                "intent": graph_intent,
                "evidence": [
                    {"command_id": item.get("command_id"), "name": item.get("canonical_name")}
                    for item in graph_evidence
                ],
            }
        )
        reused = key in command_plan_cache
        if not reused:
            command_plan_cache[key] = attach_post_plan_evidence(
                _llm_command_plan_outcome(
                    session,
                    requirement=task.requirement_text,
                    intent=graph_intent,
                    evidence=graph_evidence,
                    topology_ports=list(device_scope.get("all_ports", [])),
                    device_scope=device_scope,
                    event_sink=emit_current,
                    cancel_event=cancel_event,
                    dialect=dialect,
                )
            )
        cached = dict(command_plan_cache[key])
        if reused:
            cached["llm"] = {**dict(cached.get("llm", {})), "reused_for_task": True}
        return cached

    switches = [node for node in graph.get("nodes", []) if node.get("kind") == "switch"]
    if not switches:
        task.status = TaskStatus.blocked
        task.blocking_reason = "拓扑中没有交换机节点。"
    for node in switches:
        check_cancel(cancel_event.is_set if cancel_event else None)
        device_name = str(node.get("name") or node.get("label") or node["id"])
        current_device_event_sink = (
            (
                lambda stage, event_type, content, name=device_name: _emit(
                    event_sink, f"{name} · {stage}", event_type, content
                )
            )
            if event_sink
            else None
        )
        emit_current("设备规划", "stage", f"正在规划设备 {device_name}。")
        detected_model = node.get("detected_model") or node.get("model_name")
        detected_release = node.get("detected_release")
        status, reason, series = _manual_selection_context(manual, detected_model, detected_release)
        topology_ports, protected_ports = _switch_ports_from_topology(graph, str(node["id"]))
        access_ports = _pc_facing_ports_from_topology(graph, str(node["id"]))
        device_intent = _intent_for_device(baseline_intent, str(node["id"]))
        if (
            is_huawei_vlan_renderer(device_intent, dialect)
            and device_intent.get("feature") == "multi_vlan_intervlan"
        ):
            device_scope = _multi_vlan_device_scope(graph, str(node["id"]), device_intent, protected_ports)
            planning_ports = list(device_scope["all_ports"])
        elif (
            is_huawei_vlan_renderer(device_intent, dialect) and device_intent.get("feature") == "vlan_access"
        ):
            default_vlan = (device_intent.get("vlan_ids") or [None])[0]
            excluded_protected = [port for port in access_ports if port_identity(port) in protected_ports]
            access_ports = [port for port in access_ports if port_identity(port) not in protected_ports]
            device_scope = {
                "access_ports": [{"port": port, "vlan_id": default_vlan} for port in access_ports],
                "trunk_ports": [],
                "vlanifs": [],
                "all_ports": access_ports,
                "warnings": [
                    *[f"端口 {port} 被用户标为受保护端口，未自动生成配置。" for port in excluded_protected]
                ],
            }
            planning_ports = access_ports
        else:
            device_scope = _generic_device_scope(
                graph,
                str(node["id"]),
                protected_ports,
                device_intent,
            )
            planning_ports = list(device_scope["all_ports"])
        device_scope["current_device_context"] = _topology_device_context(
            dict(baseline_intent.get("topology_context") or {}),
            str(node["id"]),
        )
        if not topology_ports:
            device_scope["warnings"].append("交换机没有带端口名的拓扑连线，无法推导接口命令。")
        if any(port_identity(port) in protected_ports for port in planning_ports):
            device_scope["warnings"].append("存在用户标记的受保护端口，已从自动草案中排除。")
        graph_result = build_planning_graph(
            intent_refiner=refine_once,
            evidence_retriever=retrieve_once,
            command_planner=lambda graph_intent, graph_evidence: plan_commands_once(
                graph_intent, graph_evidence, device_scope
            ),
            command_renderer=lambda graph_intent, graph_evidence, command_plan: _render_relaxed_command_plan(
                graph_intent, graph_evidence, planning_ports, command_plan, device_scope, dialect
            ),
            command_reviewer=None,
        ).invoke(
            {
                "task_id": task.id,
                "device_id": str(node["id"]),
                "requirement": task.requirement_text,
                "intent": device_intent,
            }
        )
        initial_review = dict(dict(graph_result.get("command_review", {})).get("review", {}))
        review_feedback = dict(initial_review)
        initial_validation = dict(graph_result.get("validation", {}))
        initial_validation_errors = [str(item) for item in graph_result.get("validation_errors", [])]

        def apply_conservative_plan_prune(
            pruner: Callable[[LlmCommandPlan], tuple[LlmCommandPlan, list[str]]],
            *,
            reason: str,
        ) -> list[str]:
            """Recompile an exact-removal correction without inventing CLI.

            The generic planner always keeps a non-empty draft for the user.
            This path can only remove a demonstrably duplicate or independently
            rejected line from an existing model plan; if re-compilation loses
            every business command, the original visible draft is retained.
            """

            raw_plan = dict(graph_result.get("command_plan") or {})
            if not raw_plan:
                return []
            try:
                current_plan = LlmCommandPlan.model_validate(raw_plan)
            except ValueError:
                return []
            amended_plan, removed = pruner(current_plan)
            if not removed:
                return []
            amended_commands, amended_validation = _render_command_plan_or_fallback(
                dict(graph_result.get("intent", device_intent)),
                list(graph_result.get("evidence", [])),
                planning_ports,
                amended_plan.model_dump(mode="json"),
                device_scope,
                dialect,
            )
            if not amended_commands:
                return []
            graph_result.update(
                {
                    "command_plan": amended_plan.model_dump(mode="json"),
                    "candidate_commands": amended_commands,
                    "validation": amended_validation,
                    "validation_errors": list(amended_validation.get("errors", [])),
                }
            )
            _emit(event_sink, "命令纠偏", "output", f"{reason}：{', '.join(removed)}")
            return removed

        if not baseline_intent.get("relaxed_command_mode") and not is_huawei_vlan_renderer(
            device_intent, dialect
        ):
            removed_known_facts = apply_conservative_plan_prune(
                lambda current_plan: prune_command_plan_for_known_facts(
                    current_plan,
                    intent=dict(graph_result.get("intent", device_intent)),
                    dialect=dialect,
                ),
                reason="已保留需求声明的既有配置，仅移除重复写入",
            )
            if removed_known_facts:
                initial_validation = dict(graph_result.get("validation", {}))
                initial_validation_errors = [str(item) for item in graph_result.get("validation_errors", [])]

        removed_incomplete = (
            []
            if baseline_intent.get("relaxed_command_mode")
            else apply_conservative_plan_prune(
                lambda current_plan: prune_command_plan_for_incomplete_syntax(
                    current_plan,
                    evidence=list(graph_result.get("evidence", [])),
                    dialect=dialect,
                ),
                reason="已移除手册定义为缺少必填参数的裸命令",
            )
        )
        if removed_incomplete:
            initial_validation = dict(graph_result.get("validation", {}))
            initial_validation_errors = [str(item) for item in graph_result.get("validation_errors", [])]

        if (
            not baseline_intent.get("relaxed_command_mode")
            and initial_review.get("verdict") == "reject"
            and not is_huawei_vlan_renderer(device_intent, dialect)
        ):
            removed_reviewed = apply_conservative_plan_prune(
                lambda current_plan: prune_command_plan_for_review_feedback(
                    current_plan,
                    review=initial_review,
                    dialect=dialect,
                ),
                reason="已移除独立审阅明确点名的额外命令",
            )
            if removed_reviewed:
                # The prior verdict evaluated a different command sequence.
                # Preserve it for audit but do not send the unchanged rejection
                # into a needless LLM repair loop.
                graph_result["command_review"] = {
                    **dict(graph_result.get("command_review", {})),
                    "pre_prune_review": initial_review,
                    "review": {
                        "verdict": "amended_after_reject",
                        "issues": [],
                        "required_changes": [],
                        "reason_summary": "已仅删除独立审阅点名的额外命令；修订后的草案待用户审阅。",
                    },
                }
                initial_review = dict(graph_result["command_review"]["review"])
                initial_validation = dict(graph_result.get("validation", {}))
                initial_validation_errors = [str(item) for item in graph_result.get("validation_errors", [])]

        if (
            not baseline_intent.get("relaxed_command_mode")
            and review_feedback.get("verdict") == "reject"
            and not is_huawei_vlan_renderer(device_intent, dialect)
        ):

            def rebuild_required_actions(
                current_plan: LlmCommandPlan,
            ) -> tuple[LlmCommandPlan, list[str]]:
                # Explicit port facts are stronger than a weak plan's omission:
                # rebuild them from topology plus the matching handbook grammar
                # before adding only reviewer-quoted, evidence-bound CLI.
                explicit_plan = build_explicit_port_assignment_fallback_plan(
                    current_plan,
                    intent=dict(graph_result.get("intent", device_intent)),
                    evidence=list(graph_result.get("evidence", [])),
                    topology_ports=planning_ports,
                    dialect=dialect,
                )
                source_plan = explicit_plan or current_plan
                completed_plan, additions = complete_command_plan_from_review(
                    source_plan,
                    review=review_feedback,
                    evidence=list(graph_result.get("evidence", [])),
                    dialect=dialect,
                )
                explicit_plan_changed = bool(
                    explicit_plan
                    and explicit_plan.model_dump(mode="json") != current_plan.model_dump(mode="json")
                )
                if explicit_plan_changed:
                    return completed_plan, additions or ["按拓扑事实补全端口动作"]
                return completed_plan, additions

            completed_reviewed = apply_conservative_plan_prune(
                rebuild_required_actions,
                reason="已补全审阅明确缺失且有手册证据的配置动作",
            )
            if completed_reviewed:
                graph_result["command_review"] = {
                    **dict(graph_result.get("command_review", {})),
                    "pre_completion_review": review_feedback,
                    "review": {
                        "verdict": "amended_after_reject",
                        "issues": [],
                        "required_changes": [],
                        "reason_summary": "已基于审阅明确缺项、拓扑事实和手册语法补全草案；待用户审阅。",
                    },
                }
                initial_review = dict(graph_result["command_review"]["review"])
                initial_validation = dict(graph_result.get("validation", {}))
                initial_validation_errors = [str(item) for item in graph_result.get("validation_errors", [])]
        initial_unverified_draft = bool(initial_validation.get("unverified_draft"))
        review_rejected = initial_review.get("verdict") == "reject"
        repair_trigger = "llm_command_review_reject" if review_rejected else "static_compiler_reject"
        if initial_unverified_draft and not review_rejected and not initial_validation_errors:
            repair_trigger = "unverified_draft_repair"
        should_repair = (
            not baseline_intent.get("relaxed_command_mode")
            and not is_huawei_vlan_renderer(device_intent, dialect)
            and bool(graph_result.get("evidence"))
            and bool(graph_result.get("command_plan"))
            and (review_rejected or bool(initial_validation_errors) or initial_unverified_draft)
        )
        if should_repair:
            # A semantic review or deterministic handbook check can both give
            # the planner precise, bounded feedback.  The operator still
            # receives and approves the final draft explicitly.
            feedback = {
                "issues": (
                    list(initial_review.get("issues", [])) if review_rejected else initial_validation_errors
                ),
                "required_changes": (
                    list(initial_review.get("required_changes", []))
                    if review_rejected
                    else (
                        initial_validation_errors
                        or list(initial_validation.get("errors", []))
                        or list(initial_validation.get("warnings", []))[:4]
                    )
                ),
            }
            _emit(event_sink, "命令修订", "stage", "发现手册或审阅反馈，LLM 正在重建完整命令草案。")
            repair_intent = {
                **dict(graph_result.get("intent", device_intent)),
                "command_repair_feedback": feedback,
            }
            evidence = list(graph_result.get("evidence", []))
            repair_outcome = _llm_command_plan_outcome(
                session,
                requirement=task.requirement_text,
                intent=repair_intent,
                evidence=evidence,
                topology_ports=planning_ports,
                device_scope=device_scope,
                event_sink=event_sink,
                cancel_event=cancel_event,
                dialect=dialect,
            )
            evidence = _recover_local_syntax_evidence_for_plan(
                session,
                manual_id=manual.id,
                evidence=evidence,
                command_plan=dict(repair_outcome.get("command_plan") or {}) or None,
            )
            repaired_commands, repaired_validation = _render_command_plan_or_fallback(
                repair_intent,
                evidence,
                planning_ports,
                dict(repair_outcome.get("command_plan") or {}) or None,
                device_scope,
                dialect,
            )
            if repaired_commands and repaired_validation.get("status") == "ready":
                repair_state = {
                    "intent": repair_intent,
                    "command_plan": dict(repair_outcome.get("command_plan") or {}),
                    "candidate_commands": repaired_commands,
                    "validation": repaired_validation,
                    "evidence": evidence,
                }
                repaired_review = _llm_command_review_outcome(
                    session,
                    repair_state,
                    device_scope,
                    event_sink=event_sink,
                    cancel_event=cancel_event,
                )
                repaired_validation["repair"] = {
                    "attempted": True,
                    "trigger": repair_trigger,
                    "initial_review": initial_review if review_rejected else {},
                    "initial_validation_errors": initial_validation_errors,
                    "final_review": dict(repaired_review.get("review", {})),
                }
                graph_result.update(
                    {
                        "intent": repair_intent,
                        "command_plan": repair_state["command_plan"],
                        "command_plan_llm": dict(repair_outcome.get("llm", {})),
                        "candidate_commands": repaired_commands,
                        "validation": repaired_validation,
                        "command_review": repaired_review,
                        "evidence": evidence,
                        "validation_errors": [],
                    }
                )
                _emit(event_sink, "命令修订", "output", "已生成审阅反馈后的完整命令草案。")
            else:
                _emit(event_sink, "命令修订", "output", "自动修订未通过静态校验，保留首次草案供人工审阅。")
        intent = dict(graph_result.get("intent", device_intent))
        llm_status = dict(graph_result.get("llm", {"status": "not_run"}))
        intent["llm"] = llm_status
        intent["retrieval_audit"] = dict(graph_result.get("retrieval_audit", {}))
        intent["llm_command_plan"] = dict(graph_result.get("command_plan_llm", {"status": "not_run"}))
        intent["llm_command_review"] = dict(graph_result.get("command_review", {"status": "disabled"}))
        if is_huawei_vlan_renderer(intent, dialect) and intent.get("feature") == "multi_vlan_intervlan":
            # The configured LLM may inspect a single-device slice and wrongly
            # complain about commands belonging to sibling switches.  Keep its
            # verdict as an advisory record, but make the device-scope audit
            # explicit for the operator.
            review = dict(intent["llm_command_review"])
            review.setdefault("scope_note", "仅审阅当前设备；跨设备缺项不作为本设备命令错误")
            intent["llm_command_review"] = review
        evidence = list(graph_result.get("evidence", []))
        commands = list(graph_result.get("candidate_commands", []))
        validation = dict(graph_result.get("validation", {}))
        planning_warnings = list(device_scope.get("warnings", []))
        planning_warnings.extend(str(item) for item in graph_result.get("validation_errors", []))
        if planning_warnings:
            validation["warnings"] = [*validation.get("warnings", []), *planning_warnings]
        if not commands and validation.get("status") == "blocked":
            validation["status"] = "draft_with_warnings"
            validation["warnings"] = [
                *validation.get("warnings", []),
                *validation.pop("errors", []),
            ]
            validation["errors"] = []
        intent["topology_scope"] = {
            "all_linked_ports": topology_ports,
            "device_scope": device_scope,
            "rule": (
                "PC 链路按显式 PC→VLAN 映射生成 Access；交换机链路生成 Trunk；VLANIF 仅在三层核心生成。"
                if (
                    is_huawei_vlan_renderer(intent, dialect)
                    and intent.get("feature") == "multi_vlan_intervlan"
                )
                else (
                    "仅配置交换机直连 PC 的端口；上联、云和交换机互联端口不纳入 VLAN Access。"
                    if is_huawei_vlan_renderer(intent, dialect) and intent.get("feature") == "vlan_access"
                    else (
                        "通用计划可以使用当前设备已连线且未受保护的物理端口；"
                        "虚拟接口必须由手册证据和用户已确认的思路共同支持。"
                    )
                )
            ),
        }
        rollback = _rollback_draft(intent, planning_ports)
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
        # Do not retain an uncommitted DevicePlan while the next switch invokes
        # retrieval or the LLM; SQLite allows one writer only.
        session.commit()
        emit_current(
            "设备规划",
            "done",
            f"{plan.display_name} 的命令草案已生成，共 {len(commands)} 行，等待用户审阅。",
        )
    if llm_outcome:
        task_intent = dict(llm_outcome["intent"])
        task_intent["llm"] = dict(llm_outcome.get("llm", {}))
        task.intent_json = _json(task_intent)
    if switches:
        # A task is always reviewable.  Evidence and inference issues are shown
        # next to the generated draft instead of becoming an opaque safety gate.
        task.status = TaskStatus.needs_review
        if any(not _load(plan.commands_json) for plan in plans):
            task.blocking_reason = "部分设备没有足够的拓扑或手册信息生成命令，请查看各设备的规划提示。"
    session.commit()
    session.refresh(task)
    _emit(event_sink, "完成", "done", "逐设备命令草案已生成，等待用户审阅。")
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
    if not _load(plan.commands_json):
        raise ValueError("当前设备没有可审批的配置命令。")
    plan.approved_at = datetime.utcnow()
    task = plan.task
    if all(item.approved_at is not None and bool(_load(item.commands_json)) for item in task.device_plans):
        task.status = TaskStatus.approved
    session.commit()
    session.refresh(plan)
    return plan
