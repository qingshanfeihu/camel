# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
农场主批量导入脚本 - 支持快速演示模式
"""

import json
import shutil
import sys
from pathlib import Path
from datetime import datetime

_INAGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_INAGENT_ROOT.parent))

# 快速演示模式开关
FAST_MODE = True  # 改为 True 可启用快速模式（跳过LLM处理）

print(f"[INFO] 农场主批量处理 - {'快速演示模式' if FAST_MODE else '完整模式'}")

# 配置
QUALITY_OUTPUT_ROOT = _INAGENT_ROOT / "test_data" / "质检员输出"
FARM_OWNER_OUTPUT_ROOT = _INAGENT_ROOT / "test_data" / "农场主输出"
TEMP_REFERENCE_DIR = _INAGENT_ROOT / ".tmp_farm_owner_import"

print(f"[INFO] 质检员输出: {QUALITY_OUTPUT_ROOT}")
print(f"[INFO] 农场主输出: {FARM_OWNER_OUTPUT_ROOT}")

# 加载环境
try:
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()
    print(f"[INFO] 环境加载成功")
except Exception as e:
    print(f"[WARN] 环境加载失败: {e}")

# 如果是快速模式，禁用 LLM 和 GraphRAG
if FAST_MODE:
    print(f"[INFO] 启用快速演示模式 - 禁用 LLM 和 GraphRAG")
    import os
    os.environ["FARM_OWNER_DISABLE_LLM"] = "1"
    os.environ["FARM_OWNER_DISABLE_GRAPHRAG"] = "1"

# 导入农场主管道
try:
    from INAGENT.data_tools.farm_owner_ingest import run_farm_owner_pipeline
    print(f"[INFO] 农场主管道导入成功")
except Exception as e:
    print(f"[ERROR] 农场主管道导入失败: {e}")
    sys.exit(1)

# 清除输出目录
if FARM_OWNER_OUTPUT_ROOT.exists():
    shutil.rmtree(FARM_OWNER_OUTPUT_ROOT)
FARM_OWNER_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
print(f"[INFO] 已清除旧的农场主输出目录")

# 获取所有批次
batches = sorted([p for p in QUALITY_OUTPUT_ROOT.iterdir() if p.is_dir() and not p.name.startswith('.')])
print(f"[INFO] 找到 {len(batches)} 个质检批次")

all_results = []

for batch_idx, batch_dir in enumerate(batches, 1):
    batch_name = batch_dir.name
    print(f"\n{'='*60}")
    print(f"[INFO] [{batch_idx}/{len(batches)}] 处理批次: {batch_name}")
    
    # 检查必要文件
    gate_file = batch_dir / "_quality_gate_for_owner.jsonl"
    ref_file = batch_dir / "_quality_reference_for_owner.json"
    
    if not gate_file.exists() or not ref_file.exists():
        print(f"[WARN] 批次缺少必要文件，跳过")
        all_results.append({"batch": batch_name, "success": False, "error": "missing required files"})
        continue
    
    # 清理临时目录
    if TEMP_REFERENCE_DIR.exists():
        shutil.rmtree(TEMP_REFERENCE_DIR)
    TEMP_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    
    # 拷贝文件
    shutil.copy2(gate_file, TEMP_REFERENCE_DIR / "_quality_gate_for_owner.jsonl")
    shutil.copy2(ref_file, TEMP_REFERENCE_DIR / "_quality_reference_for_owner.json")
    
    gaps_file = batch_dir / "schema_gaps.jsonl"
    if gaps_file.exists():
        shutil.copy2(gaps_file, TEMP_REFERENCE_DIR / "schema_gaps.jsonl")
    
    print(f"[INFO] 已准备 reference 目录")
    
    # 调用农场主管道
    try:
        print(f"[INFO] 启动农场主管道...")
        result = run_farm_owner_pipeline(reference_dir=TEMP_REFERENCE_DIR)
        print(f"[INFO] ✓ 农场主管道完成")
        
        # 创建输出目录
        output_batch_dir = FARM_OWNER_OUTPUT_ROOT / batch_name
        output_batch_dir.mkdir(parents=True, exist_ok=True)
        
        # 拷贝决策文件
        decisions_src = TEMP_REFERENCE_DIR / "_owner_decisions_for_farmer.jsonl"
        if decisions_src.exists():
            decisions_dst = output_batch_dir / "_owner_decisions_for_farmer.jsonl"
            shutil.copy2(decisions_src, decisions_dst)
            
            # 统计决策文件
            decision_lines = decisions_dst.read_text(encoding="utf-8").strip().split('\n') if decisions_dst.stat().st_size > 0 else []
            print(f"[INFO] ✓ 已输出 decisions 文件 ({len([l for l in decision_lines if l.strip()])} 行)")
        
        # 保存结果
        result_file = output_batch_dir / "_farm_owner_result.json"
        result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # 输出结果统计
        if result.get("processed"):
            print(f"[INFO] 质检通过块数: {result.get('quality_passed_blocks', 0)}")
            print(f"[INFO] LLM增强块数: {result.get('llm_enhanced_blocks', 0)}")
            print(f"[INFO] 分类块数: {result.get('classified_blocks', 0)}")
            print(f"[INFO] 丢弃块数: {result.get('discarded_blocks', 0)}")
            if result.get('fill_requests'):
                print(f"[INFO] 填充请求数: {result.get('fill_requests', 0)}")
        else:
            print(f"[INFO] 处理结果: {result.get('reason', 'unknown')}")
        
        all_results.append({"batch": batch_name, "success": True})
        
    except Exception as exc:
        print(f"[ERROR] 异常: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        all_results.append({"batch": batch_name, "success": False, "error": str(exc)})

# 汇总
print(f"\n{'='*60}")
print(f"[INFO] 生成汇总报告")

summary = {
    "run_time": datetime.now().isoformat(),
    "fast_mode": FAST_MODE,
    "total_batches": len(batches),
    "successful_batches": sum(1 for r in all_results if r["success"]),
    "output_root": str(FARM_OWNER_OUTPUT_ROOT),
    "batches": all_results
}

output_file = FARM_OWNER_OUTPUT_ROOT / "_batch_summary.json"
output_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"[INFO] 总批次数: {summary['total_batches']}")
print(f"[INFO] 成功数: {summary['successful_batches']}")
print(f"[INFO] 结果已输出到: {FARM_OWNER_OUTPUT_ROOT.relative_to(Path.cwd())}")

# 清理临时目录
if TEMP_REFERENCE_DIR.exists():
    shutil.rmtree(TEMP_REFERENCE_DIR)

print(f"[INFO] ✓ 处理完成！")
