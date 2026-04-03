#!/usr/bin/env python3
"""
enrich_cli_graph.py — 为 CLI keyword graph 的 module 节点自动推导技术特征

从现有 keyword / edge / command 数据统计推导，不需人工标注。
新增字段: protocol_stack, interface_types, address_family, layer, related_modules, feature_tags

Usage:
    python INAGENT/scripts/enrich_cli_graph.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger(__name__)

GRAPH_PATH = ROOT / "knowledge_base" / "cli_keyword_graph.json"

# ── 协议关键词映射（keyword → 标准协议名）─────────────────────────
# 用于从 command keywords 中推导 protocol_stack
PROTOCOL_KEYWORD_MAP: Dict[str, str] = {
    # L7
    "http": "HTTP",
    "https": "HTTPS",
    "http2": "HTTP2",
    "ftp": "FTP",
    "sip": "SIP",
    "dns": "DNS",
    "smtp": "SMTP",
    "snmp": "SNMP",
    "ssl": "SSL",
    "tls": "SSL",
    "radius": "RADIUS",
    "ldap": "LDAP",
    "rtsp": "RTSP",
    "mqtt": "MQTT",
    # L4
    "tcp": "TCP",
    "udp": "UDP",
    "sctp": "SCTP",
    # L3
    "ip": "IP",
    "ipv4": "IPV4",
    "ipv6": "IPV6",
    "ip6": "IPV6",
    "icmp": "ICMP",
    "ospf": "OSPF",
    "bgp": "BGP",
    "rip": "RIP",
    "vrrp": "VRRP",
    "isis": "ISIS",
    # L2
    "arp": "ARP",
    "vlan": "VLAN",
    "lacp": "LACP",
    "stp": "STP",
    "lldp": "LLDP",
}

# 协议→OSI层级
PROTOCOL_LAYER: Dict[str, str] = {
    "HTTP": "L7", "HTTPS": "L7", "HTTP2": "L7", "FTP": "L7",
    "SIP": "L7", "DNS": "L7", "SMTP": "L7", "SNMP": "L7",
    "SSL": "L7", "RADIUS": "L7", "LDAP": "L7",
    "RTSP": "L7", "MQTT": "L7",
    "TCP": "L4", "UDP": "L4", "SCTP": "L4",
    "IP": "L3", "IPV4": "L3", "IPV6": "L3", "ICMP": "L3",
    "OSPF": "L3", "BGP": "L3", "RIP": "L3", "VRRP": "L3", "ISIS": "L3",
    "ARP": "L2", "VLAN": "L2", "LACP": "L2", "STP": "L2", "LLDP": "L2",
}

# 地址族关键词
ADDRESS_FAMILY_KEYWORDS: Dict[str, str] = {
    "ipv4": "IPv4", "ipv6": "IPv6", "ip6": "IPv6",
    "ip": "IPv4",  # 默认 ip 关键词映射到 IPv4
    "v4": "IPv4", "v6": "IPv6",
}

# 功能标签关键词（从 command label/keyword 中提取）
FEATURE_TAG_KEYWORDS: Dict[str, str] = {
    "persistence": "persistence", "cookie": "cookie",
    "encryption": "encryption", "encrypt": "encryption",
    "aes": "encryption", "des": "encryption",
    "health": "health-check", "healthcheck": "health-check",
    "load": "load-balancing", "balance": "load-balancing",
    "failover": "high-availability", "ha": "high-availability",
    "redundancy": "high-availability", "backup": "high-availability",
    "acl": "access-control", "firewall": "access-control",
    "nat": "nat", "snat": "nat", "dnat": "nat",
    "qos": "qos", "rate": "rate-limiting",
    "cache": "caching", "proxy": "proxy",
    "log": "logging", "audit": "logging",
    "session": "session-management",
    "timeout": "timeout", "keepalive": "keepalive",
    "ssl": "ssl-offload", "certificate": "ssl-offload",
}

# WebUI 相关模块名（用于判断 interface_types 中是否有 WebUI）
WEBUI_MODULE_IDS = {"webagent", "webui", "web"}


def load_graph(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_graph(graph: Dict[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _collect_module_keywords(
    graph: Dict[str, Any],
) -> Dict[str, Counter]:
    """Collect all keywords for each module from its commands."""
    # Build module → command list
    module_cmds: Dict[str, List[str]] = defaultdict(list)
    for edge in graph["edges"]:
        if edge.get("type") == "contains":
            module_cmds[edge["source"]].append(edge["target"])

    # For each module, count keywords from all its commands
    node_map = {n["id"]: n for n in graph["nodes"]}
    module_kw_counts: Dict[str, Counter] = {}
    for mod_id, cmd_ids in module_cmds.items():
        kw_counter: Counter = Counter()
        for cmd_id in cmd_ids:
            cmd_node = node_map.get(cmd_id, {})
            for kw in cmd_node.get("keywords", []):
                kw_counter[kw.lower()] += 1
            # Also count words from command label
            label = cmd_node.get("label", "")
            for word in label.lower().split():
                kw_counter[word] += 1
        module_kw_counts[mod_id] = kw_counter
    return module_kw_counts


def _collect_module_neighbors(
    graph: Dict[str, Any],
) -> Dict[str, Set[str]]:
    """Collect module-level neighbors from shares_keyword edges.

    shares_keyword edges connect command/operation_command nodes, not module
    nodes directly.  We resolve each endpoint to its owning module via the
    ``contains`` edges and then build module↔module adjacency.
    """
    # Build command_id → owning module_id mapping from 'contains' edges
    cmd_to_module: Dict[str, str] = {}
    for edge in graph["edges"]:
        if edge.get("type") == "contains":
            # source=module, target=command
            cmd_to_module[edge["target"]] = edge["source"]

    module_ids = {n["id"] for n in graph["nodes"] if n.get("type") == "module"}
    neighbors: Dict[str, Set[str]] = defaultdict(set)

    for edge in graph["edges"]:
        if edge.get("type") != "shares_keyword":
            continue
        src_mod = cmd_to_module.get(edge["source"])
        tgt_mod = cmd_to_module.get(edge["target"])
        if src_mod and tgt_mod and src_mod != tgt_mod:
            if src_mod in module_ids and tgt_mod in module_ids:
                neighbors[src_mod].add(tgt_mod)
                neighbors[tgt_mod].add(src_mod)

    return neighbors


def _infer_protocol_stack(
    kw_counts: Counter,
) -> List[str]:
    """Infer protocol_stack from keyword frequency."""
    protocols: Set[str] = set()
    for kw, _count in kw_counts.items():
        proto = PROTOCOL_KEYWORD_MAP.get(kw)
        if proto:
            protocols.add(proto)
    # Sort by OSI layer (L7 first)
    layer_order = {"L7": 0, "L4": 1, "L3": 2, "L2": 3}
    return sorted(
        protocols,
        key=lambda p: (layer_order.get(PROTOCOL_LAYER.get(p, "L7"), 9), p),
    )


def _infer_address_family(
    kw_counts: Counter,
) -> List[str]:
    """Infer address_family from keywords."""
    families: Set[str] = set()
    for kw in kw_counts:
        af = ADDRESS_FAMILY_KEYWORDS.get(kw)
        if af:
            families.add(af)
    return sorted(families)


def _infer_layer(protocols: List[str]) -> str:
    """Infer OSI layer from protocol_stack."""
    layers: Set[str] = set()
    for p in protocols:
        layer = PROTOCOL_LAYER.get(p)
        if layer:
            layers.add(layer)
    if not layers:
        return ""
    return "/".join(sorted(layers, key=lambda l: int(l[1:])))


def _infer_interface_types(
    module_id: str,
    neighbors: Set[str],
) -> List[str]:
    """Infer interface types. All modules have CLI. Check WebUI association."""
    types = ["CLI"]
    # If this module IS a webui module or has a webui-related neighbor
    if module_id in WEBUI_MODULE_IDS:
        types.append("WebUI")
    elif neighbors & WEBUI_MODULE_IDS:
        types.append("WebUI")
    return types


def _infer_feature_tags(
    kw_counts: Counter,
) -> List[str]:
    """Infer feature tags from keyword frequency."""
    tags: Set[str] = set()
    for kw in kw_counts:
        tag = FEATURE_TAG_KEYWORDS.get(kw)
        if tag:
            tags.add(tag)
    return sorted(tags)


def enrich_graph(graph: Dict[str, Any]) -> Dict[str, int]:
    """Enrich module nodes with tech features. Returns stats."""
    module_kw_counts = _collect_module_keywords(graph)
    module_neighbors = _collect_module_neighbors(graph)

    node_map = {n["id"]: n for n in graph["nodes"]}
    # Module-only map (avoids command nodes with same id overriding module labels)
    module_map = {n["id"]: n for n in graph["nodes"] if n.get("type") == "module"}
    stats = {"enriched": 0, "total_modules": 0}

    for node in graph["nodes"]:
        if node.get("type") != "module":
            continue
        stats["total_modules"] += 1
        mod_id = node["id"]
        kw_counts = module_kw_counts.get(mod_id, Counter())
        neighbors = module_neighbors.get(mod_id, set())

        # Infer fields
        protocol_stack = _infer_protocol_stack(kw_counts)
        address_family = _infer_address_family(kw_counts)
        layer = _infer_layer(protocol_stack)
        interface_types = _infer_interface_types(mod_id, neighbors)
        related_modules = sorted(
            {module_map[n]["label"] for n in neighbors if n in module_map},
        )
        feature_tags = _infer_feature_tags(kw_counts)

        # Write back
        node["protocol_stack"] = protocol_stack
        node["address_family"] = address_family
        node["layer"] = layer
        node["interface_types"] = interface_types
        node["related_modules"] = related_modules
        node["feature_tags"] = feature_tags
        stats["enriched"] += 1

    return stats


def print_summary(graph: Dict[str, Any]) -> None:
    """Print enriched module summary."""
    for node in sorted(graph["nodes"], key=lambda n: n.get("label", "")):
        if node.get("type") != "module":
            continue
        label = node["label"]
        proto = ", ".join(node.get("protocol_stack", [])) or "—"
        af = ", ".join(node.get("address_family", [])) or "—"
        layer = node.get("layer", "—") or "—"
        iface = ", ".join(node.get("interface_types", [])) or "—"
        related = ", ".join(node.get("related_modules", [])) or "—"
        tags = ", ".join(node.get("feature_tags", [])) or "—"
        print(
            f"{label:20s} | 协议: {proto:30s} | 地址族: {af:12s} | "
            f"层级: {layer:6s} | 接口: {iface:10s} | "
            f"关联: {related:40s} | 标签: {tags}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich CLI keyword graph with tech features")
    parser.add_argument("--dry-run", action="store_true", help="Print results without saving")
    parser.add_argument("--graph", type=Path, default=GRAPH_PATH, help="Path to graph JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    logger.info("Loading graph from %s", args.graph)
    graph = load_graph(args.graph)
    logger.info("Nodes: %d, Edges: %d", len(graph["nodes"]), len(graph["edges"]))

    stats = enrich_graph(graph)
    logger.info("Enriched %d / %d modules", stats["enriched"], stats["total_modules"])

    print("\n" + "=" * 140)
    print_summary(graph)
    print("=" * 140)

    if args.dry_run:
        logger.info("[DRY-RUN] Not saving changes.")
    else:
        save_graph(graph, args.graph)
        logger.info("Saved enriched graph to %s", args.graph)


if __name__ == "__main__":
    main()
