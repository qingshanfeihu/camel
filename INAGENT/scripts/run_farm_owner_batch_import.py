# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
从质检员输出批量导入数据到农场主，并将结果输出到农场主输出目录。

工作流程：
1. 扫描质检员输出目录中的所有批次
2. 对每个批次：
   - 拷贝质检门控工件到临时目录
   - 调用 run_farm_owner_pipeline 处理
   - 收集并输出结果
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

_INAGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_INAGENT_ROOT.parent))

from INAGENT.data_tools.farm_owner_ingest import run_farm_owner_pipeline
from INAGENT.utils.env_utils import load_inagent_env

logger = logging.getLogger("farm_owner_batch_import")

# 配置
QUALITY_OUTPUT_ROOT = _INAGENT_ROOT / "test_data" / "质检员输出"
FARM_OWNER_OUTPUT_ROOT = _INAGENT_ROOT / "test_data" / "农场主输出"
TEMP_REFERENCE_DIR = _INAGENT_ROOT / ".tmp_farm_owner_import"


def _setup_logging():
    """设置日志"""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s"
    )


def _get_batch_directories() -> List[Path]:
    """获取所有质检员输出批次目录"""
    if not QUALITY_OUTPUT_ROOT.exists():
        logger.error(f"质检员输出目录不存在: {QUALITY_OUTPUT_ROOT}")
        return []
    
    batches = [
        p for p in QUALITY_OUTPUT_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith('.')
    ]
    batches.sort()
    return batches


def _has_quality_files(batch_dir: Path) -> bool:
    """检查批次目录是否包含必要的质检工件"""
    gate_file = batch_dir / "_quality_gate_for_owner.jsonl"
    ref_file = batch_dir / "_quality_reference_for_owner.json"
    return gate_file.exists() and ref_file.exists()


def _prepare_reference_dir(batch_dir: Path) -> Path:
    """准备临时 reference 目录并拷贝必要的文件"""
    # 清理旧的临时目录
    if TEMP_REFERENCE_DIR.exists():
        shutil.rmtree(TEMP_REFERENCE_DIR)
    TEMP_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    
    # 拷贝必要文件
    gate_src = batch_dir / "_quality_gate_for_owner.jsonl"
    ref_src = batch_dir / "_quality_reference_for_owner.json"
    gaps_src = batch_dir / "schema_gaps.jsonl"
    
    gate_dst = TEMP_REFERENCE_DIR / "_quality_gate_for_owner.jsonl"
    ref_dst = TEMP_REFERENCE_DIR / "_quality_reference_for_owner.json"
    gaps_dst = TEMP_REFERENCE_DIR / "schema_gaps.jsonl"
    
    if gate_src.exists():
        shutil.copy2(gate_src, gate_dst)
    if ref_src.exists():
        shutil.copy2(ref_src, ref_dst)
    if gaps_src.exists():
        shutil.copy2(gaps_src, gaps_dst)
    
    return TEMP_REFERENCE_DIR


def _collect_output(batch_dir: Path, result: Dict) -> Path:
    """收集农场主的输出结果到指定目录"""
    batch_name = batch_dir.name
    output_batch_dir = FARM_OWNER_OUTPUT_ROOT / batch_name
    output_batch_dir.mkdir(parents=True, exist_ok=True)
    
    # 拷贝决策文件
    decisions_src = TEMP_REFERENCE_DIR / "_owner_decisions_for_farmer.jsonl"
    if decisions_src.exists():
        decisions_dst = output_batch_dir / "_owner_decisions_for_farmer.jsonl"
        shutil.copy2(decisions_src, decisions_dst)
        logger.info(f"✓ 已输出 decisions 文件: {decisions_dst.name}")
    
    # 保存结果摘要
    result_file = output_batch_dir / "_farm_owner_result.json"
    result_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logger.info(f"✓ 已输出 result 文件: {result_file.name}")
    
    return output_batch_dir


def _process_batch(batch_dir: Path) -> tuple[bool, Dict]:
    """处理单个批次"""
    batch_name = batch_dir.name
    logger.info(f"\n{'='*60}")
    logger.info(f"处理批次: {batch_name}")
    logger.info(f"{'='*60}")
    
    if not _has_quality_files(batch_dir):
        logger.warning(f"✗ 批次 {batch_name} 缺少必要的质检工件")
        return False, {"error": "missing quality files"}
    
    # 准备 reference 目录
    ref_dir = _prepare_reference_dir(batch_dir)
    logger.info(f"✓ 已准备 reference 目录: {ref_dir}")
    
    # 调用农场主管道
    try:
        logger.info("→ 启动农场主管道...")
        result = run_farm_owner_pipeline(reference_dir=ref_dir)
        logger.info(f"✓ 农场主管道完成")
        
        # 输出结果统计
        if result.get("processed"):
            logger.info(f"  - 质检通过块数: {result.get('quality_passed_blocks', 0)}")
            logger.info(f"  - LLM 增强块数: {result.get('llm_enhanced_blocks', 0)}")
            logger.info(f"  - 分类块数: {result.get('classified_blocks', 0)}")
            logger.info(f"  - 丢弃块数: {result.get('discarded_blocks', 0)}")
            logger.info(f"  - 实体添加数: {result.get('entities_added', 0)}")
            logger.info(f"  - 填充请求数: {result.get('fill_requests', 0)}")
            logger.info(f"  - 延迟处理数: {result.get('deferred', 0)}")
        else:
            logger.info(f"  - 原因: {result.get('reason', 'unknown')}")
        
        # 收集输出
        output_dir = _collect_output(batch_dir, result)
        logger.info(f"✓ 结果已输出到: {output_dir}")
        
        return True, result
        
    except Exception as exc:
        logger.exception(f"✗ 农场主管道异常: {exc}")
        return False, {"error": str(exc)}


def main():
    """主流程"""
    load_inagent_env()
    _setup_logging()
    
    logger.info("开始从质检员输出批量导入农场主...")
    logger.info(f"质检员输出根目录: {QUALITY_OUTPUT_ROOT}")
    logger.info(f"农场主输出根目录: {FARM_OWNER_OUTPUT_ROOT}")
    
    # 获取所有批次
    batches = _get_batch_directories()
    if not batches:
        logger.error("未找到任何质检员输出批次")
        return 1
    
    logger.info(f"找到 {len(batches)} 个批次")
    
    # 处理每个批次
    results = []
    for batch_dir in batches:
        success, result = _process_batch(batch_dir)
        results.append({
            "batch": batch_dir.name,
            "success": success,
            "result": result
        })
    
    # 生成汇总报告
    logger.info(f"\n{'='*60}")
    logger.info("汇总报告")
    logger.info(f"{'='*60}")
    
    summary = {
        "run_time": datetime.now().isoformat(),
        "total_batches": len(batches),
        "successful_batches": sum(1 for r in results if r["success"]),
        "output_root": str(FARM_OWNER_OUTPUT_ROOT),
        "batches": results
    }
    
    output_file = FARM_OWNER_OUTPUT_ROOT / "_batch_summary.json"
    output_file.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logger.info(f"✓ 汇总报告已保存: {output_file}")
    logger.info(f"✓ 总批次数: {summary['total_batches']}")
    logger.info(f"✓ 成功数: {summary['successful_batches']}")
    
    # 清理临时目录
    if TEMP_REFERENCE_DIR.exists():
        shutil.rmtree(TEMP_REFERENCE_DIR)
    
    return 0 if all(r["success"] for r in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    sys.exit(main())
