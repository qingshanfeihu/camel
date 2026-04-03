"""
GraphRAG 树完整性 + 检索可靠性测试

挑选原则：
  A. 不同级别(用户/权限/配置)命令
  B. 含 <> 必选参数的命令
  C. 含 [] 可选参数的命令
  D. 含 {x|y} 选择参数的命令
  E. no / show / clear 变体命令
  F. 不同功能模块(SLB/SSL/NAT/HEALTH/SNMP/SYSTEM/ACL/BGP)

每条查询同时检查:
  1. GraphRAG local_context_build 能否命中相关实体
  2. GraphRAG entity/relationship 层是否有正确关联
"""
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("retrieval_test")
logger.setLevel(logging.INFO)


@dataclass
class TestQuery:
    id: str
    category: str       # A~F tag
    query: str
    expected_entities: List[str]    # entity titles (uppercase) that MUST appear
    expected_keywords: List[str]    # keywords that should appear in retrieved text
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
    context_snippets: int = 0
    error: str = ""


# ── Test queries ──────────────────────────────────────────────────────────

QUERIES: List[TestQuery] = [
    # --- A: 不同级别命令 ---
    TestQuery(
        id="A1-global-system",
        category="A-level",
        query="system dump enable disable panic coredump",
        expected_entities=["SYSTEM_SYSTEM_DUMP"],
        expected_keywords=["dump", "on", "off"],
        description="全局级 system 命令，{on|off} 选择",
    ),
    TestQuery(
        id="A2-config-bgp-neighbor",
        category="A-level",
        query="BGP neighbor configuration peer ASN",
        expected_entities=["BGP_NEIGHBOR"],
        expected_keywords=["bgp", "neighbor"],
        description="路由协议级配置命令",
    ),
    TestQuery(
        id="A3-admin-aaa-method",
        category="A-level",
        query="admin authentication method RADIUS TACACS",
        expected_entities=["ADMIN_AAA_METHOD"],
        expected_keywords=["admin", "aaa", "method"],
        description="管理员认证级命令",
    ),

    # --- B: 含 <> 必选参数 ---
    TestQuery(
        id="B1-ssl-csr-required-params",
        category="B-required-param",
        query="ssl csr generate certificate signing request virtual host name",
        expected_entities=["SSL_CSR"],
        expected_keywords=["ssl", "csr", "virtual_host_name"],
        description="ssl csr <virtual_host_name> — 必选参数",
    ),
    TestQuery(
        id="B2-system-time-required",
        category="B-required-param",
        query="system time hour minute second set clock",
        expected_entities=["SYSTEM_SYSTEM_TIME"],
        expected_keywords=["system", "time", "hour", "minute", "second"],
        description="system time <hour> <minute> <second> — 三个必选参数",
    ),
    TestQuery(
        id="B3-system-license-key",
        category="B-required-param",
        query="system license key value activate",
        expected_entities=["SYSTEM_SYSTEM_LICENSE"],
        expected_keywords=["license", "key"],
        description="system license <license_key_value> — 必选参数",
    ),

    # --- C: 含 [] 可选参数 ---
    TestQuery(
        id="C1-ssl-activate-cert-optional",
        category="C-optional-param",
        query="ssl activate certificate host domain certificate type index",
        expected_entities=["SSL_ACTIVATE_CERTIFICATE"],
        expected_keywords=["ssl", "activate", "certificate"],
        description="ssl activate certificate <host> [cert_index] [domain] [cert_type]",
    ),
    TestQuery(
        id="C2-health-interval-optional",
        category="C-optional-param",
        query="health interval seconds timeout service check",
        expected_entities=["HEALTH_INTERVAL"],
        expected_keywords=["健康检查", "间隔"],
        description="health interval，含可选超时参数",
    ),

    # --- D: 含 {x|y} 选择参数 ---
    TestQuery(
        id="D1-ssl-host-virtual-real",
        category="D-choice-param",
        query="ssl host virtual real configuration slb service",
        expected_entities=["SSL_HOST"],
        expected_keywords=["ssl", "virtual"],
        description="ssl host {virtual|real} <host_name> — 选择参数",
    ),
    TestQuery(
        id="D2-system-tcp-slowstart",
        category="D-choice-param",
        query="system tune tcp slowstart on off toggle",
        expected_entities=["SYSTEM_SYSTEM_TUNE_TCP_SLOWSTART"],
        expected_keywords=["tcp", "slowstart", "on", "off"],
        description="system tune tcp slowstart {on|off}",
    ),
    TestQuery(
        id="D3-system-thread-mode",
        category="D-choice-param",
        query="system thread single auto mode configuration",
        expected_entities=["SYSTEM_SYSTEM_THREAD"],
        expected_keywords=["thread", "single", "auto"],
        description="system thread {single|auto}",
    ),

    # --- E: no/show/clear 变体 ---
    TestQuery(
        id="E1-show-slb-group-health",
        category="E-show",
        query="show slb group health check status display",
        expected_entities=["SHOW_SLB_GROUP_HEALTH"],
        expected_keywords=["健康检查", "slb"],
        description="show 命令：查看 SLB 组健康检查",
    ),
    TestQuery(
        id="E2-no-snmp-community",
        category="E-no",
        query="no snmp community remove delete string",
        expected_entities=["NO_SNMP_COMMUNITY"],
        expected_keywords=["no", "snmp", "community"],
        description="no 命令：删除 SNMP 社区字符串",
    ),
    TestQuery(
        id="E3-clear-health-checker",
        category="E-clear",
        query="clear health checker reset default",
        expected_entities=["CLEAR_HEALTH_CHECKER"],
        expected_keywords=["健康检查", "探测"],
        description="clear 命令：清除健康检查器配置",
    ),
    TestQuery(
        id="E4-no-accesslist-deny-tcp",
        category="E-no",
        query="no accesslist deny TCP traffic rule remove",
        expected_entities=["NO_ACCESSLIST_DENY_TCP"],
        expected_keywords=["no", "accesslist", "deny", "tcp"],
        description="no 命令：删除 TCP 拒绝 ACL 规则",
    ),
    TestQuery(
        id="E5-show-nat-port",
        category="E-show",
        query="show nat port NAPT source translation status",
        expected_entities=["SHOW_NAT_PORT"],
        expected_keywords=["show", "nat", "port"],
        description="show 命令：查看 NAT 端口转换",
    ),
    TestQuery(
        id="E6-clear-ip-arp",
        category="E-clear",
        query="clear ip arp table entry reset",
        expected_entities=["CLEAR_IP_ARP"],
        expected_keywords=["clear", "ip", "arp"],
        description="clear 命令：清除 ARP 表项",
    ),

    # --- F: 跨模块关联检索 ---
    TestQuery(
        id="F1-snmp-host-trap",
        category="F-cross-module",
        query="snmp host trap receiver IP version community security",
        expected_entities=["SNMP_HOST"],
        expected_keywords=["snmp", "host", "trap"],
        description="SNMP Trap 主机配置",
    ),
    TestQuery(
        id="F2-nat-portdst-destination",
        category="F-cross-module",
        query="nat portdst destination IP address port translation NAPT",
        expected_entities=["NAT_PORTDST"],
        expected_keywords=["nat", "portdst"],
        description="基于目的 IP 的 NAT 端口转换",
    ),
    TestQuery(
        id="F3-health-app-bind",
        category="F-cross-module",
        query="health app bind health check list real service AppHC",
        expected_entities=["HEALTH_APP"],
        expected_keywords=["健康检查", "apphc"],
        description="健康检查列表绑定到服务",
    ),

    # --- G: ircookie 协作断链修复验证 ---
    TestQuery(
        id="G1-ircookie-enc-param",
        category="G-ircookie",
        query="ircookie enc_name 加密格式 group_name password",
        expected_entities=["SLB_MODE_IRCOOKIE"],
        expected_keywords=["enc_name", "ircookie"],
        description="ircookie 参数详情（来自农民富化）",
    ),
    TestQuery(
        id="G2-ic-method-cross-ref",
        category="G-ircookie",
        query="slb group method ic cookie 值来源 插入模式",
        expected_entities=["SLB_GROUP_METHOD_IC"],
        expected_keywords=["ic", "ircookie"],
        description="ic 调度方式跨引用 slb mode ircookie",
    ),
    TestQuery(
        id="G3-override-attribute",
        category="G-ircookie",
        query="系统命令覆盖 slb mode ircookie override",
        expected_entities=["SLB_MODE_IRCOOKIE"],
        expected_keywords=["覆盖", "override"],
        description="ircookie 支持系统命令覆盖属性",
    ),
]


