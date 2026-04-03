"""enrich_cli_leaf_schema.py — Phase A: CLI 知识树树苗建设

从 XML 命令树（arg 语义层）和现有 cli_keyword_graph.json（description 文本层）
双源合并，丰富每个 command 节点的 schema，添加：
  - parameters    : 带 optional/type/source 的参数列表
  - full_syntax   : 完整语法字符串（不截断）
  - func          : XML C 函数名（稳定标识符）
  - help_string   : XML 简洁说明
  - scope         : ["global"] 或 ["global", "group"]
  - operations    : {config/no/show/clear → node_id}
  - knowledge_faces: {syntax, constraint, ui, experience} 空槽
  - keywords      : 追加参数名和枚举值

用法:
  python -m INAGENT.scripts.enrich_cli_leaf_schema [--dry-run] [--output PATH]
  python INAGENT/scripts/enrich_cli_leaf_schema.py --dry-run
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_GRAPH = _ROOT / "knowledge_base" / "cli_keyword_graph.json"
_DEFAULT_XML = (
    _ROOT / "knowledge_base" / "input" / "command_tree-Beta_APV_10_5_0_73.xml"
)
_FLUSH_BATCH = 500

# ── Operation prefixes ──────────────────────────────────────────

_OP_PREFIXES = {
    "no_": "no",
    "show_": "show",
    "clear_": "clear",
    "display_": "show",
}


def _strip_op_prefix(node_id: str) -> Tuple[str, Optional[str]]:
    """(base_id, op_key) — base_id has prefix stripped, op_key is the verb."""
    for prefix, op_key in _OP_PREFIXES.items():
        if node_id.startswith(prefix):
            return node_id[len(prefix):], op_key
    return node_id, None


# ── XML extraction ───────────────────────────────────────────────

def _build_cmd_path(path_parts: List[str]) -> str:
    """Convert a list of XML element names to a CLI command path string."""
    return " ".join(path_parts)


def _path_to_id(path_parts: List[str]) -> str:
    return "_".join(p.lower().replace("-", "_") for p in path_parts)


_INT_TYPES_SET = {"U32", "U16", "INTEGER", "INT"}

# Range patterns in help_string:
#   <1-255>  (HTML-unescaped angle brackets)
#   (1 to 65535) / (1-255) / (10 - 4096, default ...) / (1025 - 65000, def...)
#   bare: "1 to 65535" / "1-3 only"
_RANGE_RE = re.compile(
    r"<\s*(\d+)\s*-\s*(\d+)\s*>"             # <min-max>
    r"|"
    r"[\(\[]\s*(\d+)\s*(?:to|-)\s*(\d+)"     # (min to max) or (min-max) or [min-max]
    r"|"
    r"\b(\d+)\s*(?:to|-)\s*(\d+)\b",         # bare min to max / min-max
    re.IGNORECASE,
)


def _extract_int_range(help_str: str) -> str:
    """Extract numeric range from an integer parameter's help_string.

    Returns a normalized '[min - max]' string, or '' if no range found.
    """
    m = _RANGE_RE.search(help_str)
    if not m:
        return ""
    # groups: (1,2) → <min-max>, (3,4) → bracket form, (5,6) → bare form
    g = m.groups()
    if g[0] is not None:
        lo, hi = g[0], g[1]
    elif g[2] is not None:
        lo, hi = g[2], g[3]
    else:
        lo, hi = g[4], g[5]
    # Sanity: min <= max and neither is unreasonably large pattern artifact
    try:
        if int(lo) > int(hi):
            return ""
    except ValueError:
        return ""
    return f"[{lo} - {hi}]"


def extract_args_from_xml(xml_path: Path) -> Dict[str, Dict[str, Any]]:
    """Parse XML and return per-command metadata including args.

    Returns:
        dict mapping normalized_cmd_id →
            {
                "func": str,
                "help_string": str,
                "cmd_path": str,   # e.g. "slb mode ircookie"
                "args": [{"name": str, "type": str, "optional": bool,
                           "default_value": str, "help_string": str,
                           "length": str, "limit": str}]
            }
        Multiple items with the same base name (no/show/clear variants) share
        the same cmd_path → they are stored as separate entries keyed by
        (scope_prefix + cmd_id) or by func name.
    """
    result: Dict[str, Dict[str, Any]] = {}

    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError as exc:
        logger.error("XML parse error: %s", exc)
        return result

    root = tree.getroot()

    def _traverse(element: ET.Element, path_parts: List[str]) -> None:
        tag = element.tag

        if tag == "menu":
            name = element.attrib.get("name", "").strip()
            help_str = element.attrib.get("help_string", "")
            new_path = path_parts + [name] if name else path_parts
            for child in element:
                _traverse(child, new_path)

        elif tag == "item":
            name = element.attrib.get("name", "").strip()
            if not name:
                return
            func = element.attrib.get("func", "")
            help_str = element.attrib.get("help_string", "")
            full_path = path_parts + [name]
            cmd_path = _build_cmd_path(full_path)
            cmd_id = _path_to_id(full_path)

            args: List[Dict[str, Any]] = []
            arguments_node = element.find("arguments")
            if arguments_node is not None:
                for arg in arguments_node:
                    if arg.tag == "arg":
                        raw_help = arg.attrib.get("help_string", "")
                        raw_dv = arg.attrib.get("default_value", "")
                        raw_limit = arg.attrib.get("limit", "").strip()
                        arg_type = arg.attrib.get("type", "STRING").upper()
                        decoded_help = html.unescape(raw_help) if raw_help else ""
                        # For integer types, extract range from help_string
                        # when the limit field is absent.
                        if not raw_limit and arg_type in _INT_TYPES_SET and decoded_help:
                            raw_limit = _extract_int_range(decoded_help)
                        args.append({
                            "name": arg.attrib.get("name", "").strip(),
                            "type": arg_type,
                            "optional": arg.attrib.get("optional", "NO").upper() == "YES",
                            "default_value": html.unescape(raw_dv) if raw_dv else "",
                            "help_string": decoded_help,
                            "limit": raw_limit,
                            "length": arg.attrib.get("length", ""),
                        })

            entry = {
                "func": func,
                "help_string": help_str,
                "cmd_path": cmd_path,
                "args": args,
            }

            # Key by cmd_id; if duplicate (same cmd_id different scope),
            # prefer the one with more args or with a func value.
            if cmd_id not in result or (
                len(args) > len(result[cmd_id]["args"])
                or (not result[cmd_id]["func"] and func)
            ):
                result[cmd_id] = entry

        elif tag in ("scope", "commands"):
            for child in element:
                _traverse(child, path_parts)

    _traverse(root, [])
    logger.info("XML: extracted %d item entries", len(result))
    return result


# ── Description parameter parser ────────────────────────────────

_LATEX_CLEANUP_RE = re.compile(r"\$\\mid\s*>?\s*\$?|\$\\mid")  # $\mid >$ or $\mid


def _normalize_description(desc: str) -> str:
    """Normalize CLI syntax strings before parsing.
    - Replace LaTeX pipe escapes ($\mid) with actual |
    - Collapse multiple spaces
    """
    desc = re.sub(r"\$\\mid\s*>?\s*\$?", "|", desc)  # $\mid >$ -> |
    desc = re.sub(r"\s+", " ", desc)
    return desc.strip()


_PARAM_RE = re.compile(
    r"(?:"
    r"<(?P<req>[^>/]+)>"              # <required_param>
    r"|"
    r"\[(?P<opt_enum>[^\]|]+(?:\|[^\]|]+)+)\]"  # [x|y|z] — optional enum (has |)
    r"|"
    r"\[(?P<opt>[^\]|]+)\]"          # [optional_param] — no |
    r"|"
    r"\{(?P<enum>[^}]+)\}"           # {a|b|c} — required enum
    r")"
)


def parse_parameters_from_description(description: str) -> List[Dict[str, Any]]:
    """Extract parameter info from CLI syntax description string.

    Only the portion after the command name (tokens that start with < [ {)
    is considered. Returns list of param dicts with keys:
      name, required, type, values (for enum), source="description"

    Handles:
      <param>      → required string
      [param]      → optional string
      {x|y|…}     → required enum
      [x|y|…]     → optional enum
    """
    params: List[Dict[str, Any]] = []
    seen_names: set = set()

    description = _normalize_description(description)

    for m in _PARAM_RE.finditer(description):
        req      = m.group("req")
        opt      = m.group("opt")
        opt_enum = m.group("opt_enum")
        enum     = m.group("enum")

        if req:
            name = req.strip().lower().replace(" ", "_").replace("-", "_")
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            params.append({
                "name": name,
                "required": True,
                "type": "string",
                "source": "description",
            })
        elif opt:
            name = opt.strip().lower().replace(" ", "_").replace("-", "_")
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            params.append({
                "name": name,
                "required": False,
                "type": "string",
                "source": "description",
            })
        elif opt_enum:
            values = [v.strip() for v in opt_enum.split("|") if v.strip()]
            if not values:
                continue
            name = "|".join(values)  # keep raw form as name for readability
            if name in seen_names:
                continue
            seen_names.add(name)
            params.append({
                "name": name,
                "required": False,
                "type": "enum",
                "values": values,
                "source": "description",
            })
        elif enum:
            values = [v.strip() for v in enum.split("|") if v.strip()]
            if not values:
                continue
            name = "|".join(values)  # keep raw form as name for readability
            if name in seen_names:
                continue
            seen_names.add(name)
            params.append({
                "name": name,
                "required": True,
                "type": "enum",
                "values": values,
                "source": "description",
            })

    return params


def _xml_type_to_schema_type(xml_type: str) -> str:
    mapping = {
        "STRING": "string",
        "XSTRING": "string",
        "INTEGER": "integer",
        "INT": "integer",
        "U32": "integer",
        "U16": "integer",
        "IPV4": "ipv4",
        "IPV6": "ipv6",
        "IP": "ipv4",
        "IPADDR": "ipv4",
        "DOTTEDIP": "ipv4",
        "IPMASK": "ipmask",
        "ENUM": "enum",
        "FLAG": "flag",
        "BOOL": "boolean",
    }
    return mapping.get(xml_type.upper(), "string")


def _infer_name_from_help(help_str: str) -> str:
    """Derive a parameter identifier from a help_string when name field is empty.

    Strategy: take the first clause (before comma/period/parenthesis),
    extract up to 4 meaningful words, lowercase and join with underscore.
    e.g. 'Host name' -> 'host_name', 'LDAP server name' -> 'ldap_server_name'
    """
    if not help_str:
        return ""
    _STOP = {
        "a", "an", "the", "to", "for", "of", "or", "and", "is", "be",
        "in", "on", "at", "with", "by", "from", "as", "it", "its",
        "this", "that", "are", "was", "were", "will", "whether",
    }
    text = re.sub(r"\([^)]*\)", "", help_str)   # strip (...) annotations
    text = re.split(r"[,.(]", text)[0].strip()  # take first clause
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]*", text.lower())
    words = [w for w in words if w not in _STOP]
    name = "_".join(words[:4])
    return name or ""


def _infer_enum_values_from_help(help_str: str) -> Optional[List[str]]:
    """Extract enum values from XML help_string like 'mode: val1|val2|val3'.

    Rejects candidates that contain angle brackets (type descriptions) or
    produce individual tokens that are too long (prose fragments).
    """
    if "|" not in help_str:
        return None
    # Find the last colon-separated segment with pipes
    parts = help_str.split(":")
    for part in reversed(parts):
        # Reject if angle brackets present — these are type placeholders, not enum values
        if "<" in part or ">" in part:
            continue
        raw_vals = [v.strip() for v in part.split("|") if v.strip()]
        if len(raw_vals) < 2:
            continue
        # Strip parenthetical annotations like (default), (optional), (e.g. ...)
        cleaned = []
        for v in raw_vals:
            v = re.sub(r"\([^)]*\)", "", v)   # remove (...) annotations
            v = re.sub(r"[^a-zA-Z0-9_\-]", "", v)  # keep identifier chars only
            v = v.strip()
            if v:
                cleaned.append(v)
        # Reject if any value is longer than 20 chars (likely a description fragment)
        if any(len(v) > 20 for v in cleaned):
            continue
        if len(cleaned) >= 2:
            return cleaned
    return None


# ── Parameter merge ──────────────────────────────────────────────

def merge_parameters(
    xml_args: List[Dict[str, Any]],
    desc_params: List[Dict[str, Any]],
    xml_help_string: str = "",
) -> List[Dict[str, Any]]:
    """Merge XML args with description-parsed params.

    Rules:
    - Count source of truth: description (possibly newer than XML)
    - Semantics source of truth: XML (has type/optional/default)
    - For params present in XML: use XML metadata, mark source="xml"
    - For params only in description (XML not updated): source="description"
    - XML args in excess of description params: included with source="xml"
    """
    merged: List[Dict[str, Any]] = []

    # Enrich XML args with enum values from help_string
    enriched_xml: List[Dict[str, Any]] = []
    for arg in xml_args:
        a = dict(arg)
        if a.get("type") == "STRING" and a.get("help_string"):
            enum_vals = _infer_enum_values_from_help(a["help_string"])
            if enum_vals:
                a["type"] = "ENUM"
                a["values"] = enum_vals
        enriched_xml.append(a)

    # Match XML args to description params by position
    xml_count = len(enriched_xml)
    desc_count = len(desc_params)
    max_count = max(xml_count, desc_count)

    for i in range(max_count):
        xml_arg = enriched_xml[i] if i < xml_count else None
        desc_p = desc_params[i] if i < desc_count else None

        if xml_arg and desc_p:
            # Both present: XML wins for semantics, description for name if XML has none
            xml_name = xml_arg.get("name") or ""
            desc_name = desc_p["name"]
            # desc_name may be "enable|disable" (raw enum token) — normalise it
            if "|" in desc_name:
                desc_name = _infer_name_from_help(xml_arg.get("help_string", "")) or \
                            "_".join(v[:6] for v in desc_name.split("|")[:2]) + "_mode"
            inferred = desc_name if not xml_name else xml_name
            param: Dict[str, Any] = {
                "name": inferred or _infer_name_from_help(xml_arg.get("help_string", "")),

                "required": not xml_arg.get("optional", False),
                "type": _xml_type_to_schema_type(xml_arg.get("type", "STRING")),
                "source": "xml",
            }
            if xml_arg.get("values"):
                param["values"] = xml_arg["values"]
            elif desc_p.get("values"):
                # Only use description enum values when XML type is STRING —
                # specific types (ipmask, ipv4, integer) take precedence over
                # description format hints like {netmask|prefix}.
                if xml_arg.get("type", "STRING").upper() in ("STRING", "XSTRING", "ENUM"):
                    param["values"] = desc_p["values"]
            if "values" in param and param["type"] != "enum":
                param["type"] = "enum"
            if xml_arg.get("help_string"):
                param["description"] = xml_arg["help_string"]
            if xml_arg.get("default_value"):
                param["default"] = xml_arg["default_value"]
            if xml_arg.get("limit"):
                param["constraint"] = xml_arg["limit"]
            merged.append(param)

        elif xml_arg and not desc_p:
            # XML-only: name from XML field, then help_string inference, then placeholder
            xml_name = xml_arg.get("name") or ""
            param = {
                "name": xml_name or _infer_name_from_help(xml_arg.get("help_string", "")) or f"param{i+1}",
                "required": not xml_arg.get("optional", False),
                "type": _xml_type_to_schema_type(xml_arg.get("type", "STRING")),
                "source": "xml",
            }
            if xml_arg.get("values"):
                param["values"] = xml_arg["values"]
            if "values" in param and param["type"] != "enum":
                param["type"] = "enum"
            if xml_arg.get("help_string"):
                param["description"] = xml_arg["help_string"]
            if xml_arg.get("default_value"):
                param["default"] = xml_arg["default_value"]
            if xml_arg.get("limit"):
                param["constraint"] = xml_arg["limit"]
            merged.append(param)

        elif desc_p and not xml_arg:
            # Description-only: XML not updated
            desc_name = desc_p["name"]
            if "|" in desc_name:
                desc_name = "_".join(v[:6] for v in desc_name.split("|")[:2]) + "_mode"
            param = {
                "name": desc_name,
                "required": desc_p.get("required", False),
                "type": desc_p.get("type", "string"),
                "source": "description",
            }
            if desc_p.get("values"):
                param["values"] = desc_p["values"]
            merged.append(param)

    return merged


# ── Operations grouping ──────────────────────────────────────────

def build_operations_groups(nodes: List[dict]) -> Dict[str, Dict[str, str]]:
    """Group set/no/show/clear variants of the same command.

    Returns:
        dict mapping base_cmd_id → {
            "set": base_cmd_id,
            "no": ...,       # if exists
            "show": ...,     # if exists
            "clear": ...,    # if exists
        }
    """
    # First pass: collect all command-type node IDs
    all_cmd_ids: set = set()
    for node in nodes:
        if node.get("type") in ("command", "operation_command"):
            all_cmd_ids.add(node["id"])

    groups: Dict[str, Dict[str, str]] = {}

    for nid in all_cmd_ids:
        base_id, op_key = _strip_op_prefix(nid)
        if op_key is None:
            # This is a set command — ensure base entry exists
            if nid not in groups:
                groups[nid] = {"set": nid}
            else:
                groups[nid]["set"] = nid
        else:
            # Operation variant — add to base command's group
            if base_id not in groups:
                groups[base_id] = {}
            groups[base_id][op_key] = nid

    # Remove groups that have no "set" key (base cmd not found — orphan op)
    result = {k: v for k, v in groups.items() if "set" in v}
    return result


# ── scope inference ──────────────────────────────────────────────

def infer_scope(parameters: List[Dict[str, Any]]) -> List[str]:
    """Infer applicable scope from parameter names.

    Only parameters whose name is exactly 'group' or starts with 'group_'
    are treated as CLI group-scope indicators.  Names like 'ip_group_name'
    refer to object names, not CLI access level, and are excluded.
    """
    def _is_group_param(name: str) -> bool:
        n = name.lower()
        return n == "group" or n.startswith("group_")

    for p in parameters:
        if not p.get("required", True) and _is_group_param(p.get("name", "")):
            return ["global", "group"]
    for p in parameters:
        if p.get("required") and _is_group_param(p.get("name", "")):
            return ["group"]
    return ["global"]


# ── Keywords enrichment ──────────────────────────────────────────

def enrich_keywords(existing: List[str], params: List[Dict[str, Any]]) -> List[str]:
    """Extend keywords list with parameter names and enum values."""
    extra: List[str] = []
    for p in params:
        name = p.get("name", "")
        if name and name not in existing:
            extra.append(name)
        for v in p.get("values", []):
            if v and v not in existing and v not in extra:
                extra.append(v)
    return existing + extra


# ── Per-node enrichment ──────────────────────────────────────────

def enrich_node(
    node: dict,
    xml_data: Optional[Dict[str, Any]],
    operations: Optional[Dict[str, str]],
) -> dict:
    """Apply all enrichments to a single command node.

    Modifies node in-place and returns it.
    """
    node_id = node.get("id", "")
    description = node.get("description", "")

    # 1. XML metadata
    xml_args: List[Dict[str, Any]] = []
    if xml_data:
        node["func"] = xml_data.get("func", "")
        node["help_string"] = xml_data.get("help_string", "")
        xml_args = xml_data.get("args", [])

    # 2. full_syntax (use description as full syntax — not truncated)
    node["full_syntax"] = description

    # 3. Parse parameters from description
    desc_params = parse_parameters_from_description(description)

    # 4. Merge parameters
    xml_help = xml_data.get("help_string", "") if xml_data else ""
    node["parameters"] = merge_parameters(xml_args, desc_params, xml_help)

    # 5. scope
    node["scope"] = infer_scope(node["parameters"])

    # 6. Operations
    if operations:
        node["operations"] = operations
    else:
        # Minimal self-reference
        node["operations"] = {"set": node_id}

    # 7. knowledge_faces scaffold
    node["knowledge_faces"] = {
        "syntax": description or node.get("label", ""),
        "constraint": [],
        "ui": {},
        "experience": [],
    }

    # 8. Enrich keywords
    node["keywords"] = enrich_keywords(
        node.get("keywords", []),
        node["parameters"],
    )

    return node


# ── Main graph enrichment ────────────────────────────────────────

def enrich_graph(
    graph_path: Path,
    xml_path: Path,
    output_path: Path,
    batch_size: int = _FLUSH_BATCH,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Load, enrich, and write cli_keyword_graph.json.

    Returns stats dict with counts.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    logger.info("Loading CLI graph from %s", graph_path)
    with open(graph_path, "r", encoding="utf-8") as f:
        graph = json.load(f)

    nodes: List[dict] = graph.get("nodes", [])
    edges: list = graph.get("edges", [])
    keywords_index: Dict[str, List[str]] = graph.get("keywords_index", {})

    logger.info("Loaded %d nodes, %d edges", len(nodes), len(edges))

    # Load XML
    logger.info("Loading XML from %s", xml_path)
    xml_map = extract_args_from_xml(xml_path)

    # Build operations groups
    logger.info("Building operations groups...")
    op_groups = build_operations_groups(nodes)
    logger.info("Found %d operations groups", len(op_groups))

    # Build reverse map: node_id → operations dict for that node
    # For operation variants (no/show/clear), include their group's full ops
    node_to_ops: Dict[str, Dict[str, str]] = {}
    for base_id, ops_dict in op_groups.items():
        for op_key, variant_id in ops_dict.items():
            node_to_ops[variant_id] = ops_dict

    # Build XML lookup: node_id → xml_data
    # XML map uses normalized IDs; try multiple matching strategies
    def _lookup_xml(node_id: str, label: str) -> Optional[Dict[str, Any]]:
        # 1. Direct ID match
        if node_id in xml_map:
            return xml_map[node_id]
        # 2. Label-derived ID
        label_id = label.lower().replace(" ", "_").replace("-", "_")
        if label_id in xml_map:
            return xml_map[label_id]
        # 3. Strip op prefix only for non-variant commands (no/show/clear have
        #    different parameter semantics from the set command, so never inherit).
        # 4. Partial suffix match (last 2 tokens) — only for non-variants
        base_id, op_key = _strip_op_prefix(node_id)
        if op_key is not None:
            # This is a no/show/clear variant — do NOT fall back to set's XML
            return None
        _OP_PREFIXES_SET = ("no_", "show_", "clear_", "display_")
        tokens = node_id.split("_")
        if len(tokens) >= 2:
            suffix = "_".join(tokens[-2:])
            for xid, xdata in xml_map.items():
                # Skip XML entries that are themselves op-prefix variants —
                # they carry show/clear semantics, not set semantics
                if any(xid.startswith(p) for p in _OP_PREFIXES_SET):
                    continue
                if xid.endswith(suffix):
                    return xdata
        return None

    # Enrich nodes
    stats = {"total": 0, "enriched": 0, "skipped_non_cmd": 0, "xml_matched": 0}
    enriched_nodes: List[dict] = []

    for i, node in enumerate(nodes):
        stats["total"] += 1
        ntype = node.get("type", "")

        if ntype not in ("command", "operation_command"):
            stats["skipped_non_cmd"] += 1
            enriched_nodes.append(node)
            continue

        node_id = node.get("id", "")
        label = node.get("label", "")

        xml_data = _lookup_xml(node_id, label)
        if xml_data:
            stats["xml_matched"] += 1

        ops = node_to_ops.get(node_id)
        enrich_node(node, xml_data, ops)
        stats["enriched"] += 1
        enriched_nodes.append(node)

        if (i + 1) % batch_size == 0:
            logger.info("Processed %d/%d nodes...", i + 1, len(nodes))

    # Rebuild keywords_index with new keywords
    logger.info("Rebuilding keywords_index...")
    new_index: Dict[str, List[str]] = {}
    for node in enriched_nodes:
        nid = node.get("id", "")
        for kw in node.get("keywords", []):
            if not isinstance(kw, str) or not kw:
                continue
            kw_lower = kw.lower()
            if kw_lower not in new_index:
                new_index[kw_lower] = []
            if nid not in new_index[kw_lower]:
                new_index[kw_lower].append(nid)

    graph["nodes"] = enriched_nodes
    graph["keywords_index"] = new_index

    logger.info(
        "Stats: total=%d enriched=%d xml_matched=%d skipped=%d",
        stats["total"],
        stats["enriched"],
        stats["xml_matched"],
        stats["skipped_non_cmd"],
    )

    if dry_run:
        logger.info("Dry-run: not writing output.")
        return stats

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Writing enriched graph to %s...", output_path)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=None, separators=(",", ":"))
    logger.info("Done. File size: %.1f MB", output_path.stat().st_size / 1_048_576)

    return stats


# ── CLI ──────────────────────────────────────────────────────────

def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Enrich CLI keyword graph nodes with XML args, parameters, "
                    "operations groups, and knowledge_faces scaffold.",
    )
    p.add_argument(
        "--graph",
        type=Path,
        default=_DEFAULT_GRAPH,
        help="Path to cli_keyword_graph.json (default: %(default)s)",
    )
    p.add_argument(
        "--xml",
        type=Path,
        default=_DEFAULT_XML,
        help="Path to command_tree XML (default: %(default)s)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: overwrite --graph input)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and compute but do not write output",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=_FLUSH_BATCH,
        help="Log progress every N nodes (default: %(default)s)",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    output = args.output or args.graph
    stats = enrich_graph(
        graph_path=args.graph,
        xml_path=args.xml,
        output_path=output,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
