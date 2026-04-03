from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from INAGENT.config.project_config import cfg_bool, cfg_float, cfg_int

_ib_logger = logging.getLogger(__name__)


def _simhash_64(text: str) -> int:
    tokens = text.lower().split()
    if not tokens:
        return 0
    v = [0] * 64
    for token in tokens:
        h = hash(token) & ((1 << 64) - 1)
        for i in range(64):
            v[i] += 1 if h & (1 << i) else -1
    return sum(1 << i for i in range(64) if v[i] > 0)


def _dedup_context_blocks(*contexts: str) -> str:
    seen: set = set()
    blocks: list = []
    for ctx in contexts:
        if not ctx:
            continue
        for block in ctx.split("\n\n"):
            stripped = block.strip()
            if not stripped or len(stripped) < 30:
                blocks.append(block)
                continue
            h = _simhash_64(stripped[:300])
            dup = False
            for s in seen:
                if bin(h ^ s).count("1") <= 3:
                    dup = True
                    break
            if not dup:
                seen.add(h)
                blocks.append(block)
    return "\n\n".join(blocks)
from INAGENT.toolkits.knowledge_toolkit import KnowledgeToolkit


@dataclass
class BugInfo:
    bug_id: str = ""
    title: str = ""
    root_cause: str = ""
    testing_suggestions: str = ""
    affected_release: str = ""
    affected_files: List[str] = field(default_factory=list)
    extra_impact: str = ""
    bug_detail_raw: str = ""


@dataclass
class ReviewInput:
    mode: str
    test_cases_text: str
    review_rules: str
    product_knowledge: str
    cli_reference: str
    similar_tests: str
    bug_info: Optional[BugInfo] = None
    key_questions: List[str] = field(default_factory=list)
    sheet_role: str = ""
    function_hierarchy_prefix: str = ""
    retrieval_confidence: Optional[Dict[str, Any]] = None


