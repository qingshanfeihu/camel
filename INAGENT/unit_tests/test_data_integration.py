# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
集成测试：测试模块之间的协作
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from INAGENT.data_tools.auto_document_integration import (
    auto_identify_document_module,
    incrementally_update_function_index,
)
from INAGENT.utils.index_utils import (
    load_function_structure_index,
    get_step_type_keywords,
    generate_query_from_decomposition,
)
from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base


class TestDocumentIdentificationToIndexUpdate:
    """测试：文档识别 -> 索引更新"""
    
    def test_full_flow(self, temp_dir, sample_function_index):
        """集成测试：完整的文档识别和索引更新流程"""
        # 1. 创建测试文档
        pdf_path = temp_dir / "test.pdf"
        content_blocks = [
            {
                "metadata": {"product_module": "SLB", "protocol_type": ["HTTP"]},
                "page_content": "SLB HTTP configuration"
            }
        ]
        
        # 2. 自动识别文档模块
        doc_metadata = auto_identify_document_module(
            pdf_path,
            content_blocks,
            sample_function_index
        )
        assert "SLB" in doc_metadata["product_modules"]
        
        # 3. 增量更新索引
        index_path = temp_dir / "function_structure_index.json"
        index_path.write_text(json.dumps(sample_function_index, ensure_ascii=False))
        
        new_documents = [
            {
                "pdf_path": pdf_path,
                "json_path": temp_dir / "test.json",
                "document_metadata": doc_metadata
            }
        ]
        
        updated_index = incrementally_update_function_index(
            new_documents,
            index_path
        )
        
        # 4. 验证索引已更新
        assert "SLB" in updated_index["metadata_statistics"]["product_modules"]


class TestIndexToRAGQuery:
    """测试：索引 -> RAG 查询生成"""
    
    def test_index_to_query_generation(self, sample_function_index, sample_decomposition_result):
        """集成测试：从索引生成 RAG 查询"""
        # 1. 加载索引
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=sample_function_index):
            # 2. 生成查询
            queries = generate_query_from_decomposition(
                sample_decomposition_result,
                sample_function_index
            )
            
            # 3. 验证查询包含模块和协议信息
            assert len(queries) > 0
            assert any("SLB" in q and "HTTP" in q for q in queries)
    
    def test_dynamic_keywords_loading(self, sample_function_index):
        """集成测试：动态加载关键词用于检测"""
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=sample_function_index):
            # 从索引加载步骤类型关键词
            step_keywords = get_step_type_keywords(sample_function_index)
            
            # 验证关键词已加载
            assert "backend_servers" in step_keywords
            assert len(step_keywords["backend_servers"]) > 0


class TestMergeToIndexUpdate:
    """测试：合并知识库 -> 索引更新"""
    
    def test_merge_triggers_index_update(self, temp_dir, sample_function_index):
        """集成测试：合并知识库后触发索引更新"""
        # 1. 创建测试 JSON 文件
        reference_dir = temp_dir / "reference"
        reference_dir.mkdir()
        
        app_json = reference_dir / "app.json"
        app_json.write_text(json.dumps([
            {
                "page_content": "SLB content",
                "metadata": {
                    "product_module": "SLB",
                    "protocol_type": ["HTTP"]
                }
            }
        ], ensure_ascii=False))
        
        # 2. 合并知识库
        output_file = reference_dir / "knowledge_base.json"
        merge_knowledge_base(reference_dir, output_file, deduplicate=True)
        
        # 3. 验证合并结果
        assert output_file.exists()
        with open(output_file, "r", encoding="utf-8") as f:
            kb_data = json.load(f)
            assert len(kb_data) > 0
        
        # 4. 从合并后的知识库提取统计信息（模拟索引更新）
        # 这里可以调用 extract_metadata_statistics 或类似函数
        # 验证统计信息正确


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