def load_graphrag():
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever
    workspace = Path("INAGENT/graphrag_index")
    retriever = GraphRAGRetriever(workspace_dir=workspace)
    if not retriever.is_available():
        raise RuntimeError("GraphRAG not available")
    return retriever


def check_entity_direct(retriever, expected_titles: List[str]) -> tuple:
    found = []
    missed = []
    if retriever._entities is None:
        return found, expected_titles
    for title in expected_titles:
        match = retriever._entities[retriever._entities["title"].str.upper() == title.upper()]
        if len(match) > 0:
            found.append(title)
        else:
            missed.append(title)
    return found, missed


def check_relationships(retriever, entity_title: str) -> List[dict]:
    if retriever._relationships is None:
        return []
    rels = retriever._relationships[
        (retriever._relationships["source"].str.upper() == entity_title.upper()) |
        (retriever._relationships["target"].str.upper() == entity_title.upper())
    ]
    result = []
    for _, r in rels.head(5).iterrows():
        result.append({
            "source": r.get("source", ""),
            "target": r.get("target", ""),
            "description": str(r.get("description", ""))[:100],
        })
    return result


async def run_single_test(retriever, tq: TestQuery) -> TestResult:
    result = TestResult(query_id=tq.id)

    # 1. Entity existence check
    found, missed = check_entity_direct(retriever, tq.expected_entities)
    result.entity_found = found
    result.entity_missed = missed
    result.entity_hit = len(missed) == 0

    # 2. Retrieval check via local_context_build
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

    return result


