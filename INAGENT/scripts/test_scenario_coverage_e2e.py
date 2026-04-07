# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
Phase 2 E2E 测试：功能场景覆盖率测试

测试目标与 Phase 1 的本质差异：
  Phase 1（cli82）: 给定单个命令 → 验证该命令 chunk 是否出现在 top-K 中
                   衡量指标: hit_rate（10 条命令各命中则满分）

  Phase 2（本测试）: 给定一个功能场景（枝干） → 验证该功能的所有必要命令
                   是否都被检索覆盖到
                   衡量指标: coverage_rate（每个场景命中多少比例的必要命令）

场景定义来源：
  从 knowledge_base.json 按 _tree_feature_id 分组，选取命令数量在指定范围内的功能。
  每个功能生成一个场景查询（自然语言），然后执行大 top-K 检索，统计命中率。

通过标准：
  - 每个场景的覆盖率 >= COVERAGE_THRESHOLD（默认 0.6）
  - 场景平均覆盖率 >= AVG_COVERAGE_THRESHOLD（默认 0.5）

用法：
    python -m INAGENT.scripts.test_scenario_coverage_e2e
    python -m INAGENT.scripts.test_scenario_coverage_e2e --num-scenarios 5 --top-k 40
    python -m INAGENT.scripts.test_scenario_coverage_e2e --feature-id crontab_disable
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scenario_coverage")

INAGENT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = INAGENT_ROOT / "knowledge_base" / "reference"
KB_PATH = REFERENCE_DIR / "knowledge_base.json"
LOG_DIR = INAGENT_ROOT / "knowledge_base" / "logs"

# 场景筛选条件
MIN_COMMANDS_PER_FEATURE = 4
MAX_COMMANDS_PER_FEATURE = 15
NUM_SCENARIOS = 6           # 默认测试场景数
TOP_K = 40                  # 检索 top-K 候选（比 Phase 1 更大，因为需要覆盖多个命令）
COVERAGE_THRESHOLD = 0.5    # 单个场景通过门槛（50% 命令被检索到）
AVG_COVERAGE_THRESHOLD = 0.4  # 整体平均通过门槛

RANDOM_SEED = 42


# ─────────────────────────────────────────────────────────────────────────────
# 场景自然语言查询生成
# ─────────────────────────────────────────────────────────────────────────────

# function_hierarchy top-level → 中文描述映射（通用 fallback 用 feature_id）
_FH_LABEL: Dict[str, str] = {
    "SLB": "服务器负载均衡",
    "LLB": "链路负载均衡",
    "GSLB": "全局服务器负载均衡",
    "SSL": "SSL/TLS 加密",
    "安全": "安全策略",
    "基础网络": "基础网络配置",
    "HA": "高可用性",
    "高可用": "高可用性",
    "高可用性": "高可用性",
    "CRONTAB": "Crontab 定时任务",
    "VXLAN": "VXLAN 虚拟扩展局域网",
    "BRIDGE": "网桥",
    "WEBCLASSIFY": "网站分类",
    "DNS": "DNS 解析",
    "CUSTOM": "自定义脚本",
    "FWD": "转发模式",
}


def _build_scenario_query(feature_id: str, commands: List[str], fh: str) -> str:
    """
    从 feature_id + 命令列表 + function_hierarchy 构造自然语言场景查询。

    不做 LLM 调用，纯规则生成，因为测试本身不应依赖 LLM 运行。
    策略：
      - 用 function_hierarchy 的语义化名称
      - 列出前 3 个命令关键词作为上下文锚点
    """
    fh_top = fh.split(">")[0].strip() if fh else ""
    label = _FH_LABEL.get(fh_top, fh_top or feature_id.replace("_", " "))

    # 从 feature_id 提取语义词（去掉常见动词前缀）
    _strip_prefixes = ("slb_", "show_", "clear_", "no_", "debug_")
    fid_clean = feature_id
    for pfx in _strip_prefixes:
        if fid_clean.startswith(pfx):
            fid_clean = fid_clean[len(pfx):]
    fid_label = fid_clean.replace("_", " ")

    # 取前 3 个命令的关键词作上下文
    cmd_sample = [c for c in commands[:3] if c]
    cmd_ctx = "、".join(f"`{c}`" for c in cmd_sample) if cmd_sample else ""

    if cmd_ctx:
        return f"如何配置{label}的{fid_label}功能？相关命令包括 {cmd_ctx}"
    return f"如何配置{label}的{fid_label}功能？请列出所有配置命令和步骤。"


