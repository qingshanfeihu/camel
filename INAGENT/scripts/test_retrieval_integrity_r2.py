"""
GraphRAG + 向量库 综合检索完整性测试 (Round 2)

挑选原则（与 test_retrieval_integrity.py 的 23 条查询完全不重复）:
  H. 不同适用范围/权限级别：用户管理(user)、组级(group)、全局(global)
  I. 含 <> 必选参数的命令（多参数）
  J. 含 [] 可选参数的命令
  K. 含 {x|y} 选择参数的命令
  L. no / show / clear 变体命令（不同功能模块）

每条查询同时检查:
  1. GraphRAG 实体是否存在
  2. GraphRAG local_context_build 能否命中关键词
  3. 向量检索（Qdrant）能否返回相关内容
  4. GraphRAG 实体关系链是否存在
"""
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("retrieval_test_r2")
logger.setLevel(logging.INFO)


@dataclass
class TestQuery:
    id: str
    category: str
    query: str
    expected_entities: List[str]
    expected_keywords: List[str]
    vector_keywords: List[str]
    description: str = ""


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
    error: str = ""


QUERIES: List[TestQuery] = [
    # --- H: 不同适用范围/权限级别 ---
    TestQuery(
        id="H1-user-management",
        category="H-scope-level",
        query="user account create password privilege level management",
        expected_entities=["USER"],
        expected_keywords=["用户", "user"],
        vector_keywords=["user", "password"],
        description="用户管理命令（用户级权限概念）",
    ),
    TestQuery(
        id="H2-group-scope-slb",
        category="H-scope-level",
        query="slb group enable disable backend server group",
        expected_entities=["SLB_GROUP"],
        expected_keywords=["slb", "group"],
        vector_keywords=["slb", "group", "enable"],
        description="slb group {enable|disable} <group_name> 组级命令",
    ),
    TestQuery(
        id="H3-aaa-ldap-host",
        category="H-scope-level",
        query="aaa ldap host server name ip port username password timeout",
        expected_entities=["AAA_LDAP_HOST"],
        expected_keywords=["ldap", "host"],
        vector_keywords=["ldap", "host", "ip"],
        description="aaa ldap host 多必选参数全局命令",
    ),

    # --- I: 含 <> 必选参数（多参数） ---
    TestQuery(
        id="I1-ip-address-interface",
        category="I-required-param",
        query="ip address interface configuration netmask prefix overlap",
        expected_entities=["IP_ADDRESS"],
        expected_keywords=["ip", "address"],
        vector_keywords=["ip", "address", "interface"],
        description="ip address <interface_name> <ip_address> {netmask|prefix}",
    ),
    TestQuery(
        id="I2-ip-route-static",
        category="I-required-param",
        query="ip route static destination gateway network next hop",
        expected_entities=["IP_ROUTE_STATIC"],
        expected_keywords=["ip", "route", "static"],
        vector_keywords=["route", "static", "gateway"],
        description="ip route static <dest_ip> {netmask|prefix} <gateway_ip>",
    ),
    TestQuery(
        id="I3-aaa-method-bind",
        category="I-required-param",
        query="aaa method bind virtual service authentication",
        expected_entities=["AAA_METHOD_BIND"],
        expected_keywords=["认证", "绑定"],
        vector_keywords=["aaa", "method", "bind"],
        description="aaa method bind <method_name> <virtual_service>",
    ),

    # --- J: 含 [] 可选参数 ---
    TestQuery(
        id="J1-health-connclose",
        category="J-optional-param",
        query="health connclose fin rst real service connection close",
        expected_entities=["HEALTH_CONNCLOSE"],
        expected_keywords=["健康检查", "关闭"],
        vector_keywords=["health", "connclose"],
        description="health connclose {fin|rst} [real_service]",
    ),
    TestQuery(
        id="J2-slb-tcpoption-wscale",
        category="J-optional-param",
        query="slb tcpoption wscale window scale virtual service shift count",
        expected_entities=["SLB TCPOPTION WSCALE"],
        expected_keywords=["window", "scaling"],
        vector_keywords=["wscale", "shift"],
        description="slb tcpoption wscale <vs> {on|off} [shift_count]",
    ),
    TestQuery(
        id="J3-health-ldap-optional",
        category="J-optional-param",
        query="health ldap real service bind dn password base",
        expected_entities=["HEALTH_LDAP"],
        expected_keywords=["ldap", "健康检查"],
        vector_keywords=["health", "ldap"],
        description="health ldap {real_service|...} [bind_dn] [password]",
    ),

    # --- K: 含 {x|y} 选择参数 ---
    TestQuery(
        id="K1-ha-checkpeer",
        category="K-choice-param",
        query="ha checkpeer on off high availability peer check",
        expected_entities=["HA_CHECKPEER"],
        expected_keywords=["ha", "peer"],
        vector_keywords=["ha", "checkpeer"],
        description="ha checkpeer {on|off}",
    ),
    TestQuery(
        id="K2-ip-dhcp-toggle",
        category="K-choice-param",
        query="ip dhcp on off interface enable disable DHCP client",
        expected_entities=["IP_DHCP"],
        expected_keywords=["dhcp"],
        vector_keywords=["ip", "dhcp"],
        description="ip dhcp {on|off} <interface_name>",
    ),
    TestQuery(
        id="K3-bgp-network-prefix",
        category="K-choice-param",
        query="bgp network ip address netmask prefix announce route",
        expected_entities=["BGP_NETWORK"],
        expected_keywords=["bgp", "network"],
        vector_keywords=["bgp", "network", "netmask"],
        description="bgp network <ip_address> {netmask|prefix}",
    ),

    # --- L: no/show/clear 变体（不同模块） ---
    TestQuery(
        id="L1-show-acl-all",
        category="L-show",
        query="show acl all access control list display configuration",
        expected_entities=["SHOW_ACL_ALL"],
        expected_keywords=["show", "acl"],
        vector_keywords=["acl"],
        description="show acl all — 显示全部ACL配置",
    ),
    TestQuery(
        id="L2-no-ip-route-static",
        category="L-no",
        query="no ip route static delete remove routing entry",
        expected_entities=["NO_IP_ROUTE_STATIC"],
        expected_keywords=["no", "route", "static"],
        vector_keywords=["route", "static"],
        description="no ip route static — 删除静态路由",
    ),
    TestQuery(
        id="L3-clear-ha-all",
        category="L-clear",
        query="clear ha all high availability reset configuration",
        expected_entities=["CLEAR_HA_ALL"],
        expected_keywords=["clear", "ha"],
        vector_keywords=["ha"],
        description="clear ha all — 清除全部HA配置",
    ),
    TestQuery(
        id="L4-show-aaa-all",
        category="L-show",
        query="show aaa all authentication authorization accounting display",
        expected_entities=["SHOW_AAA_ALL"],
        expected_keywords=["显示", "aaa"],
        vector_keywords=["aaa"],
        description="show aaa all — 显示全部AAA配置",
    ),
    TestQuery(
        id="L5-no-health-app",
        category="L-no",
        query="no health app real service remove health check list binding",
        expected_entities=["NO_HEALTH_APP"],
        expected_keywords=["no", "health", "app"],
        vector_keywords=["health", "app"],
        description="no health app {real_service|...} — 移除健康检查绑定",
    ),
    TestQuery(
        id="L6-clear-ddos-blacklist",
        category="L-clear",
        query="clear ddos blacklist DDoS protection remove block list",
        expected_entities=["CLEAR_DDOS_BLACKLIST"],
        expected_keywords=["clear", "ddos", "blacklist"],
        vector_keywords=["ddos", "blacklist"],
        description="clear ddos blacklist — 清除DDoS黑名单",
    ),
]


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