class ReviewInputBuilder:
    """将评审原始输入构建为稳定可复用的结构化输入。"""

    def __init__(self, unified_rag=None, rules_engine=None, knowledge_router=None):
        self._knowledge_router = knowledge_router
        self.knowledge_tk = KnowledgeToolkit(
            mode="test_review",
            unified_rag=unified_rag,
            rules_engine=rules_engine,
        )

    @staticmethod
    def _extract_sheet_name(text: str) -> str:
        m = re.search(r"\*\*Sheet\*\*:\s*(.+)", text)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_module_hint(text: str) -> str:
        names = re.findall(r"### 模块 \d+: (.+)", text)
        if names:
            return "、".join(n.strip() for n in names if n.strip())
        m = re.search(r"\*\*模块\*\*:\s*(.+)", text)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_protocol_hint(text: str) -> str:
        from INAGENT.scripts.enrich_cli_graph import PROTOCOL_KEYWORD_MAP
        ltxt = (text or "").lower()
        seen: set = set()
        protocols: list = []
        for kw, proto in PROTOCOL_KEYWORD_MAP.items():
            if proto not in seen and re.search(rf"\b{re.escape(kw)}\b", ltxt):
                seen.add(proto)
                protocols.append(proto)
        return "/".join(protocols[:5])

    @staticmethod
    def _extract_cli_hints(text: str) -> str:
        """从测试描述中提取可能的 CLI 命令关键词，提升 CLI 检索命中率。"""
        raw = text or ""
        patterns = [
            r"system\s+tune\s+[a-zA-Z0-9_ ]+",
            r"slb\s+[a-zA-Z0-9_ ]+",
            r"show\s+[a-zA-Z0-9_ ]+",
            r"no\s+slb\s+[a-zA-Z0-9_ ]+",
        ]
        hints: List[str] = []
        seen = set()
        for pat in patterns:
            for m in re.findall(pat, raw, flags=re.IGNORECASE):
                cmd = re.sub(r"\s+", " ", m.strip().lower())
                if cmd and cmd not in seen:
                    seen.add(cmd)
                    hints.append(cmd)
                if len(hints) >= 8:
                    break
            if len(hints) >= 8:
                break
        return " ; ".join(hints)

    @staticmethod
    def _build_bug_info(bug_profile: Optional[Dict[str, Any]]) -> Optional[BugInfo]:
        if not bug_profile:
            return None
        return BugInfo(
            bug_id=str(
                bug_profile.get("bug_id")
                or bug_profile.get("Bug ID")
                or bug_profile.get("id")
                or ""
            ),
            title=str(bug_profile.get("title") or bug_profile.get("Title") or ""),
            root_cause=str(
                bug_profile.get("root_cause") or bug_profile.get("Root Cause") or ""
            ),
            testing_suggestions=str(
                bug_profile.get("testing_suggestions")
                or bug_profile.get("Testing Suggestions")
                or ""
            ),
            affected_release=str(
                bug_profile.get("affected_release")
                or bug_profile.get("Affected Release")
                or ""
            ),
            affected_files=list(bug_profile.get("affected_files") or []),
            extra_impact=str(
                bug_profile.get("extra_impact")
                or bug_profile.get("Extra Impact")
                or ""
            ),
            bug_detail_raw=str(
                bug_profile.get("bug_detail_raw")
                or bug_profile.get("Bug_Detail_Raw")
                or bug_profile.get("raw_text")
                or ""
            ),
        )

    @staticmethod
    def _build_rag_queries(
        module_hint: str,
        protocol_hint: str,
        bug_info: Optional[BugInfo],
        module_focus_words: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        rag_queries: List[Dict[str, Any]] = []
        # Bug 特定查询在最前面（priority=0），确保不被截断
        if bug_info:
            if bug_info.root_cause:
                rag_queries.append({
                    "query": f"{bug_info.root_cause} {protocol_hint}".strip(),
                    "priority": 0,
                })
            if bug_info.title and bug_info.title != bug_info.root_cause:
                rag_queries.append({
                    "query": f"{bug_info.title} {protocol_hint}".strip(),
                    "priority": 0,
                })
            if bug_info.testing_suggestions:
                rag_queries.append({
                    "query": f"{bug_info.testing_suggestions} {protocol_hint}".strip(),
                    "priority": 1,
                })
        # 模块特征词补充查询（从 manager_taskpack focus_points 提取），
        # 确保模块级知识能被检索到（如 directfwd、faststack 等子功能名）
        if module_focus_words:
            for word in module_focus_words[:3]:
                w = word.strip()
                if w and w != module_hint:
                    rag_queries.append({
                        "query": f"{w} CLI 命令 配置参考 {protocol_hint}".strip(),
                        "priority": 1,
                    })
        if module_hint:
            rag_queries.append({"query": f"{module_hint} 功能规格 设计要求", "priority": 2})
        if protocol_hint:
            rag_queries.append({"query": f"{protocol_hint} CLI 配置命令 语法 参考", "priority": 3})
        return rag_queries

    @staticmethod
    def _build_relation_query(
        module_hint: str,
        protocol_hint: str,
        module_focus_words: Optional[List[str]] = None,
        bug_info: Optional[BugInfo] = None,
    ) -> str:
        """构建“功能关系视图”检索查询（通用，不依赖硬编码规则）。"""
        focus = [w.strip() for w in (module_focus_words or []) if w and w.strip()]
        focus = focus[:4]
        bug_terms: List[str] = []
        if bug_info:
            for raw in [bug_info.root_cause, bug_info.title, bug_info.testing_suggestions]:
                token = (raw or "").strip()
                if token:
                    bug_terms.append(token[:80])
        parts = [
            module_hint,
            protocol_hint,
            "功能关系 别名 依赖 实现模式",
            " ".join(focus),
            " ".join(bug_terms[:2]),
        ]
        return " ".join([p for p in parts if p]).strip()

    def build(
        self,
        test_cases_text: str,
        bug_profile: Optional[Dict[str, Any]] = None,
        cache: Optional[Dict[str, str]] = None,
    ) -> ReviewInput:
        module_hint = self._extract_module_hint(test_cases_text)
        protocol_hint = self._extract_protocol_hint(test_cases_text)
        sheet_name = self._extract_sheet_name(test_cases_text)
        bug_info = self._build_bug_info(bug_profile)

        key_questions: List[str] = []
        raw_q = str((bug_profile or {}).get("Key_Questions", "")).strip()
        if raw_q:
            key_questions = [q.strip("- ").strip() for q in raw_q.splitlines() if q.strip()]

        sheet_role = str((bug_profile or {}).get("Sheet_Context", "")).strip()
        manager_taskpack = {}
        if isinstance(bug_profile, dict):
            raw_pack = bug_profile.get("__manager_taskpack__")
            if isinstance(raw_pack, dict):
                manager_taskpack = raw_pack
        manager_plan = manager_taskpack.get("manager_plan") if isinstance(manager_taskpack, dict) else {}
        worker_tasks = manager_taskpack.get("worker_tasks") if isinstance(manager_taskpack, dict) else {}
        active_worker_type = str(manager_taskpack.get("active_worker_type") or "").strip().lower()
        active_task_id = str(manager_taskpack.get("active_task_id") or "").strip()
        active_scope = str(manager_plan.get("sheet_scope") or "").strip() if isinstance(manager_plan, dict) else ""
        active_task = worker_tasks.get(active_worker_type) if isinstance(worker_tasks, dict) else {}
        # 从 manager_taskpack focus_points 提取模块特征词，用于针对性检索
        module_focus_words: List[str] = []
        if isinstance(worker_tasks, dict):
            for _wt, payload in worker_tasks.items():
                if not isinstance(payload, dict):
                    continue
                for fp in (payload.get("focus_points") or []):
                    w = str(fp).strip()
                    # 排除过长的句子（key_questions），只取模块/功能名短词
                    if w and len(w) <= 30 and w not in module_focus_words:
                        module_focus_words.append(w)
                if module_focus_words:
                    break  # 所有 worker 共享 focus_points，取一组即可
        # Bug 特定查询优先（root_cause, title, affected files）
        bug_queries = self._build_rag_queries(module_hint, protocol_hint, bug_info, module_focus_words)
        # suggested_queries 去重并限制数量，避免挤出高价值 bug 查询
        max_suggested = cfg_int(
            "bug_to_case.rag.max_suggested_queries",
            2,
            env="BUG_TO_CASE_MAX_SUGGESTED_QUERIES",
        )
        extra_queries: List[str] = []
        if isinstance(active_task, dict):
            extra_queries.extend([str(q).strip() for q in (active_task.get("suggested_queries") or []) if str(q).strip()])
        if isinstance(worker_tasks, dict):
            for _wt, payload in worker_tasks.items():
                if not isinstance(payload, dict):
                    continue
                extra_queries.extend(
                    [str(q).strip() for q in (payload.get("suggested_queries") or []) if str(q).strip()]
                )
        if extra_queries:
            seen_query = set()
            merged = []
            for q in extra_queries:
                if q in seen_query:
                    continue
                seen_query.add(q)
                merged.append({"query": q, "priority": 5})
            # 限制 suggested_queries 数量，为 bug 查询保留槽位
            merged = merged[:max(1, max_suggested)]
        else:
            merged = []
        # Bug 查询在前，suggested 在后
        rag_queries = bug_queries + merged
        max_rag_queries = max(
            1,
            cfg_int(
                "bug_to_case.rag.max_rag_queries",
                5,
                env="BUG_TO_CASE_MAX_RAG_QUERIES",
            ),
        )
        rag_queries = rag_queries[:max_rag_queries]
        cli_hints = self._extract_cli_hints(test_cases_text)
        use_graphrag_for_cli = cfg_bool(
            "bug_to_case.rag.use_graphrag_for_cli",
            False,
            env="BUG_TO_CASE_RAG_USE_GRAPHRAG_FOR_CLI",
        )
        use_graphrag_for_product = cfg_bool(
            "bug_to_case.rag.use_graphrag_for_product",
            True,
            env="BUG_TO_CASE_RAG_USE_GRAPHRAG_FOR_PRODUCT",
        )
        use_graphrag_for_similar = cfg_bool(
            "bug_to_case.rag.use_graphrag_for_similar",
            False,
            env="BUG_TO_CASE_RAG_USE_GRAPHRAG_FOR_SIMILAR",
        )

        if cache is None:
            cache = {}

        # 规则走确定性引擎（bug 级缓存）
        rules_cache_key = "review_rules:review"
        review_rules = cache.get(rules_cache_key, "")
        if not review_rules:
            review_rules = self.knowledge_tk.get_review_rules(purpose="review")
            cache[rules_cache_key] = review_rules

        # 图检索 + 向量检索多查询（单路径，避免重复检索放大）
        # main_query 应优先包含 bug 特征词（root_cause/title），否则回退到 module/protocol
        bug_query_seed = ""
        if bug_info:
            bug_query_seed = (bug_info.root_cause or bug_info.title or "").strip()
        if bug_query_seed:
            main_query = f"{bug_query_seed} {protocol_hint}".strip()
        else:
            main_query = f"{module_hint} {protocol_hint} 测试评审".strip()
        rq_sig = "|".join([str(q.get("query") or "").strip() for q in rag_queries[:4]])
        pack_sig = "|".join(
            [
                active_worker_type or "generic",
                active_task_id or "na",
                active_scope[:80] or "scope-na",
            ]
        )
        product_cache_key = f"product:{main_query or test_cases_text[:120]}::{rq_sig[:160]}::{pack_sig}"
        product_knowledge = cache.get(product_cache_key, "")
        if not product_knowledge:
            # 优先走 KnowledgeRouter（包含 HybridKnowledgeFusion）
            if self._knowledge_router and rag_queries:
                router_result = self._knowledge_router.retrieve(
                    query=main_query or test_cases_text[:300],
                    mode="test_review",
                )
                if isinstance(router_result, dict):
                    product_knowledge = str(router_result.get("context") or "").strip()
                elif isinstance(router_result, str):
                    product_knowledge = router_result.strip()
            if not product_knowledge and rag_queries:
                try:
                    unified = self.knowledge_tk._get_unified_rag()
                    rag_timeout = cfg_float(
                        "bug_to_case.rag.timeout_seconds",
                        120.0,
                        env="BUG_TO_CASE_RAG_TIMEOUT_SECONDS",
                    )
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        # Bug-to-case 模式放宽分类过滤，避免产品知识被误过滤
                        whitelist = (
                            []  # 不限分类，依赖 reranker 做相关性控制
                            if bug_info
                            else [
                                "spec/prd",
                                "spec/func_spec",
                                "spec/design",
                            ]
                        )
                        future = executor.submit(
                            unified.retrieve,
                            main_query or test_cases_text[:300],
                            50,
                            8,
                            True,
                            {"rag_queries": rag_queries},
                            None,
                            whitelist,
                        )
                        ctx, _constraints, _ = future.result(timeout=rag_timeout)
                    if ctx:
                        product_knowledge = ctx
                except FutureTimeout as e:
                    raise RuntimeError(
                        f"RAG 检索超时 ({rag_timeout}s)"
                    ) from e
            if not product_knowledge:
                product_knowledge = self.knowledge_tk.search_product_knowledge(
                    query=main_query or test_cases_text[:300],
                    max_results=8,
                    use_graphrag=use_graphrag_for_product,
                )
            relation_query = self._build_relation_query(
                module_hint=module_hint,
                protocol_hint=protocol_hint,
                module_focus_words=module_focus_words,
                bug_info=bug_info,
            )
            relation_focus_sig = "|".join((module_focus_words or [])[:3])
            relation_cache_key = (
                f"feature_rel:{relation_query[:180]}::{relation_focus_sig}::{pack_sig}"
            )
            relation_knowledge = cache.get(relation_cache_key, "")
            if not relation_knowledge and relation_query:
                relation_category_filter = (
                    ""
                    if bug_info
                    else "spec/design,spec/func_spec,spec/prd,cli/reference"
                )
                relation_max_results = 12 if bug_info else 6
                relation_knowledge = self.knowledge_tk.search_product_knowledge(
                    query=relation_query,
                    # bug-to-case 下不过滤分类，避免 root cause/测试输入中的关键术语被排除。
                    category_filter=relation_category_filter,
                    max_results=relation_max_results,
                    use_graphrag=use_graphrag_for_product,
                )
                cache[relation_cache_key] = relation_knowledge
            if relation_knowledge and "(未找到相关知识)" not in relation_knowledge:
                product_knowledge = (
                    (product_knowledge or "").strip()
                    + "\n\n[功能关系视图]\n"
                    + relation_knowledge.strip()
                ).strip()
            cache[product_cache_key] = product_knowledge

            # CLI 功能树子图注入 product_knowledge
            from INAGENT.rag.cli_graph_store import get_cli_graph_store
            cli_graph = get_cli_graph_store()
            tree_hints = module_focus_words[:10] if module_focus_words else []
            if module_hint and module_hint not in tree_hints:
                tree_hints.insert(0, module_hint)
            if tree_hints:
                subgraph = cli_graph.extract_subgraph(tree_hints)
                tree_text = cli_graph.format_for_prompt(subgraph, max_chars=1200)
                if tree_text:
                    product_knowledge = (
                        product_knowledge.strip()
                        + f"\n\n[CLI功能树]\n{tree_text}"
                    ).strip()
                    cache[product_cache_key] = product_knowledge

            # v8 B2: 产品架构背景注入（双引擎 — 多查询融合）
            if self._knowledge_router and not cache.get(f"arch:{module_hint}"):
                arch_queries = [
                    f"产品协议栈架构 L4 L7 设计 {module_hint}",
                    "ustack TCP Faststack 分层",
                ]
                arch_parts: list[str] = []
                for arch_q in arch_queries:
                    r = self._knowledge_router.retrieve(
                        query=arch_q,
                        mode="test_review",
                    )
                    ctx = ""
                    if isinstance(r, dict):
                        ctx = str(r.get("context") or "").strip()
                    elif isinstance(r, str):
                        ctx = r.strip()
                    if ctx and ctx not in arch_parts:
                        arch_parts.append(ctx)
                if arch_parts:
                    arch_text = "\n\n".join(arch_parts)[:1500]
                    product_knowledge = (
                        product_knowledge.strip()
                        + f"\n\n[产品架构背景 - 协议栈分层]\n{arch_text}"
                    ).strip()
                    cache[product_cache_key] = product_knowledge
                cache[f"arch:{module_hint}"] = True

        cli_query_parts = [module_hint, protocol_hint, "CLI 命令语法 参考"]
        # 追加模块特征词到 CLI 查询，提升模块级命令检索命中率
        if module_focus_words:
            cli_query_parts.extend(module_focus_words[:3])
        if cli_hints:
            cli_query_parts.append(cli_hints)
        cli_query = " ".join([p for p in cli_query_parts if p]).strip() or "CLI 命令语法"
        module_focus_sig = "|".join(module_focus_words[:3]) if module_focus_words else ""
        cli_cache_key = f"cli:{protocol_hint or 'generic'}::{module_focus_sig}::{pack_sig}"
        cli_reference = cache.get(cli_cache_key, "")
        if not cli_reference:
            # v8 改进：优先搜索官方CLI文档，然后fallback到产品知识（带标记）
            cli_reference = self.knowledge_tk.search_product_knowledge(
                query=cli_query,
                category_filter="cli/reference",  # ← v8: 优先官方
                max_results=10,
                use_graphrag=use_graphrag_for_cli,
            )

            # fallback: 搜索其他分类但严格标记为非官方
            if not cli_reference.strip():
                other_cli = self.knowledge_tk.search_product_knowledge(
                    query=cli_query,
                    category_filter="",
                    max_results=5,
                    use_graphrag=False,
                )
                if other_cli.strip():
                    # v8: 为非官方参考添加置信度标记
                    cli_reference = f"[产品知识中的相关参考 - 非官方CLI文档]\n{other_cli}"

            cache[cli_cache_key] = cli_reference

        similar_cache_key = f"similar:{main_query or test_cases_text[:120]}::{sheet_name or 'unknown'}::{pack_sig}"
        similar_tests = cache.get(similar_cache_key, "")
        if not similar_tests:
            similar_tests = self.knowledge_tk.search_similar_tests(
                query=main_query or test_cases_text[:200],
                max_results=10,
                use_graphrag=use_graphrag_for_similar,
            )
            cache[similar_cache_key] = similar_tests

        mode = "bug_to_case" if bug_info else "standard"

        hierarchy_prefix = ""
        try:
            from INAGENT.rag.cli_graph_store import get_cli_graph_store
            _cli_graph = get_cli_graph_store()
            hierarchy_prefix = _cli_graph.get_hierarchy_prefix(module_hint)
        except Exception as _e:
            _ib_logger.debug("hierarchy_prefix derivation failed: %s", _e)

        pre_dedup_len = len(product_knowledge)
        product_knowledge = _dedup_context_blocks(product_knowledge)
        if pre_dedup_len > 0:
            saved = pre_dedup_len - len(product_knowledge)
            if saved > 100:
                _ib_logger.info(
                    "[InputBuilder] cross-dedup: %d -> %d chars (saved %d)",
                    pre_dedup_len, len(product_knowledge), saved,
                )

        return ReviewInput(
            mode=mode,
            test_cases_text=test_cases_text,
            review_rules=review_rules,
            product_knowledge=product_knowledge,
            cli_reference=cli_reference,
            similar_tests=similar_tests,
            bug_info=bug_info,
            key_questions=key_questions,
            sheet_role=sheet_role,
            function_hierarchy_prefix=hierarchy_prefix,
        )
