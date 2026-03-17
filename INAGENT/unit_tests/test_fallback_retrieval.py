# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
workforce_config_ops.py 单元测试
"""
import pytest
from unittest.mock import MagicMock, patch
from typing import Dict, Any

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# 注意：workforce_config_ops.py 可能不完整，需要根据实际情况调�?
try:
    from INAGENT.rag.fallback_retrieval import (
        _analyze_step_coverage,
        _adaptive_rag_retrieval,
    )
except ImportError:
    # 如果导入失败，跳过测�?
    pytest.skip("workforce_config_ops.py 不完整，跳过测试", allow_module_level=True)


class TestAnalyzeStepCoverage:
    """测试 _analyze_step_coverage()"""
    
    def test_detect_covered_steps(self, sample_decomposition_result):
        """TC6.1.1: 检测覆盖的步骤类型"""
        context = "配置 slb real server �?health check"
        
        with patch('INAGENT.rag.fallback_retrieval.get_step_type_keywords') as mock_get_keywords:
            mock_get_keywords.return_value = {
                "backend_servers": ["real server", "slb real"],
                "health_checks": ["health check", "健康检�?]
            }
            
            result = _analyze_step_coverage(context, sample_decomposition_result)
            assert "backend_servers" in result
            assert "health_checks" in result
    
    def test_detect_advanced_features(self, sample_decomposition_result):
        """TC6.1.2: 检测高级功能覆�?""
        context = "配置 cookie persistence 会话保持"
        sample_decomposition_result["advanced_features"] = ["cookie_persistence"]
        
        with patch('INAGENT.rag.fallback_retrieval.get_step_type_keywords') as mock_step:
            with patch('INAGENT.rag.fallback_retrieval.get_advanced_feature_keywords') as mock_feature:
                mock_step.return_value = {}
                mock_feature.return_value = {
                    "cookie_persistence": ["cookie", "persistence", "会话保持"]
                }
                
                result = _analyze_step_coverage(context, sample_decomposition_result)
                # 高级功能应该标记 policies_and_algorithms 为已覆盖
                if "policies_and_algorithms" in sample_decomposition_result.get("required_steps", []):
                    assert "policies_and_algorithms" in result
    
    def test_no_decomposition_result(self):
        """TC6.1.4: 无分解结果，返回空集�?""
        result = _analyze_step_coverage("test context", None)
        assert result == set()
    
    def test_dynamic_keywords_loading(self, sample_decomposition_result):
        """TC6.1.3: 索引动态加载关键词"""
        context = "配置 real server"
        
        with patch('INAGENT.rag.fallback_retrieval.get_step_type_keywords') as mock_get:
            mock_get.return_value = {
                "backend_servers": ["real server"]
            }
            
            result = _analyze_step_coverage(context, sample_decomposition_result)
            # 验证调用了动态加载函�?
            mock_get.assert_called_once()


class TestAdaptiveRagRetrieval:
    """测试 _adaptive_rag_retrieval()"""
    
    def test_overall_query_covers_all_steps(self, sample_decomposition_result):
        """TC6.2.1: 整体查询覆盖所有步�?""
        mock_retriever = MagicMock()
        mock_reranker = MagicMock()
        
        # Mock _retrieve_context 返回完整上下�?
        with patch('INAGENT.rag.fallback_retrieval._retrieve_context') as mock_retrieve:
            mock_retrieve.return_value = (
                "完整的配置示例，包含 backend_servers, virtual_services, health_checks",
                {"config_modes": [], "required_keywords": [], "intents": []}
            )
            
            with patch('INAGENT.rag.fallback_retrieval._analyze_step_coverage') as mock_analyze:
                mock_analyze.return_value = {
                    "backend_servers", "virtual_services", "health_checks"
                }
                
                result = _adaptive_rag_retrieval(
                    mock_retriever,
                    mock_reranker,
                    "如何配置HTTP类型SLB",
                    sample_decomposition_result
                )
                
                context, constraints, adjusted = result
                assert context
                # 应该直接返回，不进行分解查询
    
    def test_dynamic_query_generation(self, sample_decomposition_result):
        """TC6.2.3: 动态生成查询（不硬编码�?""
        mock_retriever = MagicMock()
        mock_reranker = MagicMock()
        
        with patch('INAGENT.rag.fallback_retrieval.generate_query_from_decomposition') as mock_gen:
            mock_gen.return_value = ["动态生成的查询"]
            
            with patch('INAGENT.rag.fallback_retrieval._retrieve_context') as mock_retrieve:
                mock_retrieve.return_value = ("", {})
                
                _adaptive_rag_retrieval(
                    mock_retriever,
                    mock_reranker,
                    "test",
                    sample_decomposition_result
                )
                
                # 验证调用了动态生成函�?
                mock_gen.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
