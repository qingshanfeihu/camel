# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
auto_document_integration.py 单元测试
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from typing import Dict, Any, List

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from INAGENT.data_tools.auto_document_integration import (
    compute_chunk_hash,
    infer_module_from_path,
    infer_document_type,
    extract_document_statistics,
    auto_identify_document_module,
    merge_statistics,
    incrementally_update_function_index,
    load_function_structure_index,
)


class TestComputeChunkHash:
    """测试 compute_chunk_hash()"""
    
    def test_same_content_same_hash(self):
        """TC2.1.1: 相同内容生成相同 hash"""
        chunk1 = {
            "page_content": "test content",
            "metadata": {"product_module": "SLB"}
        }
        chunk2 = {
            "page_content": "test content",
            "metadata": {"product_module": "SLB"}
        }
        
        hash1 = compute_chunk_hash(chunk1)
        hash2 = compute_chunk_hash(chunk2)
        assert hash1 == hash2
    
    def test_different_content_different_hash(self):
        """TC2.1.2: 不同内容生成不同 hash"""
        chunk1 = {
            "page_content": "content 1",
            "metadata": {"product_module": "SLB"}
        }
        chunk2 = {
            "page_content": "content 2",
            "metadata": {"product_module": "SLB"}
        }
        
        hash1 = compute_chunk_hash(chunk1)
        hash2 = compute_chunk_hash(chunk2)
        assert hash1 != hash2
    
    def test_different_metadata_different_hash(self):
        """TC2.1.3: metadata 不同，hash 不同"""
        chunk1 = {
            "page_content": "test content",
            "metadata": {"product_module": "SLB"}
        }
        chunk2 = {
            "page_content": "test content",
            "metadata": {"product_module": "LLB"}
        }
        
        hash1 = compute_chunk_hash(chunk1)
        hash2 = compute_chunk_hash(chunk2)
        assert hash1 != hash2


class TestInferModuleFromPath:
    """测试 infer_module_from_path()"""
    
    def test_path_contains_module_keyword(self):
        """TC2.2.1: 路径包含已知模块关键词"""
        test_index = {
            "metadata_statistics": {
                "product_modules": {
                    "SLB": {"keywords": ["slb", "负载均衡"]}
                }
            }
        }
        
        pdf_path = Path("knowledge_base/slb/app_manual.pdf")
        
        with patch('INAGENT.data_tools.auto_document_integration.load_function_structure_index', return_value=test_index):
            result = infer_module_from_path(pdf_path)
            assert "SLB" in result
    
    def test_path_contains_module_name(self):
        """TC2.2.5: 模块名称本身匹配"""
        test_index = {
            "metadata_statistics": {
                "product_modules": {
                    "SLB": {"keywords": ["slb"]}
                }
            }
        }
        
        pdf_path = Path("knowledge_base/SLB/manual.pdf")
        
        with patch('INAGENT.data_tools.auto_document_integration.load_function_structure_index', return_value=test_index):
            result = infer_module_from_path(pdf_path)
            assert "SLB" in result
    
    def test_path_no_match(self):
        """TC2.2.3: 路径不包含任何模块关键词"""
        test_index = {
            "metadata_statistics": {
                "product_modules": {
                    "SLB": {"keywords": ["slb"]}
                }
            }
        }
        
        pdf_path = Path("knowledge_base/unknown/manual.pdf")
        
        with patch('INAGENT.data_tools.auto_document_integration.load_function_structure_index', return_value=test_index):
            result = infer_module_from_path(pdf_path)
            assert result == []


class TestInferDocumentType:
    """测试 infer_document_type()"""
    
    def test_path_contains_cli(self):
        """TC2.3.1: 路径包含 'cli'，返回 'cli'"""
        pdf_path = Path("knowledge_base/cli/reference.pdf")
        result = infer_document_type(pdf_path, [])
        assert result == "cli"
    
    def test_content_contains_cli_syntax(self):
        """TC2.3.3: 内容包含 CLI 语法，返回 'cli'"""
        pdf_path = Path("knowledge_base/manual.pdf")
        content_blocks = [
            {"page_content": "slb real <ip> [port]", "text": "slb real <ip> [port]"}
        ]
        result = infer_document_type(pdf_path, content_blocks)
        assert result == "cli"
    
    def test_default_app(self):
        """TC2.3.5: 无法推断，返回 'app'（默认）"""
        pdf_path = Path("knowledge_base/manual.pdf")
        content_blocks = [{"page_content": "普通文本", "text": "普通文本"}]
        result = infer_document_type(pdf_path, content_blocks)
        assert result == "app"


