"""CLI功能树的内存存储与子图提取。

从 cli_keyword_graph.json 加载产品 CLI 命令层级结构，
提供运行时子图查询，用于注入 global_audit prompt 和 Worker 上下文。
"""

import json
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_KB_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"


class CLIGraphStore:
    """从 cli_keyword_graph.json 加载并提供运行时子图查询。"""

    def __init__(self, graph_path: Optional[Path] = None):
        self._path = graph_path or (_KB_DIR / "cli_keyword_graph.json")
        self._loaded = False
        self._nodes_by_id: Dict[str, dict] = {}
        # Module nodes stored separately to handle duplicate IDs
        # (each module ID also has a command node with same ID)
        self._module_nodes: Dict[str, dict] = {}
        self._edges_by_source: Dict[str, List[dict]] = defaultdict(list)
        self._edges_by_target: Dict[str, List[dict]] = defaultdict(list)
        self._keywords_index: Dict[str, List[str]] = {}
        self._module_ids: Set[str] = set()
        self._hierarchy_cache: Dict[str, str] = {}
        self._kb_chunks_cache: Optional[list] = None
        self._l2_index_built = False
        self._token_l2_sets: Dict[str, Set[str]] = {}
        self._total_l2_count = 0
        self._cmd_l2_cache: Dict[str, str] = {}
        self._child_to_parent: Dict[str, str] = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self._path.exists():
            logger.warning("CLI graph file not found: %s", self._path)
            self._loaded = True
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for node in data.get("nodes", []):
                nid = node.get("id", "")
                if not nid:
                    continue
                if node.get("type") == "module":
                    self._module_ids.add(nid)
                    self._module_nodes[nid] = node
                # For duplicate IDs, module type takes priority in _nodes_by_id
                if nid not in self._nodes_by_id or node.get("type") == "module":
                    self._nodes_by_id[nid] = node
            for edge in data.get("edges", []):
                src = edge.get("source", "")
                tgt = edge.get("target", "")
                if src and tgt:
                    self._edges_by_source[src].append(edge)
                    self._edges_by_target[tgt].append(edge)
            self._keywords_index = data.get("keywords_index", {})
            logger.info(
                "CLI graph loaded: %d nodes, %d modules, %d keywords",
                len(self._nodes_by_id),
                len(self._module_ids),
                len(self._keywords_index),
            )
        except Exception as e:
            logger.error("Failed to load CLI graph: %s", e)
        self._loaded = True

    # ── Subgraph extraction ──────────────────────────────────────

    def extract_subgraph(
        self,
        module_hints: List[str],
        max_nodes: int = 60,
        max_depth: int = 2,
    ) -> dict:
        """提取与给定模块 hint 相关的子图。

        Args:
            module_hints: 模块名列表，如 ["slb directfwd", "itcpopt"]
            max_nodes: 最大返回节点数
            max_depth: BFS 展开深度

        Returns:
            {"nodes": [...], "edges": [...], "modules": [...]}
        """
        self._ensure_loaded()
        if not self._nodes_by_id:
            return {"nodes": [], "edges": [], "modules": []}

        seed_ids: Set[str] = set()
        matched_modules: Set[str] = set()

        for hint in module_hints:
            if not hint or not hint.strip():
                continue
            ids = self._resolve_hint_to_seeds(hint.strip())
            seed_ids.update(ids)
            # Track which module nodes are matched
            for nid in ids:
                node = self._nodes_by_id.get(nid)
                if node and node.get("type") == "module":
                    matched_modules.add(nid)

        if not seed_ids:
            return {"nodes": [], "edges": [], "modules": []}

        # BFS expand from seeds
        visited: Set[str] = set()
        collected_nodes: List[dict] = []
        collected_edges: List[dict] = []
        queue: deque = deque()

        for sid in seed_ids:
            if sid in self._nodes_by_id:
                queue.append((sid, 0))
                visited.add(sid)
            # If it's a module, also ensure the module node version is in the graph
            if sid in self._module_nodes and sid not in visited:
                queue.append((sid, 0))
                visited.add(sid)

        # Edge types to follow during BFS, with priority
        _expand_edge_types = {"contains", "has_operation", "parent_of"}

        while queue and len(collected_nodes) < max_nodes:
            nid, depth = queue.popleft()
            node = self._nodes_by_id.get(nid)
            if not node:
                continue
            collected_nodes.append(node)

            if depth >= max_depth:
                continue

            for edge in self._edges_by_source.get(nid, []):
                etype = edge.get("type", "")
                target = edge.get("target", "")
                if etype in _expand_edge_types and target not in visited:
                    visited.add(target)
                    queue.append((target, depth + 1))
                    collected_edges.append(edge)

        # Also collect shares_keyword edges between visited nodes (no BFS expansion)
        visited_set = {n.get("id") for n in collected_nodes}
        for node in collected_nodes:
            nid = node.get("id", "")
            for edge in self._edges_by_source.get(nid, []):
                if (
                    edge.get("type") == "shares_keyword"
                    and edge.get("target") in visited_set
                ):
                    collected_edges.append(edge)

        return {
            "nodes": collected_nodes[:max_nodes],
            "edges": collected_edges,
            "modules": sorted(matched_modules),
        }

    def _resolve_hint_to_seeds(self, hint: str) -> Set[str]:
        """将一个 module hint 解析为 seed node IDs。

        支持格式:
        - "slb directfwd" → 分词 ["slb", "directfwd"]，取交集
        - "TCP SLB > itcpopt" → 提取 "itcpopt"
        - "itcpopt" → 直接查 keywords_index
        """
        seeds: Set[str] = set()

        # Strip "TCP SLB > " or "TCP > " prefix if present
        hint_clean = hint
        if ">" in hint:
            hint_clean = hint.split(">")[-1].strip()

        tokens = hint_clean.lower().split()
        if not tokens:
            return seeds

        # 1. Direct module ID match
        for token in tokens:
            if token in self._module_ids:
                seeds.add(token)

        # 2. Keywords index lookup — intersect for multi-token
        token_node_sets: List[Set[str]] = []
        for token in tokens:
            kw_nodes = self._keywords_index.get(token)
            if kw_nodes:
                token_node_sets.append(set(kw_nodes))

        if token_node_sets:
            if len(token_node_sets) == 1:
                seeds.update(token_node_sets[0])
            else:
                # Intersection for multi-token hints (e.g., "slb directfwd")
                intersection = token_node_sets[0]
                for s in token_node_sets[1:]:
                    intersection = intersection & s
                if intersection:
                    seeds.update(intersection)
                else:
                    # No intersection — union top keyword results
                    for s in token_node_sets:
                        seeds.update(s)

        # 3. Also find parent module for matched command nodes
        module_seeds: Set[str] = set()
        for sid in list(seeds):
            node = self._nodes_by_id.get(sid)
            if not node:
                continue
            ntype = node.get("type", "")
            if ntype in ("command", "operation_command"):
                # Find parent module via edges
                parent = node.get("parent", "")
                if parent and parent in self._module_ids:
                    module_seeds.add(parent)
                # Also check actual_module field
                actual = node.get("actual_module", "")
                if actual and actual in self._module_ids:
                    module_seeds.add(actual)
        seeds.update(module_seeds)

        return seeds

    # ── Command existence check ───────────────────────────────────

    def command_exists(self, cmd_text: str) -> Tuple[bool, List[str]]:
        """检查 CLI 命令是否在图谱中存在。

        Args:
            cmd_text: CLI 命令文本，如 "slb mode ircookie" 或 "no slb transparent"

        Returns:
            (exists, similar_commands) — exists 为精确/模糊匹配结果，
            similar_commands 为按 token 匹配到的相近命令 label 列表。
        """
        self._ensure_loaded()
        if not cmd_text or not self._nodes_by_id:
            return False, []

        cleaned = cmd_text.strip().lower()
        for prefix in ("no ", "show ", "clear "):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break

        cmd_node_labels: Dict[str, str] = {}
        for nid, node in self._nodes_by_id.items():
            ntype = node.get("type", "")
            if ntype in ("command", "operation_command"):
                label = node.get("label", nid).lower()
                cmd_node_labels[nid] = label

        for nid, label in cmd_node_labels.items():
            if cleaned == label or cleaned.replace(" ", "_") == nid:
                return True, [self._nodes_by_id[nid].get("label", nid)]

        tokens = cleaned.split()
        if not tokens:
            return False, []

        scored: Dict[str, int] = {}
        for nid, label in cmd_node_labels.items():
            label_tokens = set(label.split())
            overlap = sum(1 for t in tokens if t in label_tokens)
            if overlap > 0:
                scored[nid] = overlap

        if not scored:
            for t in tokens:
                kw_nodes = self._keywords_index.get(t, [])
                for nid in kw_nodes:
                    if nid in cmd_node_labels:
                        scored[nid] = scored.get(nid, 0) + 1

        if not scored:
            return False, []

        max_score = max(scored.values())
        threshold = max(1, len(tokens) * 0.6)
        if max_score >= len(tokens):
            best_nid = max(scored, key=scored.get)
            return True, [self._nodes_by_id[best_nid].get("label", best_nid)]

        similar = sorted(scored, key=scored.get, reverse=True)[:5]
        similar_labels = [self._nodes_by_id[nid].get("label", nid) for nid in similar]
        return False, similar_labels

    # ── Hierarchy prefix derivation (deep search) ──────────────

    _CLI_SYNTAX = frozenset({"show", "no", "clear", "display"})

    def get_hierarchy_prefix(self, module_hint: str) -> str:
        """从模块 hint 推导 function_hierarchy L2 前缀。

        三层策略 (deep search):
        1. 树匹配: 将 hint 映射到 CLI graph 命令节点 (结构匹配)
        2. KB 注释: 用 IDF 最高的 key token 在 KB 中投票确定 L2,
           L2 名称亲和度打破平票
        3. 父节点回溯: 沿 parent_of 边向上查找已注释的祖先

        Args:
            module_hint: 模块描述，如 "ircookie"、"slb directfwd"

        Returns:
            L2 前缀字符串，如 "SLB > Cookie"。无法推导时返回 ""。
        """
        self._ensure_loaded()
        if not module_hint or not module_hint.strip():
            return ""

        cache_key = module_hint.strip().lower()
        if cache_key in self._hierarchy_cache:
            return self._hierarchy_cache[cache_key]

        cmd_id = self._tree_match_cmd(module_hint.strip())
        if not cmd_id:
            self._hierarchy_cache[cache_key] = ""
            return ""

        result = self._resolve_cmd_l2(cmd_id)
        logger.debug(
            "get_hierarchy_prefix: hint=%r -> cmd=%s -> %s",
            module_hint, cmd_id, result,
        )
        self._hierarchy_cache[cache_key] = result
        return result

    def _tree_match_cmd(self, hint: str) -> Optional[str]:
        """将 hint 映射到 CLI graph 中最深匹配的命令节点。"""
        tokens = hint.lower().split()
        for length in range(len(tokens), 0, -1):
            for start in range(len(tokens) - length + 1):
                cid = "_".join(tokens[start:start + length])
                if cid in self._nodes_by_id:
                    return cid
        token_set = set(tokens)
        matches = [
            nid for nid, node in self._nodes_by_id.items()
            if node.get("type") in ("command", "operation_command")
            and token_set.issubset(set(nid.split("_")))
        ]
        if matches:
            return min(matches, key=len)
        seeds = self._resolve_hint_to_seeds(hint)
        return seeds[0] if seeds else None

    def _resolve_cmd_l2(self, cmd_id: str) -> str:
        """三阶段策略推导命令所属 L2 branch。

        Stage 1 — Forward Tree Match: 沿 parent_of 链上溯找 depth=1 branch,
                  然后从 KB fh 映射到 L2 前缀字符串。
        Stage 2 — Backward Param Resolve: 命令参数做 trunk 关联线索 (不改 L2 归属)。
        Stage 3 — KB Voting (兜底): 旧 IDF 投票逻辑。
        """
        self._build_l2_index()

        branch_id = self._forward_tree_match(cmd_id)
        if branch_id:
            l2_prefix = self._branch_to_l2_prefix(branch_id)
            if l2_prefix:
                return l2_prefix

        visited: Set[str] = set()
        current: Optional[str] = cmd_id
        while current and current not in visited:
            visited.add(current)
            l2 = self._annotate_cmd_l2(current)
            if l2:
                return l2
            current = self._child_to_parent.get(current)
        return ""

    def _forward_tree_match(self, cmd_id: str) -> str:
        """沿 parent_of 链上溯，返回 depth=1 的 branch node ID (即 L2)。

        从 cmd_id 向上走 parent_of 边，当 parent 是 module (L1) 时，
        current 就是 L2 branch。
        """
        current = cmd_id
        visited: Set[str] = set()
        while current and current not in visited:
            visited.add(current)
            parent = self._child_to_parent.get(current)
            if not parent:
                break
            if parent in self._module_ids:
                return current
            current = parent
        return ""

    def _branch_to_l2_prefix(self, branch_id: str) -> str:
        """将 CLI graph branch node ID 映射为 KB 风格的 L2 前缀字符串。

        查找 branch 所属 module 的 label 和 branch 自身 label，
        组装为 "Module Label > Branch Label" 格式。
        """
        parent_module = self._child_to_parent.get(branch_id)
        if not parent_module or parent_module not in self._module_ids:
            return ""
        module_node = self._module_nodes.get(parent_module, {})
        branch_node = self._nodes_by_id.get(branch_id, {})

        module_label = module_node.get("label", parent_module)
        branch_label = branch_node.get("label", branch_id)

        if not module_label or not branch_label:
            return ""

        prefix = f"{module_label} > {branch_label}"

        if not self._kb_chunks_cache:
            return prefix

        prefix_lower = prefix.lower().replace(" ", "")
        branch_tokens = set(branch_id.lower().split("_"))
        branch_tokens -= self._CLI_SYNTAX

        best_match = ""
        best_score = 0
        seen_l2s: Set[str] = set()
        for chunk in self._kb_chunks_cache:
            fh = self._extract_fh(chunk)
            if not fh:
                continue
            parts = [p.strip() for p in fh.split(">")]
            if len(parts) < 2:
                continue
            kb_prefix = f"{parts[0]} > {parts[1]}"
            if kb_prefix in seen_l2s:
                continue
            seen_l2s.add(kb_prefix)

            kb_lower = kb_prefix.lower().replace(" ", "")
            score = 0
            for t in branch_tokens:
                if len(t) >= 3 and t in kb_lower:
                    score += 3
            if score > best_score:
                best_score = score
                best_match = kb_prefix

        return best_match if best_score >= 3 else prefix

    def _backward_param_resolve(self, cmd_id: str) -> List[Tuple[str, str]]:
        """从命令 description 的参数推导 trunk 依赖线索。

        返回 [(param_name, related_branch_id), ...] 表示命令参数
        引用了其他 branch 的实体。仅提供 trunk 线索，不改变 L2 归属。
        """
        import re as _re
        node = self._nodes_by_id.get(cmd_id, {})
        desc = node.get("description", "")
        if not desc:
            return []

        required = _re.findall(r"<(\w+)>", desc)
        optional = _re.findall(r"\[(\w+)\]", desc)
        all_params = [(p, True) for p in required] + [(p, False) for p in optional]

        cmd_branch = self._forward_tree_match(cmd_id)
        results: List[Tuple[str, str]] = []

        for param, _is_required in all_params:
            param_parts = [kp for kp in param.lower().split("_") if len(kp) >= 4]
            if not param_parts:
                continue
            for mid in self._module_ids:
                for edge in self._edges_by_source.get(mid, []):
                    if edge.get("type") != "parent_of":
                        continue
                    child_id = edge.get("target", "")
                    if child_id == cmd_branch:
                        continue
                    child_node = self._nodes_by_id.get(child_id, {})
                    child_label = child_node.get("label", child_id).lower()
                    for kp in param_parts:
                        if kp in child_label or kp in child_id.lower():
                            results.append((param, child_id))
                            break

        return results

    # ── L2 index (lazy build) ────────────────────────────────

    def _build_l2_index(self) -> None:
        if self._l2_index_built:
            return
        self._l2_index_built = True

        self._child_to_parent: Dict[str, str] = {}
        for edges in self._edges_by_source.values():
            for e in edges:
                if e.get("type") == "parent_of":
                    self._child_to_parent[e["target"]] = e["source"]

        if self._kb_chunks_cache is None:
            try:
                kb_path = _KB_DIR / "knowledge_base.json"
                if not kb_path.exists():
                    return
                with open(kb_path, "r", encoding="utf-8") as f:
                    self._kb_chunks_cache = json.load(f)
            except Exception:
                return

        import re as _re
        self._token_l2_sets: Dict[str, Set[str]] = defaultdict(set)
        for chunk in (self._kb_chunks_cache or []):
            fh = self._extract_fh(chunk)
            if not fh:
                continue
            parts = [p.strip() for p in fh.split(">")]
            if len(parts) < 2:
                continue
            prefix = f"{parts[0]} > {parts[1]}"
            text = str(
                chunk.get("page_content") or chunk.get("text") or ""
            ).lower()
            for t in _re.findall(r"[a-z][a-z0-9]{2,}", text):
                self._token_l2_sets[t].add(prefix)

        all_l2s: Set[str] = set()
        for v in self._token_l2_sets.values():
            all_l2s.update(v)
        self._total_l2_count = len(all_l2s)

        logger.info(
            "L2 index built: %d unique tokens, %d L2 prefixes",
            len(self._token_l2_sets), self._total_l2_count,
        )

    def _token_idf(self, token: str) -> float:
        n = len(self._token_l2_sets.get(token, set()))
        if n == 0 or self._total_l2_count == 0:
            return 0.0
        import math
        return math.log(self._total_l2_count / n)

    def _annotate_cmd_l2(self, cmd_id: str) -> str:
        if cmd_id in self._cmd_l2_cache:
            return self._cmd_l2_cache[cmd_id]

        node = self._nodes_by_id.get(cmd_id, {})
        label = node.get("label", cmd_id).lower()
        label_tokens = [
            t for t in label.split()
            if len(t) >= 2 and t not in self._CLI_SYNTAX
        ]
        if not label_tokens:
            self._cmd_l2_cache[cmd_id] = ""
            return ""

        ranked = sorted(label_tokens, key=self._token_idf, reverse=True)
        key_token = ranked[0]

        from collections import Counter
        l2_votes: Counter = Counter()

        for chunk in (self._kb_chunks_cache or []):
            fh = self._extract_fh(chunk)
            if not fh:
                continue
            parts = [p.strip() for p in fh.split(">")]
            if len(parts) < 2:
                continue
            prefix = f"{parts[0]} > {parts[1]}"

            fh_lower = fh.lower().replace(" ", "")
            if key_token in fh_lower:
                l2_votes[prefix] += 10
                continue

            text = str(
                chunk.get("page_content") or chunk.get("text") or ""
            ).lower()
            cmd_label = cmd_id.replace("_", " ")
            if cmd_label in text:
                l2_votes[prefix] += 2
            elif key_token in text:
                l2_votes[prefix] += 1

        self._param_l2_boost(cmd_id, l2_votes)

        result = l2_votes.most_common(1)[0][0] if l2_votes else ""
        self._cmd_l2_cache[cmd_id] = result
        return result

    def _param_l2_boost(self, cmd_id: str, l2_votes: "Counter") -> None:
        """从 description 的参数名推导 L2 关联并 boost 投票。

        <required_param> → +8, [optional_param] → +4。
        参数名中 >=4 字符的词根与 L2 名称做子串匹配。
        """
        import re as _re
        node = self._nodes_by_id.get(cmd_id, {})
        desc = node.get("description", "")
        if not desc:
            return
        required = _re.findall(r"<(\w+)>", desc)
        optional = _re.findall(r"\[(\w+)\]", desc)
        for param, weight in (
            [(p, 8) for p in required] + [(p, 4) for p in optional]
        ):
            key_parts = [
                kp for kp in param.lower().split("_") if len(kp) >= 4
            ]
            for prefix in list(l2_votes.keys()):
                l2_name_lower = prefix.split(" > ")[-1].lower()
                for kp in key_parts:
                    if kp in l2_name_lower:
                        l2_votes[prefix] += weight
                        break

    @staticmethod
    def _extract_fh(chunk: dict) -> str:
        meta = chunk.get("metadata") or {}
        fh = meta.get("function_hierarchy", "")
        if not fh:
            rm = meta.get("regex_metadata") or {}
            if isinstance(rm, dict):
                fh = rm.get("function_hierarchy", "")
        return fh

    # ── Prompt formatting ────────────────────────────────────────

    def format_for_prompt(
        self, subgraph: dict, max_chars: int = 1500
    ) -> str:
        """将子图格式化为 LLM 可读的层级文本。"""
        nodes = subgraph.get("nodes", [])
        edges = subgraph.get("edges", [])
        if not nodes:
            return ""

        # Group by module
        modules: Dict[str, dict] = {}  # module_id -> module_node
        children: Dict[str, List[dict]] = defaultdict(list)  # module_id -> [cmd nodes]
        keyword_links: List[Tuple[str, str, str]] = []

        for node in nodes:
            ntype = node.get("type", "")
            nid = node.get("id", "")
            if ntype == "module":
                modules[nid] = node
            elif nid in self._module_nodes and nid not in modules:
                # Node was stored as command but is actually a module
                modules[nid] = self._module_nodes[nid]
            if ntype in ("command", "operation_command"):
                # Find owning module
                owner = self._find_owner_module(node, nodes)
                if owner:
                    children[owner].append(node)

        for edge in edges:
            if edge.get("type") == "shares_keyword":
                src_label = self._node_label(edge.get("source", ""))
                tgt_label = self._node_label(edge.get("target", ""))
                kw = edge.get("keyword", "")
                if src_label and tgt_label and kw:
                    keyword_links.append((src_label, tgt_label, kw))

        lines = ["[产品功能树]"]
        for mid in sorted(modules.keys()):
            mnode = modules[mid]
            label = mnode.get("label", mid)
            cmd_count = mnode.get("commands_count", 0)
            help_str = (mnode.get("help_string") or "")[:60].strip()
            header = f"{label} 模块 ({cmd_count} commands)"
            if help_str:
                header += f" — {help_str}"
            lines.append(header + ":")

            # Enriched tech features (if present from enrich_cli_graph.py)
            tech_parts = []
            proto = mnode.get("protocol_stack")
            if proto:
                tech_parts.append(f"协议: {', '.join(proto)}")
            af = mnode.get("address_family")
            if af:
                tech_parts.append(f"地址族: {', '.join(af)}")
            layer = mnode.get("layer")
            if layer:
                tech_parts.append(f"层级: {layer}")
            iface = mnode.get("interface_types")
            if iface:
                tech_parts.append(f"接口: {', '.join(iface)}")
            related = mnode.get("related_modules")
            if related:
                tech_parts.append(
                    f"关联模块: {', '.join(related[:10])}"
                    + (f" 等{len(related)}个" if len(related) > 10 else "")
                )
            tags = mnode.get("feature_tags")
            if tags:
                tech_parts.append(f"功能标签: {', '.join(tags)}")
            if tech_parts:
                lines.append(f"  [{' | '.join(tech_parts)}]")

            cmds = children.get(mid, [])
            # Group commands by first keyword / prefix for hierarchy
            cmd_labels = sorted(
                set(c.get("label", c.get("id", "")) for c in cmds)
            )
            for i, clabel in enumerate(cmd_labels[:15]):
                prefix = "  └─ " if i == len(cmd_labels[:15]) - 1 else "  ├─ "
                desc = ""
                # Find the description from the node
                for c in cmds:
                    if c.get("label") == clabel or c.get("id") == clabel:
                        desc = (c.get("description") or "")[:50].strip()
                        break
                line = f"{prefix}{clabel}"
                if desc and desc != clabel:
                    line += f" — {desc}"
                lines.append(line)
            if len(cmd_labels) > 15:
                lines.append(f"  ... 共 {len(cmd_labels)} 条命令")

        # Add keyword links
        seen_links: Set[str] = set()
        link_lines = []
        for src, tgt, kw in keyword_links[:5]:
            key = f"{src}-{tgt}-{kw}"
            if key not in seen_links:
                seen_links.add(key)
                link_lines.append(f"  {src} ←[{kw}]→ {tgt}")
        if link_lines:
            lines.append("关联关系:")
            lines.extend(link_lines)

        result = "\n".join(lines)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n...[截断]..."
        return result

    def _find_owner_module(self, cmd_node: dict, all_nodes: List[dict]) -> str:
        """Find the module that owns a command node."""
        # Check actual_module field first
        actual = cmd_node.get("actual_module", "")
        if actual and actual in self._module_ids:
            return actual
        # Check parent field
        parent = cmd_node.get("parent", "")
        if parent and parent in self._module_ids:
            return parent
        # Check if parent is in collected nodes
        nid = cmd_node.get("id", "")
        for edge in self._edges_by_target.get(nid, []):
            src = edge.get("source", "")
            etype = edge.get("type", "")
            if etype in ("contains", "has_operation") and src in self._module_ids:
                return src
        return ""

    def _node_label(self, nid: str) -> str:
        node = self._nodes_by_id.get(nid)
        if node:
            return node.get("label", nid)
        return nid

    def seed_skeleton_index(self) -> int:
        """Seed SkeletonIndex with module data from this CLI graph.

        Seeds both L1 modules (101 top-level) and L2 features
        (from function_hierarchy in knowledge_base.json).

        Returns the total number of nodes seeded.
        """
        from INAGENT.rag.skeleton_index import get_skeleton_index

        self._ensure_loaded()
        si = get_skeleton_index()
        l1_count = si.seed_from_cli_graph(self)
        l2_count = si.seed_features_from_cli_tree(self)
        return l1_count + l2_count

    # ── Trunk derivation (1C) ────────────────────────────────────

    _LAYER_ORDER = ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]
    _PLATFORM_TAGS = frozenset({
        "high-availability", "ha", "redundancy", "failover",
        "logging", "snmp", "syslog",
    })
    _PLATFORM_MODULES = frozenset({
        "log", "turbo", "ha", "sync", "snmp", "syslog", "ntp",
    })

    def derive_trunk(self, module_id: str) -> Dict[str, List[str]]:
        """自动推导模块的躯干依赖 (OSI 层 + 管理/数据/平台面)。

        数据驱动: 从 module 自身的 layer/protocol_stack/feature_tags/
        related_modules 推导，不硬编码具体协议名。

        Returns:
            {"layers": ["L7","L4","L3"], "planes": ["management","data","platform"]}
        """
        self._ensure_loaded()
        node = self._module_nodes.get(module_id)
        if not node:
            return {"layers": [], "planes": []}

        layer_str = node.get("layer", "")
        layers_in_module: List[str] = []
        for l in self._LAYER_ORDER:
            if l in layer_str.upper():
                layers_in_module.append(l)

        dep_layers: Set[str] = set()
        if layers_in_module:
            max_idx = max(self._LAYER_ORDER.index(l) for l in layers_in_module)
            for i in range(max_idx + 1):
                layer_val = self._LAYER_ORDER[i]
                if layer_val in ("L5", "L6"):
                    continue
                dep_layers.add(layer_val)
        dep_layers.update(layers_in_module)

        sorted_layers = sorted(dep_layers, key=lambda x: self._LAYER_ORDER.index(x), reverse=True)

        planes: Set[str] = set()
        planes.add("management")

        proto = node.get("protocol_stack", [])
        if proto:
            planes.add("data")

        tags = [t.lower() for t in node.get("feature_tags", [])]
        related = [r.lower() for r in node.get("related_modules", [])]

        has_platform = False
        for t in tags:
            if t in self._PLATFORM_TAGS:
                has_platform = True
                break
        if not has_platform:
            for r in related:
                if r in self._PLATFORM_MODULES:
                    has_platform = True
                    break
        if has_platform:
            planes.add("platform")

        sorted_planes = sorted(planes)

        return {"layers": sorted_layers, "planes": sorted_planes}

    # ── Cross-branch connectivity (1D) ───────────────────────────

    def l2_connected(self, branch_a: str, branch_b: str, max_hops: int = 3) -> bool:
        """两个 L2 branch 是否通过 shares_keyword 边连通。

        BFS 跟随 shares_keyword 边，cycle-safe (visited set)。
        仅检查命令节点层面的连通性，然后映射回 branch。
        """
        self._ensure_loaded()
        self._build_l2_index()

        a_lower = branch_a.lower().replace(" ", "_")
        b_lower = branch_b.lower().replace(" ", "_")

        children_a = self._get_branch_children(a_lower)
        children_b = self._get_branch_children(b_lower)

        if not children_a or not children_b:
            return False

        target_set = set(children_b)
        visited: Set[str] = set()
        queue: deque = deque()
        for c in children_a:
            queue.append((c, 0))
            visited.add(c)

        while queue:
            nid, depth = queue.popleft()
            if nid in target_set:
                return True
            if depth >= max_hops:
                continue
            for edge in self._edges_by_source.get(nid, []):
                if edge.get("type") != "shares_keyword":
                    continue
                tgt = edge.get("target", "")
                if tgt and tgt not in visited:
                    visited.add(tgt)
                    queue.append((tgt, depth + 1))
            for edge in self._edges_by_target.get(nid, []):
                if edge.get("type") != "shares_keyword":
                    continue
                src = edge.get("source", "")
                if src and src not in visited:
                    visited.add(src)
                    queue.append((src, depth + 1))

        return False

    def _get_branch_children(self, branch_id: str) -> Set[str]:
        """获取 branch 下属的所有命令节点 ID。"""
        children: Set[str] = set()
        if branch_id not in self._nodes_by_id:
            for nid in self._nodes_by_id:
                if nid.lower() == branch_id:
                    branch_id = nid
                    break
            else:
                return children

        queue: deque = deque([branch_id])
        visited: Set[str] = {branch_id}
        while queue:
            nid = queue.popleft()
            node = self._nodes_by_id.get(nid, {})
            if node.get("type") in ("command", "operation_command"):
                children.add(nid)
            for edge in self._edges_by_source.get(nid, []):
                if edge.get("type") == "parent_of":
                    child = edge.get("target", "")
                    if child and child not in visited:
                        visited.add(child)
                        queue.append(child)

        return children

    def get_branch_ids(self, module_id: str) -> List[str]:
        """返回模块下所有 depth=1 的 branch node ID。"""
        self._ensure_loaded()
        self._build_l2_index()
        branches = []
        for edge in self._edges_by_source.get(module_id, []):
            if edge.get("type") == "parent_of":
                child_id = edge.get("target", "")
                if child_id and child_id not in self._module_ids:
                    branches.append(child_id)
        return branches

    # ── New methods added in Phase B ─────────────────────────────

    def get_operations_group(self, command_id: str) -> Dict[str, str]:
        """返回命令的操作组 {set/no/show/clear → node_id}。

        从图中找出与 command_id 同 base 的所有操作变体。
        对 set 命令返回全组；对 no/show/clear 变体也返回同一组。
        """
        self._ensure_loaded()
        if not command_id:
            return {}

        # Normalize: strip known op prefixes
        _PREFIXES = ("no_", "show_", "clear_", "display_")
        base_id = command_id
        for prefix in _PREFIXES:
            if command_id.startswith(prefix):
                base_id = command_id[len(prefix):]
                break

        # Check if enriched node already has operations dict
        node = self._nodes_by_id.get(command_id)
        if node and isinstance(node.get("operations"), dict):
            return node["operations"]

        # Fallback: scan all command nodes for matching prefix
        ops: Dict[str, str] = {}
        if base_id in self._nodes_by_id:
            ops["set"] = base_id
        for prefix, op_key in (
            ("no_", "no"),
            ("show_", "show"),
            ("clear_", "clear"),
            ("display_", "show"),
        ):
            variant_id = prefix + base_id
            if variant_id in self._nodes_by_id:
                ops[op_key] = variant_id
        return ops

    def get_knowledge_face(self, node_id: str, face: str) -> Any:
        """返回节点的指定知识面内容。

        Args:
            node_id: 命令节点 ID
            face: "syntax" | "constraint" | "ui" | "experience"

        Returns:
            face 对应的值（str/list/dict），节点不存在或无 face 返回 None。
        """
        self._ensure_loaded()
        node = self._nodes_by_id.get(node_id)
        if not node:
            return None
        faces = node.get("knowledge_faces")
        if not isinstance(faces, dict):
            return None
        return faces.get(face)

    def get_cooperates_with(self, node_id: str) -> List[str]:
        """返回通过 cooperates_with 边连接的邻居节点 ID 列表。"""
        self._ensure_loaded()
        result: List[str] = []
        for edge in self._edges_by_source.get(node_id, []):
            if edge.get("type") == "cooperates_with":
                target = edge.get("target", "")
                if target:
                    result.append(target)
        for edge in self._edges_by_target.get(node_id, []):
            if edge.get("type") == "cooperates_with":
                source = edge.get("source", "")
                if source:
                    result.append(source)
        return list(dict.fromkeys(result))


# ── Singleton ────────────────────────────────────────────────────

_instance: Optional[CLIGraphStore] = None


def get_cli_graph_store(graph_path: Optional[Path] = None) -> CLIGraphStore:
    global _instance
    if _instance is None:
        _instance = CLIGraphStore(graph_path)
    return _instance
