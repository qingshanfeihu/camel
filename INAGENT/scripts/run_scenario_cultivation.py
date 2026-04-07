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
农场主 + 农民：枝干场景培育两阶段 CLI

阶段 1（农场主）: 识别枝干缺口，写骨架 scenarios_scaffold.json
阶段 2（农民）  : 读骨架，LLM 加肉，写 scenarios_synthesized.json

用法：
    # 仅骨架（农场主）
    python -m INAGENT.scripts.run_scenario_cultivation --stage scaffold

    # 仅加肉（农民，需先有骨架）
    python -m INAGENT.scripts.run_scenario_cultivation --stage enrich

    # 两阶段一起
    python -m INAGENT.scripts.run_scenario_cultivation --stage all

    # 加肉后立即刷新向量索引
    python -m INAGENT.scripts.run_scenario_cultivation --stage all --refresh-vectors

    # Dry-run: 只统计，不写文件
    python -m INAGENT.scripts.run_scenario_cultivation --stage scaffold --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scenario_cultivation")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="枝干场景培育（农场主建骨架 → 农民加肉）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--stage",
        choices=["scaffold", "enrich", "all"],
        default="all",
        help="scaffold=仅骨架(农场主) / enrich=仅加肉(农民) / all=两阶段 (默认 all)",
    )
    ap.add_argument("--dry-run", action="store_true", help="scaffold 阶段 dry-run")
    ap.add_argument("--refresh-vectors", action="store_true", help="enrich 后刷新 Qdrant/BM25")
    ap.add_argument("--force", action="store_true", help="强制重新生成已有条目")
    ap.add_argument("--min-cmds", type=int, default=4, help="branch 层最小命令数 (默认 4)")
    ap.add_argument("--max-cmds", type=int, default=15, help="branch 层最大命令数 (默认 15)")
    ap.add_argument("--no-trunk", action="store_true", help="跳过 trunk(模块)层缺口扫描")
    ap.add_argument("--no-root", action="store_true", help="跳过 root(产品)层缺口扫描")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    errors: list = []

    # ── 阶段 1：农场主建骨架 ─────────────────────────────────────────
    if args.stage in ("scaffold", "all"):
        logger.info("=== 阶段 1：农场主建骨架 ===")
        try:
            from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent
            owner = KnowledgeFarmOwnerAgent(graphrag_retriever=None)
        except Exception as exc:
            logger.error("无法初始化 KnowledgeFarmOwnerAgent: %s", exc)
            sys.exit(1)

        try:
            from INAGENT.web.deps import get_llm_model
            owner_model = get_llm_model()
            owner._model = owner_model
            logger.info("LLM 模型已注入农场主（用于 HyDE 规格生成）")
        except Exception as exc:
            logger.warning("LLM 加载失败（%s），农场主将在无 HyDE 模式下运行", exc)

        scaffold_result = owner.cultivate_scenarios(
            min_branch_cmds=args.min_cmds,
            max_branch_cmds=args.max_cmds,
            include_trunk=not args.no_trunk,
            include_root=not args.no_root,
            dry_run=args.dry_run,
            force_regenerate=args.force,
        )
        gl = scaffold_result.get("gaps_by_level", {})
        logger.info(
            "骨架: gaps=(root=%d trunk=%d branch=%d) scaffolded=%d skipped=%d errors=%d",
            gl.get("root", 0), gl.get("trunk", 0), gl.get("branch", 0),
            scaffold_result["scaffolded"],
            scaffold_result["skipped"],
            len(scaffold_result["errors"]),
        )
        errors.extend(scaffold_result["errors"])
        if not args.dry_run:
            logger.info("骨架写入: %s", scaffold_result.get("scaffold_path", ""))

    # ── 阶段 2：农民加肉 ─────────────────────────────────────────────
    if args.stage in ("enrich", "all") and not args.dry_run:
        logger.info("=== 阶段 2：农民加肉 ===")
        model = None
        try:
            from INAGENT.web.deps import get_llm_model
            model = get_llm_model()
            logger.info("LLM 模型已加载")
        except Exception as exc:
            logger.warning("LLM 加载失败（%s），将使用内联模板", exc)

        try:
            from INAGENT.agents.knowledge_farmer_agent import KnowledgeFarmerAgent
            from INAGENT.utils.env_utils import get_product_name
            farmer = KnowledgeFarmerAgent(
                model=model,
                product_name=get_product_name(),
            )
        except Exception as exc:
            logger.error("无法初始化 KnowledgeFarmerAgent: %s", exc)
            sys.exit(1)

        enrich_result = farmer.enrich_scenario_nodes(
            force_reenrich=args.force,
            refresh_hybrid_vectors=args.refresh_vectors,
        )
        logger.info(
            "加肉: total=%d enriched=%d skipped=%d errors=%d",
            enrich_result["total"],
            enrich_result["enriched"],
            enrich_result["skipped"],
            len(enrich_result["errors"]),
        )
        errors.extend(enrich_result["errors"])

        if enrich_result["enriched"] > 0:
            _ref = Path(__file__).resolve().parent.parent / "knowledge_base" / "reference"
            logger.info("场景文档写入: %s", _ref / "scenarios_synthesized.json")
            if args.refresh_vectors:
                logger.info("向量索引已刷新，可运行 Phase 2 测试")
            else:
                logger.info("提示: 运行 --refresh-vectors 或手动刷新向量后再执行 Phase 2 测试")

    if errors:
        logger.warning("共 %d 个错误:", len(errors))
        for e in errors:
            logger.warning("  %s", e)
        sys.exit(1)
    else:
        logger.info("全部完成，无错误")
        sys.exit(0)


if __name__ == "__main__":
    main()
