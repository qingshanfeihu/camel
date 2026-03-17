# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
index_utils.py 单元测试
"""
import json
import pytest
from pathlib import Path
from unittest.mock import mock_open, patch, MagicMock
from typing import Dict, Any

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from INAGENT.utils.index_utils import (
    load_function_structure_index,
    get_step_type_keywords,
    get_product_module_keywords,
    get_protocol_type_keywords,
    get_advanced_feature_keywords,
    generate_query_from_decomposition,
    detect_step_type_from_text,
    detect_product_module_from_text,
)


class TestLoadFunctionStructureIndex:
    """测试 load_function_structure_index()"""
    
    def test_load_existing_index(self, tmp_path):
        """TC1.1.1: 索引文件存在，正常加载"""
        index_file = tmp_path / "function_structure_index.json"
        test_index = {
            "version": "2.0",
            "modules": {"SLB": {"keywords": ["slb"]}},
            "scenarios": {}
        }
        index_file.write_text(json.dumps(test_index, ensure_ascii=False))
        
        result = load_function_structure_index(index_file)
        assert result == test_index
    
    def test_load_nonexistent_index(self, tmp_path):
        """TC1.1.2: 索引文件不存在，返回空字典"""
        index_file = tmp_path / "nonexistent.json"
        result = load_function_structure_index(index_file)
        assert result == {}
    
    def test_load_invalid_json(self, tmp_path, caplog):
        """TC1.1.3: 索引文件格式错误，返回空字典并记录警告"""
        index_file = tmp_path / "invalid.json"
        index_file.write_text("invalid json content")
        
        result = load_function_structure_index(index_file)
        assert result == {}
        assert "加载功能结构索引失败" in caplog.text


class TestGetStepTypeKeywords:
    """测试 get_step_type_keywords()"""
    
    def test_get_keywords_from_index(self):
        """TC1.2.1: 索引中有步骤类型，返回关键词映射"""
        test_index = {
            "metadata_statistics": {
                "step_types": {
                    "backend_servers": {"keywords": ["real server", "服务组"]},
                    "virtual_services": {"keywords": ["virtual service"]}
                }
            }
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=test_index):
            result = get_step_type_keywords()
            assert "backend_servers" in result
            assert result["backend_servers"] == ["real server", "服务组"]
            assert result["virtual_services"] == ["virtual service"]
    
    def test_empty_index(self):
        """TC1.2.2: 索引中没有步骤类型，返回空字典"""
        test_index = {"metadata_statistics": {}}
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=test_index):
            result = get_step_type_keywords()
            assert result == {}
    
    def test_auto_load_index(self):
        """TC1.2.3: 传入 None，自动加载索引"""
        test_index = {
            "metadata_statistics": {
                "step_types": {
                    "backend_servers": {"keywords": ["real server"]}
                }
            }
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=test_index):
            result = get_step_type_keywords(None)
            assert "backend_servers" in result


class TestGetProductModuleKeywords:
    """测试 get_product_module_keywords()"""
    
    def test_merge_from_both_sources(self):
        """TC1.3.3: 两个来源合并去重"""
        test_index = {
            "metadata_statistics": {
                "product_modules": {
                    "SLB": {"keywords": ["slb", "负载均衡"]}
                }
            },
            "modules": {
                "SLB": {"keywords": ["服务器负载均衡"]},
                "LLB": {"keywords": ["llb"]}
            }
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=test_index):
            result = get_product_module_keywords()
            assert "SLB" in result
            assert "LLB" in result
            # SLB 的关键词应该合并
            assert len(result["SLB"]) >= 2


class TestGenerateQueryFromDecomposition:
    """测试 generate_query_from_decomposition()"""
    
    def test_generate_with_module_and_protocol(self):
        """TC1.5.1: 有模块和协议，生成完整查询"""
        decomposition = {
            "product_modules": ["SLB"],
            "protocol_type": ["HTTP"]
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value={}):
            result = generate_query_from_decomposition(decomposition)
            assert len(result) >= 3
            assert any("HTTP" in q and "SLB" in q for q in result)
    
    def test_generate_with_multiple_modules(self):
        """TC1.5.3: 多个模块和协议，生成所有组合"""
        decomposition = {
            "product_modules": ["SLB", "LLB"],
            "protocol_type": ["HTTP", "TCP"]
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value={}):
            result = generate_query_from_decomposition(decomposition)
            # 2 模块 × 2 协议 × 3 查询格式 = 12 个查询
            assert len(result) >= 12
    
    def test_generate_with_string_protocol(self):
        """TC1.5.5: protocol_type 是字符串，自动转换为列表"""
        decomposition = {
            "product_modules": ["SLB"],
            "protocol_type": "HTTP"  # 字符串
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value={}):
            result = generate_query_from_decomposition(decomposition)
            assert len(result) >= 3


class TestDetectStepTypeFromText:
    """测试 detect_step_type_from_text()"""
    
    def test_detect_single_step_type(self):
        """TC1.6.1: 文本匹配单个步骤类型"""
        test_index = {
            "metadata_statistics": {
                "step_types": {
                    "backend_servers": {"keywords": ["real server", "服务组"]},
                    "health_checks": {"keywords": ["health check", "健康检查"]}
                }
            }
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=test_index):
            result = detect_step_type_from_text("配置 real server")
            assert result == "backend_servers"
    
    def test_detect_multiple_step_types(self):
        """TC1.6.2: 文本匹配多个步骤类型，返回匹配最多的"""
        test_index = {
            "metadata_statistics": {
                "step_types": {
                    "backend_servers": {"keywords": ["real server"]},
                    "health_checks": {"keywords": ["health check", "健康检查", "monitor"]}
                }
            }
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=test_index):
            result = detect_step_type_from_text("配置 real server 和 health check 健康检查")
            # health_checks 匹配更多关键词
            assert result == "health_checks"
    
    def test_no_match(self):
        """TC1.6.3: 文本不匹配任何步骤类型"""
        test_index = {
            "metadata_statistics": {
                "step_types": {
                    "backend_servers": {"keywords": ["real server"]}
                }
            }
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=test_index):
            result = detect_step_type_from_text("无关文本")
            assert result is None


class TestDetectProductModuleFromText:
    """测试 detect_product_module_from_text()"""
    
    def test_detect_single_module(self):
        """TC1.7.1: 文本匹配单个模块"""
        test_index = {
            "metadata_statistics": {
                "product_modules": {
                    "SLB": {"keywords": ["slb", "负载均衡"]}
                }
            }
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=test_index):
            result = detect_product_module_from_text("配置 SLB 负载均衡")
            assert "SLB" in result
    
    def test_detect_multiple_modules(self):
        """TC1.7.2: 文本匹配多个模块"""
        test_index = {
            "metadata_statistics": {
                "product_modules": {
                    "SLB": {"keywords": ["slb"]},
                    "LLB": {"keywords": ["llb"]}
                }
            }
        }
        
        with patch('INAGENT.utils.index_utils.load_function_structure_index', return_value=test_index):
            result = detect_product_module_from_text("配置 SLB 和 LLB")
            assert len(result) >= 2
            assert "SLB" in result
            assert "LLB" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