async def main():
    logger.info("Loading GraphRAG...")
    retriever = load_graphrag()
    logger.info("GraphRAG loaded: %d entities, %d relationships",
                len(retriever._entities) if retriever._entities is not None else 0,
                len(retriever._relationships) if retriever._relationships is not None else 0)

    results: List[TestResult] = []
    for tq in QUERIES:
        logger.info("Testing %s: %s", tq.id, tq.description)
        tr = await run_single_test(retriever, tq)
        results.append(tr)

        # Print relationship info for first expected entity
        if tq.expected_entities:
            rels = check_relationships(retriever, tq.expected_entities[0])
            rel_count = len(rels)
        else:
            rel_count = 0

        status = "PASS" if (tr.entity_hit and tr.keyword_hit) else "FAIL"
        logger.info("  [%s] entity=%s keyword=%s ctx=%d rels=%d %s",
                     status,
                     "OK" if tr.entity_hit else f"MISS:{tr.entity_missed}",
                     "OK" if tr.keyword_hit else f"MISS:{tr.keyword_missed}",
                     tr.context_snippets,
                     rel_count,
                     f"ERR:{tr.error}" if tr.error else "")

    # Summary
    print("\n" + "=" * 80)
    print("RETRIEVAL TEST SUMMARY")
    print("=" * 80)

    pass_count = sum(1 for r in results if r.entity_hit and r.keyword_hit)
    entity_pass = sum(1 for r in results if r.entity_hit)
    keyword_pass = sum(1 for r in results if r.keyword_hit)
    total = len(results)

    print(f"\nTotal: {total} | Full Pass: {pass_count} | Entity Pass: {entity_pass} | Keyword Pass: {keyword_pass}")
    print(f"Pass Rate: {pass_count}/{total} ({100*pass_count/total:.0f}%)")

    # Category breakdown
    categories = {}
    for tq, tr in zip(QUERIES, results):
        cat = tq.category
        if cat not in categories:
            categories[cat] = {"total": 0, "pass": 0}
        categories[cat]["total"] += 1
        if tr.entity_hit and tr.keyword_hit:
            categories[cat]["pass"] += 1

    print("\nBy Category:")
    for cat, stats in sorted(categories.items()):
        pct = 100 * stats["pass"] / stats["total"]
        print(f"  {cat}: {stats['pass']}/{stats['total']} ({pct:.0f}%)")

    # Failures detail
    failures = [(tq, tr) for tq, tr in zip(QUERIES, results) if not (tr.entity_hit and tr.keyword_hit)]
    if failures:
        print(f"\nFailed Queries ({len(failures)}):")
        for tq, tr in failures:
            print(f"  {tq.id} [{tq.category}]: {tq.description}")
            if tr.entity_missed:
                print(f"    entity missed: {tr.entity_missed}")
            if tr.keyword_missed:
                print(f"    keyword missed: {tr.keyword_missed}")
            if tr.error:
                print(f"    error: {tr.error}")

    # Save detailed results
    output_path = Path("INAGENT/graphrag_index/logs/retrieval_test_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    detail = []
    for tq, tr in zip(QUERIES, results):
        rels = check_relationships(retriever, tq.expected_entities[0]) if tq.expected_entities else []
        detail.append({
            "id": tq.id,
            "category": tq.category,
            "query": tq.query,
            "description": tq.description,
            "entity_hit": tr.entity_hit,
            "entity_found": tr.entity_found,
            "entity_missed": tr.entity_missed,
            "keyword_hit": tr.keyword_hit,
            "keyword_found": tr.keyword_found,
            "keyword_missed": tr.keyword_missed,
            "context_snippets": tr.context_snippets,
            "relationships": rels,
            "error": tr.error,
            "pass": tr.entity_hit and tr.keyword_hit,
        })
    output_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Detailed results saved to %s", output_path)

    return pass_count == total


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