def check_entity_direct(retriever, expected_titles: List[str]) -> tuple:
    found, missed = [], []
    if retriever._entities is None:
        return found, expected_titles
    for title in expected_titles:
        match = retriever._entities[retriever._entities["title"].str.upper() == title.upper()]
        if len(match) > 0:
            found.append(title)
        else:
            missed.append(title)
    return found, missed


def check_relationships(retriever, entity_title: str) -> int:
    if retriever._relationships is None:
        return 0
    rels = retriever._relationships[
        (retriever._relationships["source"].str.upper() == entity_title.upper()) |
        (retriever._relationships["target"].str.upper() == entity_title.upper())
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
    retriever, hybrid_retriever, tq: TestQuery
) -> TestResult:
    result = TestResult(query_id=tq.id)

    found, missed = check_entity_direct(retriever, tq.expected_entities)
    result.entity_found = found
    result.entity_missed = missed
    result.entity_hit = len(missed) == 0

    if tq.expected_entities:
        result.rel_count = check_relationships(retriever, tq.expected_entities[0])

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


async def main():
    import os
    os.chdir(Path(__file__).resolve().parent.parent.parent)

    logger.info("Loading GraphRAG...")
    retriever = load_graphrag()
    ent_count = len(retriever._entities) if retriever._entities is not None else 0
    rel_count = len(retriever._relationships) if retriever._relationships is not None else 0
    logger.info("GraphRAG: %d entities, %d relationships", ent_count, rel_count)

    logger.info("Loading vector store (Qdrant)...")
    hybrid_retriever = load_vector_rag()
    logger.info("Vector store ready")

    results: List[TestResult] = []
    for tq in QUERIES:
        logger.info("Testing %s: %s", tq.id, tq.description)
        tr = await run_single_test(retriever, hybrid_retriever, tq)
        results.append(tr)

        g_status = "OK" if (tr.entity_hit and tr.keyword_hit) else "FAIL"
        v_status = "OK" if tr.vector_hit else "FAIL"
        logger.info(
            "  [G:%s V:%s] entity=%s graphkw=%s vectorkw=%s ctx=%d vec=%d rels=%d %s",
            g_status, v_status,
            "OK" if tr.entity_hit else f"MISS:{tr.entity_missed}",
            "OK" if tr.keyword_hit else f"MISS:{tr.keyword_missed}",
            "OK" if tr.vector_hit else f"MISS:{tr.vector_missed}",
            tr.context_snippets, tr.vector_snippets, tr.rel_count,
            f"ERR:{tr.error}" if tr.error else "",
        )

    print("\n" + "=" * 80)
    print("RETRIEVAL TEST SUMMARY (Round 2: GraphRAG + Vector)")
    print("=" * 80)

    total = len(results)
    full_pass = sum(1 for r in results if r.entity_hit and r.keyword_hit and r.vector_hit)
    graph_pass = sum(1 for r in results if r.entity_hit and r.keyword_hit)
    vector_pass = sum(1 for r in results if r.vector_hit)
    entity_pass = sum(1 for r in results if r.entity_hit)

    print(f"\nTotal: {total}")
    print(f"Full Pass (Graph+Vector): {full_pass}/{total} ({100*full_pass/total:.0f}%)")
    print(f"GraphRAG Pass: {graph_pass}/{total} ({100*graph_pass/total:.0f}%)")
    print(f"Vector Pass: {vector_pass}/{total} ({100*vector_pass/total:.0f}%)")
    print(f"Entity Exist: {entity_pass}/{total} ({100*entity_pass/total:.0f}%)")

    categories: Dict[str, Dict[str, Any]] = {}
    for tq, tr in zip(QUERIES, results):
        cat = tq.category
        if cat not in categories:
            categories[cat] = {"total": 0, "graph": 0, "vector": 0, "full": 0}
        categories[cat]["total"] += 1
        if tr.entity_hit and tr.keyword_hit:
            categories[cat]["graph"] += 1
        if tr.vector_hit:
            categories[cat]["vector"] += 1
        if tr.entity_hit and tr.keyword_hit and tr.vector_hit:
            categories[cat]["full"] += 1

    print("\nBy Category:")
    print(f"  {'Category':<20} {'Graph':>8} {'Vector':>8} {'Full':>8}")
    print(f"  {'-'*20} {'-'*8} {'-'*8} {'-'*8}")
    for cat, stats in sorted(categories.items()):
        t = stats["total"]
        print(f"  {cat:<20} {stats['graph']}/{t:>5} {stats['vector']}/{t:>5} {stats['full']}/{t:>5}")

    failures = [
        (tq, tr) for tq, tr in zip(QUERIES, results)
        if not (tr.entity_hit and tr.keyword_hit and tr.vector_hit)
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
            if tr.error:
                parts.append(f"error={tr.error}")
            print(f"  {tq.id}: {'; '.join(parts)}")

    output_path = Path("INAGENT/graphrag_index/logs/retrieval_test_r2_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    detail = []
    for tq, tr in zip(QUERIES, results):
        detail.append({
            "id": tq.id, "category": tq.category, "query": tq.query,
            "description": tq.description,
            "entity_hit": tr.entity_hit, "entity_found": tr.entity_found,
            "entity_missed": tr.entity_missed,
            "keyword_hit": tr.keyword_hit, "keyword_found": tr.keyword_found,
            "keyword_missed": tr.keyword_missed,
            "vector_hit": tr.vector_hit, "vector_found": tr.vector_found,
            "vector_missed": tr.vector_missed,
            "context_snippets": tr.context_snippets,
            "vector_snippets": tr.vector_snippets,
            "rel_count": tr.rel_count,
            "error": tr.error,
            "full_pass": tr.entity_hit and tr.keyword_hit and tr.vector_hit,
        })
    output_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Results saved to %s", output_path)

    return full_pass == total


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
