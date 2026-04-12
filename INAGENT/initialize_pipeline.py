# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
初始化Pipeline - 完整的workflow基础流程

包含以下步骤：
1. PDF 文件识别和导入 (procurement_ingest.py — 采购管线)
2. MinerU 提取内容
3. LLM 提取 metadata (product_module, protocol_type, step_type)
4. 基于功能结构索引增强 metadata (scenario_id, step_type)
5. 自动识别文档模块和功能 (auto_document_integration.py)
6. 增量更新功能结构索引 (incrementally_update_function_index)
7. 合并为 knowledge_base.json (merge_knowledge_base.py)
8. RAG 索引 (workforce_config_ops.py: initialize_rag_system)
"""
import asyncio
import logging
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from INAGENT.utils import env_utils
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
    
    # 步骤1-7: 运行采购文档管线 procurement_ingest（实现位于 auto_convert.run_procurement_document_pipeline）
    logger.info("")
    logger.info("步骤1-7: 运行 procurement_ingest（采购：MinerU/Office/TXT → reference）")
    logger.info("  - PDF识别和导入")
    logger.info("  - MinerU提取内容")
    logger.info("  - LLM提取metadata (product_module, protocol_type, step_type)")
    logger.info("  - 基于功能结构索引增强metadata")
    logger.info("  - 自动识别文档模块和功能")
    logger.info("  - 增量更新功能结构索引")
    logger.info("  - 合并为knowledge_base.json")
    logger.info("")
    
    try:
        from INAGENT.data_tools.procurement_ingest import main as procurement_ingest_main
        await procurement_ingest_main()
        logger.info("[成功] 步骤1-7完成")
    except Exception as e:
        logger.error(f"[错误] 步骤1-7失败: {e}", exc_info=True)
        raise
    
    # 步骤8: RAG索引
    logger.info("")
    logger.info("步骤8: 初始化RAG系统并索引知识库...")
    logger.info("")
    
    try:
        result = initialize_rag_system()
        hybrid_retriever = result[0]
        reranker = result[1]
        graphrag_retriever = result[2] if len(result) > 2 else None
        if graphrag_retriever is None:
            logger.info(
                "[成功] 步骤8完成：RAG系统已初始化（GraphRAG 未构建或未启用）"
            )
        else:
            logger.info("[成功] 步骤8完成：RAG系统已初始化（含 GraphRAG）")
    except Exception as e:
        logger.error(f"[错误] 步骤8失败: {e}", exc_info=True)
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
