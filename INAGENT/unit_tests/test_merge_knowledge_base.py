# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
merge_knowledge_base.py 单元测试
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base


class TestMergeKnowledgeBase:
    """测试 merge_knowledge_base()"""
    
    def test_merge_multiple_files(self, temp_dir, caplog):
        """TC3.1.1: 正常合并多个 JSON 文件"""
        reference_dir = temp_dir / "reference"
        reference_dir.mkdir()
        output_file = reference_dir / "knowledge_base.json"
        
        # 创建测试文件
        app_json = reference_dir / "app.json"
        app_json.write_text(json.dumps([
            {"page_content": "content 1", "metadata": {"source_file": "app.json"}},
            {"page_content": "content 2", "metadata": {"source_file": "app.json"}}
        ], ensure_ascii=False))
        
        cli_json = reference_dir / "cli.json"
        cli_json.write_text(json.dumps([
            {"page_content": "content 3", "metadata": {"source_file": "cli.json"}}
        ], ensure_ascii=False))
        
        merge_knowledge_base(reference_dir, output_file, deduplicate=True)
        
        assert output_file.exists()
        with open(output_file, "r", encoding="utf-8") as f:
            result = json.load(f)
            assert len(result) == 3
    
    def test_deduplicate(self, temp_dir):
        """TC3.1.2: 去重功能正常"""
        reference_dir = temp_dir / "reference"
        reference_dir.mkdir()
        output_file = reference_dir / "knowledge_base.json"
        
        # 创建包含重复内容的文件
        app_json = reference_dir / "app.json"
        app_json.write_text(json.dumps([
            {
                "page_content": "duplicate content",
                "metadata": {"source_file": "app.json", "page_idx": 1, "block_id": 1}
            }
        ], ensure_ascii=False))
        
        cli_json = reference_dir / "cli.json"
        cli_json.write_text(json.dumps([
            {
                "page_content": "duplicate content",
                "metadata": {"source_file": "cli.json", "page_idx": 1, "block_id": 1}
            }
        ], ensure_ascii=False))
        
        merge_knowledge_base(reference_dir, output_file, deduplicate=True)
        
        with open(output_file, "r", encoding="utf-8") as f:
            result = json.load(f)
            # 应该只保留一个（去重）
            assert len(result) == 1
    
    def test_skip_invalid_file(self, temp_dir, caplog):
        """TC3.1.3: 文件格式错误，跳过并继续"""
        reference_dir = temp_dir / "reference"
        reference_dir.mkdir()
        output_file = reference_dir / "knowledge_base.json"
        
        # 创建有效文件
        app_json = reference_dir / "app.json"
        app_json.write_text(json.dumps([
            {"page_content": "content 1", "metadata": {}}
        ], ensure_ascii=False))
        
        # 创建无效文件
        invalid_json = reference_dir / "invalid.json"
        invalid_json.write_text("invalid json content")
        
        merge_knowledge_base(reference_dir, output_file, deduplicate=True)
        
        # 应该成功处理有效文件
        assert output_file.exists()
        with open(output_file, "r", encoding="utf-8") as f:
            result = json.load(f)
            assert len(result) == 1
    
    def test_exclude_knowledge_base_json(self, temp_dir):
        """TC3.1.5: 排除 knowledge_base.json 本身"""
        reference_dir = temp_dir / "reference"
        reference_dir.mkdir()
        output_file = reference_dir / "knowledge_base.json"
        
        # 创建 knowledge_base.json（应该被排除）
        kb_json = reference_dir / "knowledge_base.json"
        kb_json.write_text(json.dumps([
            {"page_content": "old content", "metadata": {}}
        ], ensure_ascii=False))
        
        # 创建其他文件
        app_json = reference_dir / "app.json"
        app_json.write_text(json.dumps([
            {"page_content": "new content", "metadata": {}}
        ], ensure_ascii=False))
        
        merge_knowledge_base(reference_dir, output_file, deduplicate=True)
        
        with open(output_file, "r", encoding="utf-8") as f:
            result = json.load(f)
            # 应该只包含 app.json 的内容，不包含 knowledge_base.json
            assert len(result) == 1
            assert result[0]["page_content"] == "new content"
    
    def test_metadata_statistics(self, temp_dir, caplog):
        """TC3.1.6: metadata 完整性统计正确"""
        reference_dir = temp_dir / "reference"
        reference_dir.mkdir()
        output_file = reference_dir / "knowledge_base.json"
        
        # 创建包含不同 metadata 的文件
        app_json = reference_dir / "app.json"
        app_json.write_text(json.dumps([
            {
                "page_content": "content 1",
                "metadata": {
                    "product_module": "SLB",
                    "protocol_type": ["HTTP"],
                    "step_type": "backend_servers",
                    "scenario_id": "SLB_HTTP_FULL_CONFIG"
                }
            },
            {
                "page_content": "content 2",
                "metadata": {
                    "product_module": "SLB"
                    # 缺少其他字段
                }
            }
        ], ensure_ascii=False))
        
        merge_knowledge_base(reference_dir, output_file, deduplicate=True)
        
        # 检查日志中的统计信息
        assert "有 product_module" in caplog.text
        assert "有 protocol_type" in caplog.text
        assert "有 step_type" in caplog.text
        assert "有 scenario_id" in caplog.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
