# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
初始化Pipeline - 完整的workflow基础流程

包含以下步骤：
1. 采购流程：原始文档 → reference / knowledge_base.json
2. 质检流程：knowledge_base 质量门控，输出 owner 输入工件
3. 农场主流程：消费质检门控 + schema_gaps，输出 farmer 决策工件
4. 农民流程：仅执行农场主决策并维护 reference
5. RAG 索引 (workforce_config_ops.py: initialize_rag_system)

说明：步骤1-4的权威实现仅保留在
`INAGENT/scripts/run_structured_ingest_pipeline.py`，本入口仅做委托调用。
"""
import asyncio
import logging
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from INAGENT.utils import env_utils
from INAGENT.scripts.run_structured_ingest_pipeline import (
    run_structured_ingest_pipeline,
)
from INAGENT.workflow_config_generator import initialize_rag_system

logger = logging.getLogger(__name__)


async def main():
    """执行完整的初始化流程"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    logger.info("=" * 80)
    logger.info("开始初始化Pipeline - 完整的workflow基础流程")
    logger.info("=" * 80)
    
    # 步骤1-4: 委托统一结构化入库入口（唯一权威实现）
    logger.info("")
    logger.info("步骤1-4: 调用 run_structured_ingest_pipeline（唯一权威入口）")
    logger.info("")

    try:
        structured_result = await run_structured_ingest_pipeline()
        logger.info("[成功] 步骤1-4完成: %s", structured_result)
    except Exception as e:
        logger.error(f"[错误] 步骤1-4失败: {e}", exc_info=True)
        raise

    # 步骤5: RAG索引
    logger.info("")
    logger.info("步骤5: 初始化RAG系统并索引知识库...")
    logger.info("")
    
    try:
        result = initialize_rag_system()
        hybrid_retriever = result[0]
        reranker = result[1]
        graphrag_retriever = result[2] if len(result) > 2 else None
        if graphrag_retriever is None:
            logger.info(
                "[成功] 步骤5完成：RAG系统已初始化（GraphRAG 未构建或未启用）"
            )
        else:
            logger.info("[成功] 步骤5完成：RAG系统已初始化（含 GraphRAG）")
    except Exception as e:
        logger.error(f"[错误] 步骤5失败: {e}", exc_info=True)
        raise
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("初始化Pipeline完成！所有步骤已成功执行。")
    logger.info("=" * 80)
    logger.info("")
    logger.info("现在可以使用功能2和功能3进行workflow处理：")
    logger.info("  - 功能2: Workflow: 输入需求生成配置")
    logger.info("  - 功能3: 运行Jobs处理")
    logger.info("")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户中断操作")
        sys.exit(0)
    except Exception as e:
        logger.error(f"初始化失败: {e}", exc_info=True)
        sys.exit(1)
