"""
GraphRAG + 向量库 + CLI 树只读校验 — Round 4

数据来源：知识库 JSON 由 CLI 文档/CLI.pdf 管线生成（仓库内无 PDF 时以
`knowledge_base/reference/knowledge_base.json` 为准）。

与 R1/R2/R3 及 `_spot_check_indexes.py` 的抽检命令集不重复（见 RESERVED_ENTITY_TITLES）。

覆盖：
  S. 用户级 / 权限级 / 配置级（各 1）
  N. 含 <> 必选参数（3）
  O. 含 [] 可选参数（3，含 restapi on [protocol] [port]）
  P. 含 {x|y} 必选选择（3）
  Q. 含 [x|y|…] 可选选择（3）
  R. no / show / clear（各 2，共 6；合计 21 条）

行为保证：
  - 只读：不写入 Qdrant / GraphRAG / Neo4j / cli_keyword_graph.json
  - 向量检索走现有 HybridRetriever.query（只读）
  - CLI 树：`CLIGraphStore.command_exists()` 只读内存图
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("retrieval_test_r4")
logger.setLevel(logging.INFO)

# 与 test_retrieval_integrity.py / r2 / r3 已用过的 expected_entities 并集（避免重复测同一实体）
RESERVED_ENTITY_TITLES: FrozenSet[str] = frozenset({
    "SYSTEM_SYSTEM_DUMP", "BGP_NEIGHBOR", "ADMIN_AAA_METHOD", "SSL_CSR", "SYSTEM_SYSTEM_TIME",
    "SYSTEM_SYSTEM_LICENSE", "SSL_ACTIVATE_CERTIFICATE", "HEALTH_INTERVAL", "SSL_HOST",
    "SYSTEM_SYSTEM_TUNE_TCP_SLOWSTART", "SYSTEM_SYSTEM_THREAD", "SHOW_SLB_GROUP_HEALTH",
    "NO_SNMP_COMMUNITY", "CLEAR_HEALTH_CHECKER", "NO_ACCESSLIST_DENY_TCP", "SHOW_NAT_PORT",
    "CLEAR_IP_ARP", "SNMP_HOST", "NAT_PORTDST", "HEALTH_APP", "SLB_MODE_IRCOOKIE",
    "SLB_GROUP_METHOD_IC", "USER", "SLB_GROUP", "AAA_LDAP_HOST", "IP_ADDRESS", "IP_ROUTE_STATIC",
    "AAA_METHOD_BIND", "HEALTH_CONNCLOSE", "SLB TCPOPTION WSCALE", "HEALTH_LDAP", "HA_CHECKPEER",
    "IP_DHCP", "BGP_NETWORK", "SHOW_ACL_ALL", "NO_IP_ROUTE_STATIC", "CLEAR_HA_ALL", "SHOW_AAA_ALL",
    "NO_HEALTH_APP", "CLEAR_DDOS_BLACKLIST", "AAA_LDAP_DEFAULTGROUP", "HA_LOG",
    "SLB_PERSISTENCE_TIMEOUT", "AAA_LDAP_SEARCHFILTER", "AAA_OAUTH_JWKSURL", "AAA_LDAP_BIND_STATIC",
    "AAA_ROLE_NAME", "RESTAPI_SOURCE", "AAA_SAMLSP_SP_ACS", "HA_CONSISTENCY", "AAA_SSO", "ACL_RULE",
    "IP_RTS_ON", "BOND_INTERFACE", "SHOW_AAA_METHOD_BIND", "SHOW_SLB_GROUP", "NO_AAA_LDAP_HOST",
    "CLEAR_SLB_GROUP_MEMBER", "CLEAR_HA_GROUP_PORT",
})

GRAPHRAG_BAD_TYPES = frozenset({
    "STEP_TYPE", "TEST_CASE", "BUSINESS_STATE", "STATE_TRANSITION", "SCENARIO", "REQUIREMENT",
    "DESIGN_KNOWLEDGE", "CONFIG_EXAMPLE", "TEST_STANDARD", "ERROR_CODE_TRIGGER",
})


@dataclass
class TestQuery:
    id: str
    category: str
    query: str
    expected_entities: List[str]
    expected_keywords: List[str]
    vector_keywords: List[str]
    description: str = ""
    tree_cmd: str = ""  # 供 CLIGraphStore.command_exists 只读校验（与 [命令] 行一致为佳）


@dataclass
class TestResult:
    query_id: str
    entity_hit: bool = False
    entity_found: List[str] = field(default_factory=list)
    entity_missed: List[str] = field(default_factory=list)
    keyword_hit: bool = False
    keyword_found: List[str] = field(default_factory=list)
    keyword_missed: List[str] = field(default_factory=list)
    vector_hit: bool = False
    vector_found: List[str] = field(default_factory=list)
    vector_missed: List[str] = field(default_factory=list)
    context_snippets: int = 0
    vector_snippets: int = 0
    rel_count: int = 0
    tree_ok: bool = False
    tree_similar: List[str] = field(default_factory=list)
    error: str = ""


QUERIES: List[TestQuery] = [
    # --- S: 用户级 / 权限级 / 配置级 ---
    TestQuery(
        id="S1-user-show-vlan",
        category="S-scope-level",
        query="show vlan interface display 802.1q tagging",
        expected_entities=["SHOW_VLAN"],
        expected_keywords=["vlan", "显示"],
        vector_keywords=["vlan", "show"],
        description="用户级查看：show vlan（读状态）",
        tree_cmd="show vlan",
    ),
    TestQuery(
        id="S2-privilege-aaa-method-rank",
        category="S-scope-level",
        query="aaa method rank authentication order priority administrator",
        expected_entities=["AAA_METHOD_RANK"],
        expected_keywords=["方法", "优先级"],
        vector_keywords=["aaa", "method", "rank"],
        description="权限/认证平面：aaa method rank",
        tree_cmd="aaa method rank",
    ),
    TestQuery(
        id="S3-config-nat-static",
        category="S-scope-level",
        query="nat static translation inside outside global local address mapping",
        expected_entities=["NAT_STATIC"],
        expected_keywords=["静态", "nat"],
        vector_keywords=["nat", "static"],
        description="配置级：NAT 静态映射",
        tree_cmd="nat static",
    ),
    # --- N: <> 必选 ---
    TestQuery(
        id="N1-restapi-ssl-import-cert",
        category="N-required-param",
        query="restapi ssl import certificate pem file path passphrase",
        expected_entities=["RESTAPI_SSL_IMPORT_CERTIFICATE"],
        expected_keywords=["restapi", "证书"],
        vector_keywords=["restapi", "ssl", "import"],
        description="restapi ssl import certificate <cert_file> <passphrase>",
        tree_cmd="restapi ssl import certificate",
    ),
    TestQuery(
        id="N2-bond-interface-add",
        category="N-required-param",
        query="bond interface add link aggregation member port",
        expected_entities=["BOND_INTERFACE_ADD"],
        expected_keywords=["bond", "接口"],
        vector_keywords=["bond", "interface", "add"],
        description="bond interface add <bond_name> <interface_name>",
        tree_cmd="bond interface",
    ),
    TestQuery(
        id="N3-bridge-vlan-add",
        category="N-required-param",
        query="bridge vlan add bridge domain 802.1q vlan id",
        expected_entities=["BRIDGE_VLAN_ADD"],
        expected_keywords=["bridge", "vlan"],
        vector_keywords=["bridge", "vlan", "add"],
        description="bridge vlan add <bridge_name> <vlan_list>",
        tree_cmd="bridge vlan",
    ),
    # --- O: [] 可选 ---
    TestQuery(
        id="O1-restapi-on-optional-protocol-port",
        category="O-optional-param",
        query="restapi on https http protocol port enable web service",
        expected_entities=["RESTAPI_ON"],
        expected_keywords=["restapi", "on"],
        vector_keywords=["restapi", "on", "https"],
        description="restapi on [protocol] [port]",
        tree_cmd="restapi on",
    ),
    TestQuery(
        id="O2-show-slb-directfwd-optional-vs",
        category="O-optional-param",
        query="show slb directfwd virtual service fasttcp forwarding mode",
        expected_entities=["SHOW SLB DIRECTFWD"],
        expected_keywords=["directfwd", "slb"],
        vector_keywords=["slb", "directfwd", "show"],
        description="show slb directfwd [virtual_service]",
        tree_cmd="show slb directfwd",
    ),
    TestQuery(
        id="O3-log-alert-optional-data-count",
        category="O-optional-param",
        query="log alert id expression email interval syslog notification",
        expected_entities=["LOG_ALERT"],
        expected_keywords=["log", "alert"],
        vector_keywords=["log", "alert", "email"],
        description="log alert … [data|count]",
        tree_cmd="log alert",
    ),
    # --- P: {x|y} ---
    TestQuery(
        id="P1-nat-static-enable-toggle",
        category="P-choice-param",
        query="nat static enable disable translation activate",
        expected_entities=["NAT_STATIC_ENABLE"],
        expected_keywords=["nat", "static"],
        vector_keywords=["nat", "static", "enable"],
        description="nat static enable {on|off}",
        tree_cmd="nat static",
    ),
    TestQuery(
        id="P2-slb-directfwd-on-off",
        category="P-choice-param",
        query="slb directfwd on off virtual service fasttcp",
        expected_entities=["SLB DIRECTFWD"],
        expected_keywords=["directfwd", "slb"],
        vector_keywords=["slb", "directfwd"],
        description="slb directfwd {on|off} [virtual_service]",
        tree_cmd="slb directfwd",
    ),
    TestQuery(
        id="P3-health-proxyip-on-off",
        category="P-choice-param",
        query="health proxyip on off checker real service",
        expected_entities=["HEALTH_PROXYIP"],
        expected_keywords=["proxyip", "health"],
        vector_keywords=["health", "proxyip"],
        description="health proxyip {on|off}",
        tree_cmd="health proxyip",
    ),
    # --- Q: [x|y|…] 可选多选一或不选 ---
    TestQuery(
        id="Q1-show-ha-status-optional-scope",
        category="Q-optional-choice",
        query="show ha status group domain link login high availability",
        expected_entities=["SHOW_HA_STATUS"],
        expected_keywords=["ha", "status"],
        vector_keywords=["ha", "status", "show"],
        description="show ha status [group|domain|link|login]",
        tree_cmd="show ha status",
    ),
    TestQuery(
        id="Q2-show-bridge-mactable-filter",
        category="Q-optional-choice",
        query="show bridge mactable mac address learning filter",
        expected_entities=["SHOW_BRIDGE_MACTABLE"],
        expected_keywords=["bridge", "mactable"],
        vector_keywords=["bridge", "mactable", "show"],
        description="show bridge mactable [bridge_name|filter_string]",
        tree_cmd="show bridge mactable",
    ),
    TestQuery(
        id="Q3-show-ip-eroute-family-state",
        category="Q-optional-choice",
        query="show ip eroute ipv4 ipv6 valid invalid routing table",
        expected_entities=["SHOW_IP_EROUTE"],
        expected_keywords=["eroute", "路由"],
        vector_keywords=["eroute", "ip", "show"],
        description="show ip eroute [ipv4|ipv6] [all|valid|invalid]",
        tree_cmd="show ip eroute",
    ),
    # --- R: show / no / clear（与 R2/R3 命令实体不重复）---
    TestQuery(
        id="R1-show-aaa-method-rank",
        category="R-show",
        query="show aaa method rank display authentication order",
        expected_entities=["SHOW_AAA_METHOD_RANK"],
        expected_keywords=["显示", "method"],
        vector_keywords=["aaa", "method", "rank"],
        description="show aaa method rank",
        tree_cmd="show aaa method rank",
    ),
    TestQuery(
        id="R2-show-nat-static",
        category="R-show",
        query="show nat static configuration translation mapping",
        expected_entities=["SHOW_NAT_STATIC"],
        expected_keywords=["nat", "static"],
        vector_keywords=["nat", "static", "show"],
        description="show nat static",
        tree_cmd="show nat static",
    ),
    TestQuery(
        id="R3-no-nat-static",
        category="R-no",
        query="no nat static remove delete translation rule",
        expected_entities=["NO_NAT_STATIC"],
        expected_keywords=["no", "nat"],
        vector_keywords=["no", "nat", "static"],
        description="no nat static",
        tree_cmd="no nat static",
    ),
    TestQuery(
        id="R4-no-vlan",
        category="R-no",
        query="no vlan remove 802.1q bridge interface",
        expected_entities=["NO_VLAN"],
        expected_keywords=["no", "vlan"],
        vector_keywords=["no", "vlan"],
        description="no vlan",
        tree_cmd="no vlan",
    ),
    TestQuery(
        id="R5-clear-slb-virtual-http",
        category="R-clear",
        query="clear slb virtual http statistics reset layer7",
        expected_entities=["CLEAR_SLB_VIRTUAL_HTTP"],
        expected_keywords=["clear", "virtual"],
        vector_keywords=["clear", "slb", "virtual"],
        description="clear slb virtual http",
        tree_cmd="clear slb virtual http",
    ),
    TestQuery(
        id="R6-clear-ip-rts",
        category="R-clear",
        query="clear ip rts route source reset",
        expected_entities=["CLEAR_IP_RTS"],
        expected_keywords=["清除", "rts"],
        vector_keywords=["clear", "ip", "rts"],
        description="clear ip rts",
        tree_cmd="clear ip rts",
    ),
]


def _assert_queries_disjoint_from_reserved() -> None:
    for tq in QUERIES:
        for ent in tq.expected_entities:
            if ent.upper() in RESERVED_ENTITY_TITLES:
                raise RuntimeError(f"Round4 query {tq.id} reuses reserved entity {ent!r}")


def check_data_purity(
    *,
    qdrant_path: Optional[Path] = None,
    graphrag_out: Optional[Path] = None,
) -> Tuple[bool, List[str]]:
    """只读检查向量库分类与 GraphRAG 实体类型是否异常。"""
    notes: List[str] = []
    ok = True
    inagent = Path(__file__).resolve().parent.parent
    out = graphrag_out or (inagent / "graphrag_index" / "output")
    qpath = qdrant_path or (Path.home() / "AppData" / "Local" / "INAGENT" / "vector_store" / "qdrant")

    # Qdrant：期望仅 cli/reference（与 _spot_check_indexes 一致）
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        notes.append("qdrant_client 未安装，跳过 Qdrant 污染检查")
        ok = False
    else:
        if not qpath.exists():
            notes.append(f"Qdrant 路径不存在: {qpath}")
            ok = False
        else:
            client = QdrantClient(path=str(qpath))
            try:
                pts, _ = client.scroll("workflow_rag", limit=5000, with_payload=True, with_vectors=False)
            except Exception as e:
                notes.append(f"Qdrant scroll 失败: {e}")
                ok = False
            else:
                cats: Dict[str, int] = {}
                for pt in pts:
                    meta = (pt.payload or {}).get("metadata") or {}
                    reg = meta.get("regex_metadata") or {}
                    cat = reg.get("document_category", "?")
                    cats[cat] = cats.get(cat, 0) + 1
                notes.append(f"Qdrant document_category 分布: {cats}")
                if set(cats.keys()) - {"cli/reference"}:
                    notes.append("警告: Qdrant 存在非 cli/reference 分类，可能混入其它文档")
                    ok = False
                if sum(cats.values()) == 0:
                    notes.append("警告: Qdrant 无点或未读到 payload")
                    ok = False
            finally:
                client.close()

    ent_file = out / "entities.parquet"
    if not ent_file.exists():
        notes.append(f"GraphRAG entities 缺失: {ent_file}")
        ok = False
    else:
        df = pd.read_parquet(ent_file)
        types = set(df["type"].astype(str).str.strip('"').str.upper().unique())
        dirty = GRAPHRAG_BAD_TYPES & types
        if dirty:
            notes.append(f"GraphRAG 发现历史类型污染: {dirty}")
            ok = False
        else:
            notes.append(f"GraphRAG 实体类型未命中已知污染集合，类型数={len(types)}")

    return ok, notes


def load_graphrag():
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever

    workspace = Path("INAGENT/graphrag_index")
    retriever = GraphRAGRetriever(workspace_dir=workspace)
    if not retriever.is_available():
        raise RuntimeError("GraphRAG not available")
    return retriever


def load_vector_rag():
    from INAGENT.web.deps import get_rag

    hybrid_retriever, _reranker = get_rag()
    return hybrid_retriever


def load_cli_graph():
    from INAGENT.rag.cli_graph_store import CLIGraphStore

    return CLIGraphStore()


def check_entity_direct(retriever, expected_titles: List[str]) -> tuple:
    found, missed = [], []
    if retriever._entities is None:
        return found, expected_titles
    col = retriever._entities["title"]
    for title in expected_titles:
        match = retriever._entities[col.astype(str).str.upper() == title.upper()]
        if len(match) > 0:
            found.append(title)
        else:
            missed.append(title)
    return found, missed


def check_relationships(retriever, entity_title: str) -> int:
    if retriever._relationships is None:
        return 0
    rels = retriever._relationships[
        (retriever._relationships["source"].str.upper() == entity_title.upper())
        | (retriever._relationships["target"].str.upper() == entity_title.upper())
    ]
    return len(rels)


def check_vector(hybrid_retriever, query: str, keywords: List[str]) -> tuple:
    try:
        result = hybrid_retriever.query(query, top_k=20, return_detailed_info=True)
        retrieved = result.get("Retrieved Context", [])
        all_text = ""
        for doc in retrieved:
            if isinstance(doc, dict):
                text = doc.get("text") or doc.get("page_content") or doc.get("content") or ""
            else:
                text = str(doc)
            all_text += " " + text.lower()

        found, missed = [], []
        for kw in keywords:
            if kw.lower() in all_text:
                found.append(kw)
            else:
                missed.append(kw)
        return found, missed, len(retrieved)
    except Exception as e:
        logger.error("Vector search failed: %s", e)
        return [], keywords, 0


async def run_single_test(
    retriever,
    hybrid_retriever,
    cli_store: Any,
    tq: TestQuery,
) -> TestResult:
    result = TestResult(query_id=tq.id)

    found, missed = check_entity_direct(retriever, tq.expected_entities)
    result.entity_found = found
    result.entity_missed = missed
    result.entity_hit = len(missed) == 0

    if tq.expected_entities:
        result.rel_count = check_relationships(retriever, tq.expected_entities[0])

    if tq.tree_cmd:
        exists, similar = cli_store.command_exists(tq.tree_cmd)
        result.tree_ok = exists
        result.tree_similar = similar[:5]

    try:
        context_results = await retriever.local_context_build(query=tq.query, top_k=15)
        result.context_snippets = len(context_results)
        all_text = " ".join(r.text.lower() for r in context_results)
        for kw in tq.expected_keywords:
            if kw.lower() in all_text:
                result.keyword_found.append(kw)
            else:
                result.keyword_missed.append(kw)
        result.keyword_hit = len(result.keyword_missed) == 0
    except Exception as e:
        result.error = str(e)

    vf, vm, vc = check_vector(hybrid_retriever, tq.query, tq.vector_keywords)
    result.vector_found = vf
    result.vector_missed = vm
    result.vector_snippets = vc
    result.vector_hit = len(vm) == 0

    return result


async def main() -> Tuple[int, int]:
    import os

    os.chdir(Path(__file__).resolve().parent.parent.parent)
    _assert_queries_disjoint_from_reserved()

    print("=" * 80)
    print("Round 4 — 数据洁净性 + 检索 + CLI 树只读校验（不写入索引与树文件）")
    print("=" * 80)

    pure_ok, purity_notes = check_data_purity()
    print("\n[洁净性] 向量库分类 + GraphRAG 实体类型")
    for line in purity_notes:
        print(f"  {line}")
    print(f"  结论: {'通过' if pure_ok else '未通过（见上）'}")

    print("\n[加载] GraphRAG / 向量 RAG / CLI 图（只读）…")
    retriever = load_graphrag()
    ent_n = len(retriever._entities) if retriever._entities is not None else 0
    rel_n = len(retriever._relationships) if retriever._relationships is not None else 0
    print(f"  GraphRAG 实体={ent_n}, 关系={rel_n}")

    hybrid_retriever = load_vector_rag()
    try:
        vr = getattr(hybrid_retriever, "vr", None)
        storage = getattr(vr, "storage", None) if vr else None
        if storage:
            client = getattr(storage, "_client", None)
            coll = getattr(storage, "collection_name", "workflow_rag")
            if client:
                info = client.get_collection(coll)
                print(f"  Qdrant: collection={coll}, points={info.points_count}")
    except Exception as e:
        print(f"  Qdrant 状态: {e}")

    cli_store = load_cli_graph()

    print(f"\n[执行] {len(QUERIES)} 条查询…")
    results: List[TestResult] = []
    for tq in QUERIES:
        logger.info("Testing %s: %s", tq.id, tq.description)
        tr = await run_single_test(retriever, hybrid_retriever, cli_store, tq)
        results.append(tr)
        g_ok = tr.entity_hit and tr.keyword_hit
        v_ok = tr.vector_hit
        t_ok = tr.tree_ok if tq.tree_cmd else True
        logger.info(
            "  [G:%s V:%s T:%s] entity=%s kw=%s vkw=%s tree=%s ctx=%d vec=%d rels=%d%s",
            "OK" if g_ok else "FAIL",
            "OK" if v_ok else "FAIL",
            "OK" if t_ok else "FAIL",
            "OK" if tr.entity_hit else f"MISS:{tr.entity_missed}",
            "OK" if tr.keyword_hit else f"MISS:{tr.keyword_missed}",
            "OK" if tr.vector_hit else f"MISS:{tr.vector_missed}",
            tr.tree_ok if tq.tree_cmd else "—",
            tr.context_snippets,
            tr.vector_snippets,
            tr.rel_count,
            f" ERR:{tr.error}" if tr.error else "",
        )

    total = len(results)
    tree_cmds = sum(1 for tq in QUERIES if tq.tree_cmd)
    tree_pass = sum(1 for tq, tr in zip(QUERIES, results) if (not tq.tree_cmd or tr.tree_ok))
    full_pass = sum(
        1
        for tq, tr in zip(QUERIES, results)
        if tr.entity_hit and tr.keyword_hit and tr.vector_hit and (not tq.tree_cmd or tr.tree_ok)
    )
    graph_pass = sum(1 for r in results if r.entity_hit and r.keyword_hit)
    vector_pass = sum(1 for r in results if r.vector_hit)

    print("\n" + "=" * 80)
    print("RETRIEVAL TEST SUMMARY (Round 4)")
    print("=" * 80)
    print(f"数据洁净性: {'OK' if pure_ok else 'FAIL'}")
    print(f"Full Pass (Graph+Vector+Tree): {full_pass}/{total}")
    print(f"GraphRAG Pass:                 {graph_pass}/{total}")
    print(f"Vector Pass:                   {vector_pass}/{total}")
    print(f"CLI tree command_exists:       {tree_pass}/{tree_cmds}（有 tree_cmd 的项）")

    categories: Dict[str, Dict[str, Any]] = {}
    for tq, tr in zip(QUERIES, results):
        cat = tq.category
        if cat not in categories:
            categories[cat] = {"total": 0, "full": 0}
        categories[cat]["total"] += 1
        if tr.entity_hit and tr.keyword_hit and tr.vector_hit and (not tq.tree_cmd or tr.tree_ok):
            categories[cat]["full"] += 1

    print("\nBy Category (full pass):")
    for cat, stats in sorted(categories.items()):
        print(f"  {cat:<22} {stats['full']}/{stats['total']}")

    failures = [
        (tq, tr)
        for tq, tr in zip(QUERIES, results)
        if not (tr.entity_hit and tr.keyword_hit and tr.vector_hit and (not tq.tree_cmd or tr.tree_ok))
    ]
    if failures:
        print(f"\nFailures ({len(failures)}):")
        for tq, tr in failures:
            parts = []
            if tr.entity_missed:
                parts.append(f"entity_miss={tr.entity_missed}")
            if tr.keyword_missed:
                parts.append(f"graphkw_miss={tr.keyword_missed}")
            if tr.vector_missed:
                parts.append(f"vectorkw_miss={tr.vector_missed}")
            if tq.tree_cmd and not tr.tree_ok:
                parts.append(f"tree_miss similar={tr.tree_similar}")
            if tr.error:
                parts.append(f"error={tr.error}")
            print(f"  {tq.id}: {'; '.join(parts)}")

    log_dir = Path("INAGENT/graphrag_index/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / "retrieval_test_r4_results.json"
    detail = []
    for tq, tr in zip(QUERIES, results):
        detail.append({
            "id": tq.id,
            "category": tq.category,
            "query": tq.query,
            "description": tq.description,
            "tree_cmd": tq.tree_cmd,
            "entity_hit": tr.entity_hit,
            "keyword_hit": tr.keyword_hit,
            "vector_hit": tr.vector_hit,
            "tree_ok": tr.tree_ok,
            "tree_similar": tr.tree_similar,
            "entity_missed": tr.entity_missed,
            "keyword_missed": tr.keyword_missed,
            "vector_missed": tr.vector_missed,
            "context_snippets": tr.context_snippets,
            "vector_snippets": tr.vector_snippets,
            "rel_count": tr.rel_count,
            "error": tr.error,
            "data_purity_ok": pure_ok,
            "purity_notes": purity_notes,
        })
    out_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Results saved to %s", out_path)

    return full_pass, total


if __name__ == "__main__":
    _assert_queries_disjoint_from_reserved()
    full_pass, total = asyncio.run(main())
    sys.exit(0 if full_pass == total else 1)
