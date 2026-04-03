"""
GraphRAG + 向量库 综合检索完整性测试 (Round 3)

挑选原则（与 R1/R2 全部不重复）:
  M. 不同配置/权限级别：组级(group)、HA管理、SLB持久化
  N. 含 <> 必选多参数命令（3条）
  O. 含 [] 可选参数命令（3条）
  P. 含 {x|y} 必选选择命令（3条）
  Q. 含 [x|y] 可选选择命令（3条）
  R. no / show / clear 变体（各2条，共6条）

每条查询同时检查:
  1. GraphRAG 实体是否存在
  2. GraphRAG local_context_build 能否命中关键词
  3. 向量检索（Qdrant）能否返回相关内容
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
logger = logging.getLogger("retrieval_test_r3")
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
    # --- M: 不同配置/权限级别 ---
    TestQuery(
        id="M1-group-scope-aaa-ldap-defaultgroup",
        category="M-privilege-level",
        query="aaa ldap defaultgroup group scope ldap server default group",
        expected_entities=["AAA_LDAP_DEFAULTGROUP"],
        expected_keywords=["ldap", "默认"],
        vector_keywords=["ldap", "defaultgroup", "group"],
        description="aaa ldap defaultgroup <ldap_server_name> <group> — 组级命令",
    ),
    TestQuery(
        id="M2-ha-log-admin",
        category="M-privilege-level",
        query="ha log on off high availability logging enable disable",
        expected_entities=["HA_LOG"],
        expected_keywords=["日志", "ha"],
        vector_keywords=["ha", "log"],
        description="ha log {on|off} — HA 管理员级配置",
    ),
    TestQuery(
        id="M3-slb-persistence-timeout",
        category="M-privilege-level",
        query="slb persistence timeout minutes group session idle duration",
        expected_entities=["SLB_PERSISTENCE_TIMEOUT"],
        expected_keywords=["持久化", "timeout"],
        vector_keywords=["slb", "persistence", "timeout"],
        description="slb persistence timeout <timeout_minutes> [group_name] [idle|duration]",
    ),

    # --- N: 含 <> 必选多参数 ---
    TestQuery(
        id="N1-aaa-ldap-searchfilter",
        category="N-required-param",
        query="aaa ldap searchfilter server filter string attribute search",
        expected_entities=["AAA_LDAP_SEARCHFILTER"],
        expected_keywords=["ldap", "过滤"],
        vector_keywords=["ldap", "searchfilter", "filter"],
        description="aaa ldap searchfilter <ldap_server_name> <filter_string>",
    ),
    TestQuery(
        id="N2-aaa-oauth-jwksurl",
        category="N-required-param",
        query="aaa oauth jwks url server name json web key set endpoint",
        expected_entities=["AAA_OAUTH_JWKSURL"],
        expected_keywords=["oauth", "jwks"],
        vector_keywords=["oauth", "jwks", "url"],
        description="aaa oauth jwksurl <server_name> <jwks_url>",
    ),
    TestQuery(
        id="N3-aaa-ldap-bind-static",
        category="N-required-param",
        query="aaa ldap bind static dn prefix suffix server name",
        expected_entities=["AAA_LDAP_BIND_STATIC"],
        expected_keywords=["ldap", "bind"],
        vector_keywords=["ldap", "bind", "static", "dn"],
        description="aaa ldap bind static <ldap_server_name> <dn_prefix> <dn_suffix>",
    ),

    # --- O: 含 [] 可选参数 ---
    TestQuery(
        id="O1-aaa-role-name",
        category="O-optional-param",
        query="aaa role name description optional create define",
        expected_entities=["AAA_ROLE_NAME"],
        expected_keywords=["角色", "role"],
        vector_keywords=["aaa", "role", "name"],
        description="aaa role name <role_name> [description]",
    ),
    TestQuery(
        id="O2-restapi-source",
        category="O-optional-param",
        query="restapi source ip address netmask prefix access control optional action",
        expected_entities=["RESTAPI_SOURCE"],
        expected_keywords=["restapi", "source"],
        vector_keywords=["restapi", "source", "ip"],
        description="restapi source <source_ip> <netmask|prefix> [action]",
    ),
    TestQuery(
        id="O3-aaa-samlsp-sp-acs",
        category="O-optional-param",
        query="aaa samlsp sp acs service provider assertion consumer service type",
        expected_entities=["AAA_SAMLSP_SP_ACS"],
        expected_keywords=["saml", "认证"],
        vector_keywords=["samlsp", "acs", "sp"],
        description="aaa samlsp sp acs <server_name> [type]",
    ),

    # --- P: 含 {x|y} 必选选择参数 ---
    TestQuery(
        id="P1-ha-consistency",
        category="P-choice-param",
        query="ha consistency on off enable disable cluster synchronization",
        expected_entities=["HA_CONSISTENCY"],
        expected_keywords=["一致性", "ha"],
        vector_keywords=["ha", "consistency"],
        description="ha consistency {on|off}",
    ),
    TestQuery(
        id="P2-aaa-sso",
        category="P-choice-param",
        query="aaa sso single sign-on on off virtual service enable",
        expected_entities=["AAA_SSO"],
        expected_keywords=["单点登录", "sso"],
        vector_keywords=["aaa", "sso", "virtual"],
        description="aaa sso {on|off} <virtual_service>",
    ),
    TestQuery(
        id="P3-acl-rule",
        category="P-choice-param",
        query="acl rule client ip group netmask prefix access control list policy",
        expected_entities=["ACL_RULE"],
        expected_keywords=["访问控制", "acl"],
        vector_keywords=["acl", "rule", "ip"],
        description="acl rule <rule_name> <client_ip|ip_group_name> {netmask|prefix}",
    ),

    # --- Q: 含 [x|y] 可选选择参数 ---
    TestQuery(
        id="Q1-ip-rts-on",
        category="Q-optional-choice",
        query="ip rts on all gateway route source optional selection",
        expected_entities=["IP_RTS_ON"],
        expected_keywords=["路由", "ip"],
        vector_keywords=["ip", "rts"],
        description="ip rts on [all|gateway]",
    ),
    TestQuery(
        id="Q2-bond-interface",
        category="Q-optional-choice",
        query="bond interface name link aggregation optional mode 1 0",
        expected_entities=["BOND_INTERFACE"],
        expected_keywords=["bond", "接口"],
        vector_keywords=["bond", "interface"],
        description="bond interface <bond_name> <interface_name> [1|0]",
    ),
    TestQuery(
        id="Q3-health-interval",
        category="Q-optional-choice",
        query="health interval timeout server real service add hc check timing",
        expected_entities=["HEALTH_INTERVAL"],
        expected_keywords=["健康检查", "间隔"],
        vector_keywords=["health", "interval"],
        description="health interval <interval> [server_timeout] [real_service|add_hc_name|...]",
    ),

    # --- R: no / show / clear 变体 ---
    TestQuery(
        id="R1-show-aaa-method-bind",
        category="R-show",
        query="show aaa method bind display authentication method binding",
        expected_entities=["SHOW_AAA_METHOD_BIND"],
        expected_keywords=["显示", "绑定"],
        vector_keywords=["aaa", "method", "bind"],
        description="show aaa method bind [method_name]",
    ),
    TestQuery(
        id="R2-show-slb-group",
        category="R-show",
        query="show slb group display server load balancing group members",
        expected_entities=["SHOW_SLB_GROUP"],
        expected_keywords=["显示", "slb"],
        vector_keywords=["slb", "group"],
        description="show slb group — 显示SLB组配置",
    ),
    TestQuery(
        id="R3-no-aaa-ldap-host",
        category="R-no",
        query="no aaa ldap host delete remove ldap server index",
        expected_entities=["NO_AAA_LDAP_HOST"],
        expected_keywords=["删除", "ldap"],
        vector_keywords=["aaa", "ldap", "host"],
        description="no aaa ldap host <ldap_server_name> <index>",
    ),
    TestQuery(
        id="R4-clear-slb-group-member",
        category="R-clear",
        query="clear slb group member remove server from load balancing group",
        expected_entities=["CLEAR_SLB_GROUP_MEMBER"],
        expected_keywords=["清除", "slb"],
        vector_keywords=["slb", "group", "member"],
        description="clear slb group member <group_name>",
    ),
    TestQuery(
        id="R5-clear-ha-group-port",
        category="R-clear",
        query="clear ha group port floating IP remove HA group port config",
        expected_entities=["CLEAR_HA_GROUP_PORT"],
        expected_keywords=["清除", "ha"],
        vector_keywords=["ha", "group", "port"],
        description="clear ha group port <group_id> — 组级HA配置",
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


async def run_single_test(retriever, hybrid_retriever, tq: TestQuery) -> TestResult:
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

    print("=" * 80)
    print("Round 3 检索完整性测试 — 20 条查询, 6 类 (M/N/O/P/Q/R)")
    print("=" * 80)

    print("\n[1/3] 加载 GraphRAG 索引...")
    retriever = load_graphrag()
    ent_n = len(retriever._entities) if retriever._entities is not None else 0
    rel_n = len(retriever._relationships) if retriever._relationships is not None else 0
    print(f"  实体数: {ent_n}, 关系数: {rel_n}")

    print("\n[2/3] 加载向量库（SHA256 指纹检查，变更后自动重建）...")
    hybrid_retriever = load_vector_rag()
    try:
        vr = getattr(hybrid_retriever, "vr", None)
        storage = getattr(vr, "storage", None) if vr else None
        if storage:
            client = getattr(storage, "_client", None)
            coll = getattr(storage, "collection_name", "workflow_rag")
            if client:
                info = client.get_collection(coll)
                print(f"  Qdrant: collection={coll}, points={info.points_count}, status={info.status}")
    except Exception as e:
        print(f"  Qdrant 状态: {e}")

    print(f"\n[3/3] 执行 {len(QUERIES)} 条测试...")
    results: List[TestResult] = []
    for tq in QUERIES:
        logger.info("Testing %s: %s", tq.id, tq.description)
        tr = await run_single_test(retriever, hybrid_retriever, tq)
        results.append(tr)
        g_ok = tr.entity_hit and tr.keyword_hit
        v_ok = tr.vector_hit
        logger.info("  [G:%s V:%s] entity=%s graphkw=%s vectorkw=%s ctx=%d vec=%d rels=%d%s",
            "OK" if g_ok else "FAIL", "OK" if v_ok else "FAIL",
            "OK" if tr.entity_hit else f"MISS:{tr.entity_missed}",
            "OK" if tr.keyword_hit else f"MISS:{tr.keyword_missed}",
            "OK" if tr.vector_hit else f"MISS:{tr.vector_missed}",
            tr.context_snippets, tr.vector_snippets, tr.rel_count,
            f" ERR:{tr.error}" if tr.error else "")

    total = len(results)
    full_pass = sum(1 for r in results if r.entity_hit and r.keyword_hit and r.vector_hit)
    graph_pass = sum(1 for r in results if r.entity_hit and r.keyword_hit)
    vector_pass = sum(1 for r in results if r.vector_hit)
    entity_pass = sum(1 for r in results if r.entity_hit)

    print("\n" + "=" * 80)
    print("RETRIEVAL TEST SUMMARY (Round 3)")
    print("=" * 80)
    print(f"Full Pass (Graph+Vector): {full_pass}/{total}")
    print(f"GraphRAG Pass:            {graph_pass}/{total}")
    print(f"Vector Pass:              {vector_pass}/{total}")
    print(f"Entity Exist:             {entity_pass}/{total}")

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
    print(f"  {'Category':<22} {'Graph':>8} {'Vector':>8} {'Full':>8}")
    print(f"  {'-'*22} {'-'*8} {'-'*8} {'-'*8}")
    for cat, stats in sorted(categories.items()):
        t = stats["total"]
        print(f"  {cat:<22} {stats['graph']}/{t:>5} {stats['vector']}/{t:>5} {stats['full']}/{t:>5}")

    failures = [(tq, tr) for tq, tr in zip(QUERIES, results)
                if not (tr.entity_hit and tr.keyword_hit and tr.vector_hit)]
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

    return full_pass, total


if __name__ == "__main__":
    full_pass, total = asyncio.run(main())
    sys.exit(0 if full_pass == total else 1)

