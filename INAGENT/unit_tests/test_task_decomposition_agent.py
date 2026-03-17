# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
task_decomposition_agent.py 单元测试
"""
import pytest
from unittest.mock import MagicMock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from INAGENT.agents.task_decomposition_agent import (
    build_task_decomposition_agent,
    build_task_decomposition_prompt,
)


class TestBuildTaskDecompositionAgent:
    """测试 build_task_decomposition_agent()"""
    
    def test_create_agent(self):
        """TC7.1.1: 正常创建 Agent"""
        mock_model = MagicMock()
        agent = build_task_decomposition_agent(mock_model)
        assert agent is not None
        assert agent.model == mock_model
    
    def test_prompt_contains_index_info(self, sample_function_index):
        """TC7.1.2: Prompt 包含索引信息"""
        mock_model = MagicMock()
        agent = build_task_decomposition_agent(mock_model)
        
        # 检查 system message 内容
        system_content = agent.system_message.content
        # 应该包含动态加载的说明，不硬编码模块名称
        assert "function_structure_index" in system_content.lower() or "索引" in system_content
        assert "SLB" not in system_content or "示例" in system_content  # 如果是示例，可以接受


class TestBuildTaskDecompositionPrompt:
    """测试 build_task_decomposition_prompt()"""
    
    def test_include_index_info(self, sample_function_index):
        """TC7.2.1: 包含索引信息"""
        job_content = "如何配置HTTP类型SLB服务"
        
        prompt = build_task_decomposition_prompt(job_content, sample_function_index)
        
        assert "配置逻辑树" in prompt or "场景" in prompt
        assert job_content in prompt
    
    def test_empty_index_fallback(self):
        """TC7.2.2: 索引为空，降级处理"""
        job_content = "如何配置HTTP类型SLB服务"
        
        prompt = build_task_decomposition_prompt(job_content, None)
        
        # 应该仍然能生成 prompt，即使没有索引
        assert job_content in prompt
        assert len(prompt) > 0
    
    def test_dynamic_scenario_loading(self, sample_function_index):
        """TC7.2.3: 动态加载场景列表"""
        job_content = "test"
        
        prompt = build_task_decomposition_prompt(job_content, sample_function_index)
        
        # 应该包含场景信息
        assert "SLB_HTTP_FULL_CONFIG" in prompt or "场景" in prompt
    
    def test_no_hardcoded_modules(self, sample_function_index):
        """TC7.2.4: 不硬编码模块名称"""
        job_content = "如何配置LLB链路负载均衡"
        
        prompt = build_task_decomposition_prompt(job_content, sample_function_index)
        
        # 检查是否硬编码了特定模块（除了作为示例）
        # 如果包含 "SLB"，应该是在场景示例中，而不是硬编码的描述
        if "SLB" in prompt:
            assert "示例" in prompt or "场景" in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
