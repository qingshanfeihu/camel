#!/usr/bin/env python3
"""测试农场主质检反馈处理完整流程。

测试场景：
1. 创建测试 reference 目录，包含 NSAE 和竞品知识块
2. 农场主自动分析产品匹配度，生成反馈到 inbox
3. 验证 inbox、生成规则建议、生成 purge manifest
4. 模拟执行删除（dry_run=True）
"""

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

# 添加项目根目录到 sys.path
project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_test_reference_dir(base_dir: Path) -> Path:
    """创建测试 reference 目录，包含 NSAE 和竞品知识块。"""
    ref_dir = base_dir / "test_reference"
    if ref_dir.exists():
        shutil.rmtree(ref_dir)
    ref_dir.mkdir(parents=True)

    # NSAE 知识块（使用 page_content 和 metadata 结构）
    nsae_chunks = [
        {
            "page_content": "使用 slb virtual-server 命令配置虚拟服务器。支持 TCP/UDP 协议。",
            "metadata": {
                "source_file": "nsae_knowledge.json",
                "block_id": "nsae_001",
                "section_title": "SLB虚拟服务器配置",
                "source": "NSAE_manual_v1.0.pdf",
                "page": 42,
            },
        },
        {
            "page_content": "配置 NAT 规则，支持源地址转换和目的地址转换。",
            "metadata": {
                "source_file": "nsae_knowledge.json",
                "block_id": "nsae_002",
                "section_title": "NAT配置",
                "source": "NSAE_manual_v1.0.pdf",
                "page": 58,
            },
        },
    ]

    # 华为知识块（包含明确的产品特征）
    huawei_chunks = [
        {
            "page_content": "华为S5700交换机接口配置。使用 system-view 进入系统视图，然后使用 interface GigabitEthernet 命令配置接口。支持 VLAN 和 IP 地址配置。使用 display interface 查看接口状态。",
            "metadata": {
                "source_file": "huawei_knowledge.json",
                "block_id": "huawei_001",
                "section_title": "华为S5700接口配置",
                "source": "Huawei_S5700_manual.pdf",
                "page": 12,
            },
        },
        {
            "page_content": "华为交换机VLAN配置。使用 vlan batch 命令批量创建 VLAN。支持 VLAN 范围配置。使用 display vlan 查看VLAN信息。CloudEngine系列交换机支持更多高级特性。",
            "metadata": {
                "source_file": "huawei_knowledge.json",
                "block_id": "huawei_002",
                "section_title": "华为VLAN配置",
                "source": "Huawei_S5700_manual.pdf",
                "page": 25,
            },
        },
    ]

    # 思科知识块（包含明确的产品特征）
    cisco_chunks = [
        {
            "page_content": "Cisco IOS基本配置。使用 enable 进入特权模式，使用 configure terminal 进入配置模式。使用 show running-config 查看当前配置。Catalyst交换机和ISR路由器都支持这些命令。",
            "metadata": {
                "source_file": "cisco_knowledge.json",
                "block_id": "cisco_001",
                "section_title": "Cisco IOS基本配置",
                "source": "Cisco_IOS_guide.pdf",
                "page": 8,
            },
        },
    ]

    # 写入文件
    (ref_dir / "nsae_knowledge.json").write_text(
        json.dumps(nsae_chunks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ref_dir / "huawei_knowledge.json").write_text(
        json.dumps(huawei_chunks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ref_dir / "cisco_knowledge.json").write_text(
        json.dumps(cisco_chunks, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info("创建测试 reference 目录: %s", ref_dir)
    logger.info("  - nsae_knowledge.json: %d 块", len(nsae_chunks))
    logger.info("  - huawei_knowledge.json: %d 块", len(huawei_chunks))
    logger.info("  - cisco_knowledge.json: %d 块", len(cisco_chunks))

    return ref_dir


def main():
    """主函数。"""
    logger.info("=" * 80)
    logger.info("测试农场主质检反馈处理完整流程")
    logger.info("=" * 80)

    # 1. 创建测试环境
    test_dir = Path(__file__).parent / "test_output"
    test_dir.mkdir(exist_ok=True)
    ref_dir = create_test_reference_dir(test_dir)

    # 2. 创建农场主实例（产品过滤不需要 graphrag_retriever）
    from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent

    logger.info("\n创建农场主实例...")
    # 产品过滤功能不依赖 graphrag，传入 None 即可
    farm_owner = KnowledgeFarmOwnerAgent(graphrag_retriever=None)

    # 3. 执行质检反馈处理（dry_run=True）
    logger.info("\n执行质检反馈处理（dry_run=True）...")
    report = farm_owner.process_quality_feedback(
        reference_dir=ref_dir,
        auto_analyze_product=True,
        dry_run=True,
    )

    # 4. 显示处理报告
    logger.info("\n" + "=" * 80)
    logger.info("处理报告")
    logger.info("=" * 80)
    logger.info("产品分析:")
    logger.info("  - 分析块数: %d", report["product_analysis"]["analyzed"])
    logger.info("  - 不匹配块数: %d", report["product_analysis"]["mismatches"])
    logger.info("\nInbox 验证:")
    logger.info("  - 有效记录: %d", report["inbox_validation"]["valid"])
    logger.info("  - 错误记录: %d", report["inbox_validation"]["errors"])
    logger.info("\n规则建议:")
    logger.info("  - 生成规则数: %d", report["rule_proposals"]["generated"])
    logger.info("\n删除操作:")
    logger.info("  - 删除块数: %d", report["purge"]["deleted"])
    logger.info("  - 错误数: %d", report["purge"]["errors"])
    logger.info("  - Dry run: %s", report["dry_run"])

    # 5. 检查生成的文件
    logger.info("\n" + "=" * 80)
    logger.info("生成的文件")
    logger.info("=" * 80)

    inbox_path = ref_dir / "_quality_feedback_inbox.jsonl"
    if inbox_path.exists():
        inbox_lines = inbox_path.read_text(encoding="utf-8").strip().split("\n")
        logger.info("\n质检反馈 inbox (_quality_feedback_inbox.jsonl): %d 条", len(inbox_lines))
        for i, line in enumerate(inbox_lines[:3], 1):
            rec = json.loads(line)
            logger.info("  [%d] %s / %s: %s", i, rec["source_file"], rec["block_id"], rec.get("notes", ""))

    proposals_path = ref_dir / "_quality_rule_proposals.json"
    if proposals_path.exists():
        proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
        logger.info("\n规则建议 (_quality_rule_proposals.json): %d 条", len(proposals.get("proposals", [])))
        for i, prop in enumerate(proposals.get("proposals", [])[:3], 1):
            logger.info("  [%d] %s: %s", i, prop.get("rule_type", "unknown"), prop.get("pattern", prop.get("description", "")))

    manifest_path = ref_dir / "_quality_purge_manifest.jsonl"
    if manifest_path.exists():
        manifest_lines = manifest_path.read_text(encoding="utf-8").strip().split("\n")
        logger.info("\n删除清单 (_quality_purge_manifest.jsonl): %d 条", len(manifest_lines))
        for i, line in enumerate(manifest_lines[:3], 1):
            rec = json.loads(line)
            logger.info("  [%d] %s / %s: %s", i, rec["source_file"], rec["block_id"], rec["action"])

    logger.info("\n" + "=" * 80)
    logger.info("测试完成")
    logger.info("=" * 80)
    logger.info("测试目录: %s", ref_dir)


if __name__ == "__main__":
    main()