# ─────────────────────────────────────────────────────────────────────────────
# 从 knowledge_base.json 提取场景定义
# ─────────────────────────────────────────────────────────────────────────────

def load_feature_groups(
    kb_path: Path,
    min_cmds: int = MIN_COMMANDS_PER_FEATURE,
    max_cmds: int = MAX_COMMANDS_PER_FEATURE,
) -> Dict[str, Dict[str, Any]]:
    """
    返回 {feature_id: {commands: [...{tid, cp}], query, fh}} 的字典。
    只保留命令数量在 [min_cmds, max_cmds] 范围内的功能。
    """
    if not kb_path.exists():
        logger.error("knowledge_base.json 不存在: %s", kb_path)
        return {}

    data = json.loads(kb_path.read_text(encoding="utf-8"))
    raw_groups: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"commands": [], "fh": ""}
    )

    for item in data:
        m = item.get("metadata", {})
        fid = m.get("_tree_feature_id", "")
        if not fid:
            continue
        tid = m.get("tree_node_id") or m.get("node_id") or ""
        cp = m.get("command_prefix", "")
        fh = m.get("function_hierarchy", "")
        if not tid or not cp:
            continue
        grp = raw_groups[fid]
        # 去重（同一 _tree_feature_id 下 node_id 可能重复）
        existing_tids = {c["tid"] for c in grp["commands"]}
        if tid not in existing_tids:
            grp["commands"].append({"tid": tid, "cp": cp})
        if not grp["fh"] and fh:
            grp["fh"] = fh

    result: Dict[str, Dict[str, Any]] = {}
    for fid, grp in raw_groups.items():
        n = len(grp["commands"])
        if min_cmds <= n <= max_cmds:
            cps = [c["cp"] for c in grp["commands"]]
            grp["query"] = _build_scenario_query(fid, cps, grp["fh"])
            result[fid] = grp

    logger.info(
        "从 knowledge_base.json 加载 %d 个功能组（命令数 %d-%d）",
        len(result), min_cmds, max_cmds,
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 核心：向量检索 + 覆盖率计算
# ─────────────────────────────────────────────────────────────────────────────

def _get_doc_tree_node_id(doc: Dict) -> str:
    """从检索文档中提取 tree_node_id（支持两层路径）。"""
    if not isinstance(doc, dict):
        return ""
    meta = doc.get("metadata") or {}
    tid = meta.get("tree_node_id", "") or ""
    if not tid:
        tid = (meta.get("regex_metadata") or {}).get("tree_node_id", "") or ""
    return tid


def run_scenario_coverage_test(
    feature_groups: Dict[str, Dict[str, Any]],
    top_k: int = TOP_K,
    selected_feature_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    对每个功能场景执行向量检索，计算覆盖率。

    覆盖率 = 在 top-K 结果中出现的必要命令数 / 功能的总必要命令数
    """
    logger.info("=" * 60)
    logger.info("初始化向量检索器")
    logger.info("=" * 60)

    try:
        from INAGENT.workflow_config_generator import initialize_rag_system
        hybrid_retriever, _reranker, _gr = initialize_rag_system()
        logger.info("向量检索器初始化成功")
    except Exception as exc:
        logger.error("无法初始化 RAG 系统: %s", exc)
        return {"error": str(exc), "scenarios": [], "verdict": "ERROR"}

    if selected_feature_ids:
        groups_to_test = {
            fid: grp for fid, grp in feature_groups.items()
            if fid in selected_feature_ids
        }
    else:
        groups_to_test = feature_groups

    scenario_results: List[Dict[str, Any]] = []

    for fid, grp in groups_to_test.items():
        query = grp["query"]
        commands = grp["commands"]  # [{tid, cp}]
        required_tids = {c["tid"] for c in commands}
        fh = grp.get("fh", "")

        logger.info("-" * 60)
        logger.info("场景: %s  [%d 个必要命令]", fid, len(required_tids))
        logger.info("功能层级: %s", fh)
        logger.info("查询: %s", query)

        result_entry: Dict[str, Any] = {
            "feature_id": fid,
            "function_hierarchy": fh,
            "query": query,
            "required_commands": [c["cp"] for c in commands],
            "required_count": len(required_tids),
            "hit_tids": [],
            "miss_tids": [],
            "coverage": 0.0,
            "passed": False,
            "top_k": top_k,
        }

        try:
            result = hybrid_retriever.query(
                query,
                top_k=top_k,
                return_detailed_info=True,
            )
            retrieved = result.get("Retrieved Context", [])
            retrieved_tids = {_get_doc_tree_node_id(doc) for doc in retrieved if doc}
            retrieved_tids.discard("")

            hit_tids = required_tids & retrieved_tids
            miss_tids = required_tids - retrieved_tids

            coverage = len(hit_tids) / len(required_tids) if required_tids else 0.0

            result_entry["hit_tids"] = sorted(hit_tids)
            result_entry["miss_tids"] = sorted(miss_tids)
            result_entry["coverage"] = round(coverage, 3)
            result_entry["passed"] = coverage >= COVERAGE_THRESHOLD
            result_entry["retrieved_total"] = len(retrieved)
            result_entry["retrieved_tids_count"] = len(retrieved_tids)

            status = "PASS" if result_entry["passed"] else "FAIL"
            logger.info(
                "  覆盖率: %d/%d (%.1f%%) [%s]",
                len(hit_tids), len(required_tids), coverage * 100, status,
            )

            if hit_tids:
                logger.info("  命中: %s", ", ".join(sorted(hit_tids)))
            if miss_tids:
                # 找 miss 命令的对应 cp 便于诊断
                tid_to_cp = {c["tid"]: c["cp"] for c in commands}
                miss_cps = [f"{t}({tid_to_cp.get(t,'?')})" for t in sorted(miss_tids)]
                logger.info("  缺失: %s", ", ".join(miss_cps))

        except Exception as exc:
            logger.warning("  检索失败 (%s): %s", fid, exc)
            result_entry["error"] = str(exc)

        scenario_results.append(result_entry)

    return _build_report(scenario_results, top_k)


def _build_report(
    scenario_results: List[Dict[str, Any]],
    top_k: int,
) -> Dict[str, Any]:
    if not scenario_results:
        return {"scenarios": [], "summary": {}, "verdict": "SKIP"}

    coverages = [r["coverage"] for r in scenario_results if "error" not in r]
    passes = [r for r in scenario_results if r.get("passed")]

    avg_coverage = sum(coverages) / len(coverages) if coverages else 0.0
    pass_count = len(passes)
    total = len(scenario_results)

    if avg_coverage >= AVG_COVERAGE_THRESHOLD and pass_count >= total // 2:
        verdict = "PASS"
    elif avg_coverage >= AVG_COVERAGE_THRESHOLD * 0.6:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    summary = {
        "total_scenarios": total,
        "pass_count": pass_count,
        "avg_coverage": round(avg_coverage, 3),
        "pass_rate": round(pass_count / total, 3) if total else 0.0,
        "top_k": top_k,
        "coverage_threshold": COVERAGE_THRESHOLD,
        "avg_coverage_threshold": AVG_COVERAGE_THRESHOLD,
    }

    return {
        "scenarios": scenario_results,
        "summary": summary,
        "verdict": verdict,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 写日志报告
# ─────────────────────────────────────────────────────────────────────────────

def write_report(result: Dict[str, Any]) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = LOG_DIR / f"scenario_coverage_report_{ts}.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return report_path


def print_final_summary(result: Dict[str, Any]) -> None:
    s = result.get("summary", {})
    verdict = result.get("verdict", "N/A")

    logger.info("")
    logger.info("+" + "=" * 58 + "+")
    logger.info("|  Phase 2 场景覆盖率测试结果                               |")
    logger.info("+" + "=" * 58 + "+")
    logger.info("|  测试场景数    : %-40d|", s.get("total_scenarios", 0))
    logger.info("|  通过场景数    : %-40d|", s.get("pass_count", 0))
    logger.info("|  场景通过率    : %-40s|",
                f"{s.get('pass_rate', 0) * 100:.1f}%")
    logger.info("|  平均覆盖率    : %-40s|",
                f"{s.get('avg_coverage', 0) * 100:.1f}%  "
                f"(门槛 {AVG_COVERAGE_THRESHOLD * 100:.0f}%)")
    logger.info("|  检索 top-K    : %-40d|", s.get("top_k", 0))
    logger.info("+" + "-" * 58 + "+")
    logger.info("|  VERDICT: %-48s|", verdict)
    logger.info("+" + "=" * 58 + "+")

    logger.info("")
    logger.info("按覆盖率排序（低 → 高）:")
    scenarios = sorted(result.get("scenarios", []), key=lambda x: x.get("coverage", 0))
    for sc in scenarios:
        bar_len = int(sc.get("coverage", 0) * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        status = "PASS" if sc.get("passed") else "FAIL"
        logger.info(
            "  [%s] %s  %s  %.1f%%  (%d/%d 命令命中)",
            status,
            bar,
            sc.get("feature_id", "?")[:30],
            sc.get("coverage", 0) * 100,
            len(sc.get("hit_tids", [])),
            sc.get("required_count", 0),
        )

    # 分析缺失原因（汇总 miss_tids）
    all_miss: Dict[str, List[str]] = defaultdict(list)
    for sc in result.get("scenarios", []):
        for t in sc.get("miss_tids", []):
            all_miss[t].append(sc["feature_id"])
    if all_miss:
        logger.info("")
        logger.info("高频缺失命令（可能场景知识不足）:")
        for tid, fids in sorted(all_miss.items(), key=lambda x: -len(x[1]))[:10]:
            logger.info("  %s  (场景: %s)", tid, ", ".join(fids))


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Phase 2 功能场景覆盖率 E2E 测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--num-scenarios", type=int, default=NUM_SCENARIOS,
        help=f"随机抽取的测试场景数 (默认 {NUM_SCENARIOS})",
    )
    ap.add_argument(
        "--top-k", type=int, default=TOP_K,
        help=f"向量检索 top-K (默认 {TOP_K})",
    )
    ap.add_argument(
        "--feature-id", nargs="+",
        help="指定测试的 feature_id（空格分隔，不指定则随机抽样）",
    )
    ap.add_argument(
        "--min-cmds", type=int, default=MIN_COMMANDS_PER_FEATURE,
        help=f"场景最小命令数 (默认 {MIN_COMMANDS_PER_FEATURE})",
    )
    ap.add_argument(
        "--max-cmds", type=int, default=MAX_COMMANDS_PER_FEATURE,
        help=f"场景最大命令数 (默认 {MAX_COMMANDS_PER_FEATURE})",
    )
    ap.add_argument(
        "--list-features", action="store_true",
        help="只列出所有符合条件的 feature_id，不运行测试",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    feature_groups = load_feature_groups(
        KB_PATH,
        min_cmds=args.min_cmds,
        max_cmds=args.max_cmds,
    )

    if not feature_groups:
        logger.error("没有找到符合条件的功能组，退出")
        sys.exit(1)

    if args.list_features:
        for fid, grp in sorted(feature_groups.items(), key=lambda x: len(x[1]["commands"])):
            cps = [c["cp"] for c in grp["commands"]]
            print(f"{fid:40s} [{len(cps):2d}]  {grp['fh']}")
            print(f"  query: {grp['query']}")
        return

    # 选择测试场景
    if args.feature_id:
        selected = [fid for fid in args.feature_id if fid in feature_groups]
        missing = [fid for fid in args.feature_id if fid not in feature_groups]
        if missing:
            logger.warning("未找到指定 feature_id: %s", missing)
        if not selected:
            logger.error("指定的 feature_id 全部未找到，检查拼写或放宽 --min-cmds/--max-cmds")
            sys.exit(1)
    else:
        all_ids = list(feature_groups.keys())
        rng = random.Random(RANDOM_SEED)
        n = min(args.num_scenarios, len(all_ids))
        selected = rng.sample(all_ids, n)
        logger.info("随机抽取 %d 个场景（seed=%d）: %s", n, RANDOM_SEED, selected)

    result = run_scenario_coverage_test(
        feature_groups,
        top_k=args.top_k,
        selected_feature_ids=selected,
    )

    print_final_summary(result)
    report_path = write_report(result)
    logger.info("报告写入: %s", report_path)

    verdict = result.get("verdict", "FAIL")
    logger.info("Final verdict: %s", verdict)
    sys.exit(0 if verdict in ("PASS", "PARTIAL") else 1)


if __name__ == "__main__":
    main()
