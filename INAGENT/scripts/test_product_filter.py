#!/usr/bin/env python3
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""测试农场主的产品特定过滤功能"""

import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def create_test_chunks():
    """创建测试用的知识块（包含NSAE和华为的内容）"""
    return [
        # NSAE 产品知识
        {
            "page_content": "slb virtual-server 192.168.1.100 配置虚拟服务器，支持健康检查和会话保持功能。",
            "metadata": {
                "source_file": "test_nsae.json",
                "block_id": "nsae_001",
                "section_title": "SLB虚拟服务器配置",
                "product_module": "SLB",
            },
        },
        {
            "page_content": "nat pureip on 启用纯IP模式的NAT转换，适用于负载均衡场景。",
            "metadata": {
                "source_file": "test_nsae.json",
                "block_id": "nsae_002",
                "section_title": "NAT配置",
                "product_module": "NAT",
            },
        },
        # 华为产品知识（应该被过滤）
        {
            "page_content": "display interface gigabitethernet 0/0/1 查看接口状态。system-view 进入系统视图。",
            "metadata": {
                "source_file": "test_huawei.json",
                "block_id": "huawei_001",
                "section_title": "接口配置",
                "product_module": "基础网络",
            },
        },
        {
            "page_content": "华为CloudEngine交换机支持VLAN batch配置，可以批量创建VLAN。acl number 3000 创建高级ACL。",
            "metadata": {
                "source_file": "test_huawei.json",
                "block_id": "huawei_002",
                "section_title": "VLAN配置",
                "product_module": "VLAN",
            },
        },
        # 思科产品知识（应该被过滤）
        {
            "page_content": "enable 进入特权模式。configure terminal 进入全局配置模式。interface fastethernet 0/1 配置接口。",
            "metadata": {
                "source_file": "test_cisco.json",
                "block_id": "cisco_001",
                "section_title": "基本配置",
                "product_module": "基础配置",
            },
        },
        # 混合内容（NSAE + 通用术语）
        {
            "page_content": "高可用（HA）配置说明：设备支持主备模式和N+1模式，通过VRRP协议实现故障切换。",
            "metadata": {
                "source_file": "test_nsae.json",
                "block_id": "nsae_003",
                "section_title": "高可用配置",
                "product_module": "高可用",
            },
        },
    ]


def main():
    from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent

    logger.info("=" * 80)
    logger.info("测试农场主产品过滤功能")
    logger.info("=" * 80)

    # 创建测试数据
    test_chunks = create_test_chunks()
    logger.info("创建 %d 个测试知识块", len(test_chunks))

    # 创建农场主实例（不需要 GraphRAG）
    farm_owner = KnowledgeFarmOwnerAgent(graphrag_retriever=None)

    # 分析产品匹配度
    logger.info("\n开始产品匹配度分析...")
    feedback_records = farm_owner.analyze_product_match(test_chunks)

    logger.info("\n" + "=" * 80)
    logger.info("分析结果")
    logger.info("=" * 80)
    logger.info("总块数: %d", len(test_chunks))
    logger.info("不匹配块数: %d", len(feedback_records))

    if feedback_records:
        logger.info("\n检测到以下非目标产品的知识块：")
        for i, rec in enumerate(feedback_records, 1):
            logger.info("\n[%d] %s", i, rec["source_file"])
            logger.info("    block_id: %s", rec["block_id"])
            logger.info("    reason_code: %s", rec["reason_code"])
            logger.info("    notes: %s", rec["notes"])
            if "suggested_rule" in rec:
                rule = rec["suggested_rule"]
                logger.info(
                    "    建议规则: %s (竞品: %s, 置信度: %.2f)",
                    rule.get("type"),
                    rule.get("competitor"),
                    rule.get("confidence", 0),
                )
    else:
        logger.info("\n✓ 所有知识块均匹配目标产品")

    # 显示每个块的详细得分
    logger.info("\n" + "=" * 80)
    logger.info("详细得分分析")
    logger.info("=" * 80)

    # 加载产品特征配置
    config_path = Path(__file__).resolve().parent.parent / "config" / "product_signatures.json"
    if config_path.exists():
        signatures = json.loads(config_path.read_text(encoding="utf-8"))
        target = signatures.get("target_product", {})
        competitors = signatures.get("competitor_products", [])

        for chunk in test_chunks:
            meta = chunk.get("metadata", {})
            logger.info("\n[%s] %s", meta.get("source_file"), meta.get("block_id"))
            logger.info("  标题: %s", meta.get("section_title"))

            target_score = farm_owner._calculate_product_score(chunk, target)
            logger.info("  目标产品(NSAE)得分: %.3f", target_score)

            for comp in competitors:
                comp_score = farm_owner._calculate_product_score(chunk, comp)
                if comp_score > 0.1:
                    logger.info("  竞品(%s)得分: %.3f", comp.get("name"), comp_score)

    logger.info("\n" + "=" * 80)
    logger.info("测试完成")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
