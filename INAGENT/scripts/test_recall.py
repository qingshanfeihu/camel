#!/usr/bin/env python3
"""GraphRAG + Vector 召回质量对比测试"""
import sys, asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from INAGENT.utils import env_utils; env_utils.load_inagent_env()
from INAGENT.rag.graphrag_integration import GraphRAGRetriever

import logging
logging.disable(logging.CRITICAL)

QUERIES = [
    # === 必选参数 <param> — 不同级别 ===
    ("必选<> 用户级 admin aaa server",   "admin aaa server 配置管理员认证服务器"),
    ("必选<> 配置级 http2 group",        "http2 group 配置HTTP2分组参数"),
    ("必选<> 权限级 accesslist deny ah", "accesslist deny ah 访问列表拒绝AH协议"),

    # === 可选参数 [param] ===
    ("可选[] activationserver",     "activationserver 命令可选参数有哪些"),
    ("可选[] ddos threshold http",  "clear ddos threshold http 可选参数"),

    # === {x|y} 枚举参数 ===
    ("{x|y} interface promisc",     "interface promisc 混杂模式枚举选项"),
    ("{x|y} nat port",              "nat port 地址转换端口枚举参数"),

    # === [x|y] 可选枚举 ===
    ("[x|y] llb method outbound",   "llb method outbound dd 负载均衡出站可选枚举"),
    ("[x|y] snmp on",               "snmp on 命令可选枚举参数"),

    # === no/show/clear 命令 — 不同模块 ===
    ("no cluster virtual",          "no cluster virtual 删除集群虚拟配置"),
    ("show config file",            "show config file 显示配置文件信息"),
    ("clear crontab config",        "clear crontab config 清除定时任务配置"),
]


def run():
    asyncio.run(_run())

async def _run():
    ws = Path(__file__).parent.parent / "graphrag_index"
    gr = GraphRAGRetriever(workspace_dir=ws)
    gr._ensure_initialized()

    try:
        from INAGENT.web.deps import get_unified_rag
        rag = get_unified_rag()
        has_vector = True
    except Exception as e:
        print(f"[warn] 统一RAG不可用: {e}")
        has_vector = False

    print("\n" + "=" * 78)
    print("GraphRAG + 向量召回对比测试")
    print("=" * 78)

    all_ok = True
    for label, q in QUERIES:
        print(f"\n{'─'*70}")
        print(f"[{label}]")
        print(f"查询: {q}")

        # GraphRAG
        gr_results = await gr.local_context_build(q, top_k=3)
        if gr_results:
            for i, r in enumerate(gr_results[:2]):
                print(f"  GraphRAG#{i+1} score={r.score:.3f} | {r.text[:100].replace(chr(10), ' ')}")
        else:
            print("  GraphRAG: 无结果 [!]")
            all_ok = False

        # Vector (via UnifiedRAG.hybrid_retriever direct)
        if has_vector:
            try:
                ctx, _, _ = rag.retrieve(q, category_whitelist=["cli/reference"], top_k_final=3)
                lines = [l for l in ctx.split("\n") if l.strip()][:4]
                if lines:
                    for i, l in enumerate(lines[:2]):
                        print(f"  Vector  #{i+1} | {l[:110]}")
                else:
                    print("  Vector: 无结果 [!]")
                    all_ok = False
            except Exception as e:
                print(f"  Vector ERROR: {e}")

    print("\n" + "=" * 78)
    print("conclusion: OK" if all_ok else "conclusion: GAP FOUND")
    print("=" * 78)
    if has_vector:
        try:
            rag.hybrid_retriever.vr.storage.close_client()
        except Exception:
            pass

if __name__ == "__main__":
    run()
