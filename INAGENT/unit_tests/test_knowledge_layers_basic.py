# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
知识分层模块基础单元测试

目标：
1. 使用最小化的临时 CLI 参考数据，验证 CLIReferenceRetriever 的加载与检索。
2. 验证 KnowledgeRouter 在 test_write 模式下能整合 Rules + CLI 层结果。

说明：
- 测试使用临时文件，不依赖真实大文件（如 10k+ cli.json）和外部 LLM。
- 仅覆盖简单关键路径，便于快速回归。
"""
from pathlib import Path
import sys

# 与现有 unit_tests 的导入方式保持一致，并兼容 rootdir=INAGENT/unit_tests 场景
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.rag.cli_reference import CLIReferenceRetriever
from INAGENT.rag.knowledge_router import KnowledgeLayer, KnowledgeRouter, classify_for_mode
from INAGENT.rag.test_rules import TestRulesEngine


def test_cli_reference_retriever_basic_search(temp_dir: Path):
    """最小 CLI 数据：能按命令前缀和关键词检索。"""
    cli_file = temp_dir / "cli.json"
    cli_file.write_text(
        """
[
  {
    "page_content": "slb virtual VS1 10.0.0.1 80",
    "metadata": {
      "command_prefix": "slb virtual",
      "product_module": "SLB",
      "config_mode": "global",
      "step_type": "virtual_services",
      "section_title": "Virtual Service",
      "source_file": "cli.json"
    }
  },
  {
    "page_content": "slb health HC1 method get",
    "metadata": {
      "command_prefix": "slb health",
      "product_module": "SLB",
      "config_mode": "global",
      "step_type": "health_checks",
      "section_title": "Health Check",
      "source_file": "cli.json"
    }
  }
]
        """.strip(),
        encoding="utf-8",
    )

    retriever = CLIReferenceRetriever(cli_path=cli_file)

    assert retriever.entry_count == 2

    by_prefix = retriever.search_by_prefix("slb virtual")
    assert len(by_prefix) == 1
    assert "VS1" in by_prefix[0]["text"]

    by_keyword = retriever.search("health")
    assert len(by_keyword) >= 1
    assert any("health" in item["command_prefix"].lower() for item in by_keyword)


def test_knowledge_router_test_write_mode_includes_rules_and_cli(temp_dir: Path):
    """test_write 模式下，路由器应至少包含 Rules 和 CLI 上下文。"""
    # 构造最小 CLI 参考
    cli_file = temp_dir / "cli.json"
    cli_file.write_text(
        """
[
  {
    "page_content": "slb virtual VS1 10.0.0.1 80",
    "metadata": {
      "command_prefix": "slb virtual",
      "product_module": "SLB",
      "config_mode": "global",
      "step_type": "virtual_services",
      "section_title": "Virtual Service",
      "source_file": "cli.json"
    }
  }
]
        """.strip(),
        encoding="utf-8",
    )

    # 构造最小测试列表目录（TestRulesEngine 会按固定文件名加载）
    ref_dir = temp_dir / "reference"
    ref_dir.mkdir(parents=True, exist_ok=True)

    minimal_test_item = """
[
  {
    "page_content": "测试项: HTTP2 basic config\\n测试类型: Configuration\\n描述: 基础配置",
    "metadata": {
      "source_file": "sample.xlsx",
      "step_type": "basic_config",
      "product_module": "SLB"
    }
  }
]
    """.strip()

    for name in [
        "Test List HTTP2.0_phaseII.json",
        "Test List HTTP_2_new_cli.json",
        "Test list_HC.json",
        "Test_list_Cache_HTTP2.json",
        "HTTP2 test in phase II_jiangyz.json",
    ]:
        (ref_dir / name).write_text(minimal_test_item, encoding="utf-8")

    router = KnowledgeRouter(
        cli_retriever=CLIReferenceRetriever(cli_path=cli_file),
        rules_engine=TestRulesEngine(reference_dir=ref_dir),
        unified_rag=None,
        hybrid_retriever=None,
        reranker=None,
    )

    out = router.retrieve(
        query="请为 slb virtual 编写 configuration 测试用例",
        mode="test_write",
        product_module="SLB",
        max_context_chars=3000,
    )

    assert "rules" in out["layers_used"]
    assert "cli" in out["layers_used"]

    # 有规则上下文
    assert out["rules_context"]
    assert "测试类型" in out["rules_context"]

    # 有 CLI 检索结果
    assert out["cli_results"]
    assert any("slb virtual" in item["command_prefix"].lower() for item in out["cli_results"])

    # 合并上下文中含有分层标题
    assert "[测试规范]" in out["context"]
    assert "[CLI 参考]" in out["context"]


def test_classify_for_mode_mapping():
    """模式到知识层映射应与设计一致。"""
    layers = classify_for_mode("test_review")
    assert layers == [KnowledgeLayer.RULES, KnowledgeLayer.TEST]