class TestExtractDocumentStatistics:
    """测试 extract_document_statistics()"""
    
    def test_extract_statistics(self):
        """TC2.4.1: 正常提取统计信息"""
        content_blocks = [
            {
                "metadata": {"product_module": "SLB", "protocol_type": ["HTTP"]},
                "page_content": "test"
            },
            {
                "metadata": {"product_module": "SLB", "step_type": "backend_servers"},
                "page_content": "test"
            }
        ]
        
        result = extract_document_statistics(content_blocks)
        assert result["product_modules"]["SLB"] == 2
        assert result["protocol_types"]["HTTP"] == 1
        assert result["step_types"]["backend_servers"] == 1
    
    def test_protocol_type_as_list(self):
        """TC2.4.4: protocol_type 是列表"""
        content_blocks = [
            {
                "metadata": {"protocol_type": ["HTTP", "HTTPS"]},
                "page_content": "test"
            }
        ]
        
        result = extract_document_statistics(content_blocks)
        assert result["protocol_types"]["HTTP"] == 1
        assert result["protocol_types"]["HTTPS"] == 1
    
    def test_protocol_type_as_string(self):
        """TC2.4.5: protocol_type 是字符串"""
        content_blocks = [
            {
                "metadata": {"protocol_type": "HTTP"},
                "page_content": "test"
            }
        ]
        
        result = extract_document_statistics(content_blocks)
        assert result["protocol_types"]["HTTP"] == 1


class TestAutoIdentifyDocumentModule:
    """测试 auto_identify_document_module()"""
    
    def test_identify_from_content_stats(self):
        """TC2.5.1: 从内容统计识别模块（高置信度）"""
        pdf_path = Path("test.pdf")
        content_blocks = [
            {
                "metadata": {"product_module": "SLB", "protocol_type": ["HTTP"]},
                "page_content": "SLB configuration"
            }
        ]
        test_index = {
            "metadata_statistics": {
                "product_modules": {"SLB": {"keywords": ["slb"]}},
                "protocol_types": {"HTTP": {"keywords": ["http"]}}
            }
        }
        
        with patch('INAGENT.data_tools.auto_document_integration.load_function_structure_index', return_value=test_index):
            result = auto_identify_document_module(pdf_path, content_blocks, test_index)
            assert "SLB" in result["product_modules"]
            assert result["confidence"] > 0.8
    
    def test_discover_new_module(self):
        """TC2.5.3: 发现新模块"""
        pdf_path = Path("webui.pdf")
        content_blocks = [
            {
                "metadata": {"product_module": "webui"},
                "page_content": "webui configuration"
            }
        ]
        test_index = {
            "metadata_statistics": {
                "product_modules": {}
            }
        }
        
        with patch('INAGENT.data_tools.auto_document_integration.load_function_structure_index', return_value=test_index):
            result = auto_identify_document_module(pdf_path, content_blocks, test_index)
            assert "webui" in result["new_modules_discovered"]


class TestMergeStatistics:
    """测试 merge_statistics()"""
    
    def test_merge_statistics(self):
        """TC2.6.1: 合并新统计到现有统计"""
        existing = {
            "product_modules": {"SLB": 100},
            "protocol_types": {"HTTP": 50}
        }
        new = {
            "product_modules": {"SLB": 50, "LLB": 30},
            "protocol_types": {"HTTP": 20, "TCP": 10}
        }
        
        result = merge_statistics(existing, new)
        assert result["product_modules"]["SLB"] == 150
        assert result["product_modules"]["LLB"] == 30
        assert result["protocol_types"]["HTTP"] == 70
        assert result["protocol_types"]["TCP"] == 10


class TestIncrementallyUpdateFunctionIndex:
    """测试 incrementally_update_function_index()"""
    
    def test_update_existing_index(self, tmp_path):
        """TC2.7.1: 索引文件存在，增量更新"""
        index_path = tmp_path / "function_structure_index.json"
        existing_index = {
            "version": "2.0",
            "metadata_statistics": {
                "product_modules": {"SLB": {"count": 100, "keywords": []}}
            },
            "modules": {}
        }
        index_path.write_text(json.dumps(existing_index, ensure_ascii=False))
        
        new_documents = [
            {
                "pdf_path": Path("test.pdf"),
                "json_path": Path("test.json"),
                "document_metadata": {
                    "document_statistics": {
                        "product_modules": {"LLB": 30},
                        "protocol_types": {"HTTP": 20}
                    }
                }
            }
        ]
        
        result = incrementally_update_function_index(new_documents, index_path)
        assert "LLB" in result["metadata_statistics"]["product_modules"]
        assert result["metadata_statistics"]["product_modules"]["SLB"]["count"] == 100
    
    def test_create_new_index(self, tmp_path):
        """TC2.7.2: 索引文件不存在，创建新索引"""
        index_path = tmp_path / "nonexistent.json"
        
        new_documents = [
            {
                "pdf_path": Path("test.pdf"),
                "json_path": Path("test.json"),
                "document_metadata": {
                    "document_statistics": {
                        "product_modules": {"SLB": 50}
                    }
                }
            }
        ]
        
        result = incrementally_update_function_index(new_documents, index_path)
        assert result["version"] == "2.0"
        assert "SLB" in result["metadata_statistics"]["product_modules"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
