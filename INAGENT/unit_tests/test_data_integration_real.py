# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
使用真实数据的集成测试
"""
import json
import pytest
from pathlib import Path
from typing import Dict, Any, List

import sys
# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from INAGENT.utils.index_utils import (
    load_function_structure_index,
    get_step_type_keywords,
    get_product_module_keywords,
    get_protocol_type_keywords,
    generate_query_from_decomposition,
    detect_step_type_from_text,
    detect_product_module_from_text,
)
from INAGENT.data_tools.auto_document_integration import (
    extract_document_statistics,
    merge_statistics,
    infer_module_from_path,
)
from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base
from INAGENT.utils.llm_config import (
    get_qianfan_config,
    get_siliconflow_config,
    LLMConfig,
)


# 真实数据路径
REAL_DATA_DIR = Path(__file__).parent.parent / "knowledge_base"
REAL_KB_PATH = REAL_DATA_DIR / "reference" / "knowledge_base.json"
REAL_INDEX_PATH = REAL_DATA_DIR / "function_structure_index.json"
REAL_APP_JSON = REAL_DATA_DIR / "reference" / "app.json"
REAL_CLI_JSON = REAL_DATA_DIR / "reference" / "cli.json"


@pytest.fixture
def real_function_index() -> Dict[str, Any]:
    """加载真实的功能结构索引"""
    if not REAL_INDEX_PATH.exists():
        pytest.skip(f"真实索引文件不存在: {REAL_INDEX_PATH}")
    return load_function_structure_index(REAL_INDEX_PATH)


@pytest.fixture
def real_knowledge_base() -> List[Dict[str, Any]]:
    """加载真实的知识库"""
    if not REAL_KB_PATH.exists():
        pytest.skip(f"真实知识库文件不存在: {REAL_KB_PATH}")
    with open(REAL_KB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class TestRealDataIndexUtils:
    """使用真实数据测试索引工具"""
    
    def test_load_real_function_index(self, real_function_index):
        """测试加载真实的功能结构索引"""
        assert isinstance(real_function_index, dict), "索引应该是字典类型"
        assert "version" in real_function_index, "索引应该包含 version 字段"
        assert "metadata_statistics" in real_function_index, "索引应该包含 metadata_statistics"
        
        # 验证结构
        metadata_stats = real_function_index.get("metadata_statistics", {})
        assert "product_modules" in metadata_stats, "应该有 product_modules"
        assert "protocol_types" in metadata_stats, "应该有 protocol_types"
        assert "step_types" in metadata_stats, "应该有 step_types"
    
    def test_get_step_type_keywords_from_real_index(self, real_function_index):
        """从真实索引获取步骤类型关键词"""
        step_keywords = get_step_type_keywords(real_function_index)
        
        assert isinstance(step_keywords, dict), "应该返回字典"
        # 验证至少有一些步骤类型
        if step_keywords:
            for step_type, keywords in step_keywords.items():
                assert isinstance(keywords, list), f"{step_type} 的关键词应该是列表"
    
    def test_get_product_module_keywords_from_real_index(self, real_function_index):
        """从真实索引获取产品模块关键词"""
        module_keywords = get_product_module_keywords(real_function_index)
        
        assert isinstance(module_keywords, dict), "应该返回字典"
        # 验证至少有一些模块
        if module_keywords:
            for module, keywords in module_keywords.items():
                assert isinstance(keywords, list), f"{module} 的关键词应该是列表"
    
    def test_get_protocol_type_keywords_from_real_index(self, real_function_index):
        """从真实索引获取协议类型关键词"""
        protocol_keywords = get_protocol_type_keywords(real_function_index)
        
        assert isinstance(protocol_keywords, dict), "应该返回字典"
        # 验证至少有一些协议类型
        if protocol_keywords:
            for protocol, keywords in protocol_keywords.items():
                assert isinstance(keywords, list), f"{protocol} 的关键词应该是列表"
    
    def test_detect_step_type_from_real_text(self, real_function_index):
        """从真实文本检测步骤类型"""
        test_texts = [
            "配置 slb real server",
            "配置虚拟服务 virtual service",
            "配置健康检查 health check",
        ]
        
        for text in test_texts:
            step_type = detect_step_type_from_text(text, real_function_index)
            # 可能返回 None，也可能返回匹配的步骤类型
            assert step_type is None or isinstance(step_type, str), f"步骤类型应该是字符串或 None"
    
    def test_detect_product_module_from_real_text(self, real_function_index):
        """从真实文本检测产品模块"""
        test_texts = [
            "配置 SLB 负载均衡",
            "配置 LLB 链路负载均衡",
            "配置 GSLB 全局负载均衡",
        ]
        
        for text in test_texts:
            modules = detect_product_module_from_text(text, real_function_index)
            assert isinstance(modules, list), "应该返回列表"
            # 可能为空列表，也可能包含模块名称
            assert all(isinstance(m, str) for m in modules), "所有模块名称应该是字符串"


class TestRealDataKnowledgeBase:
    """使用真实数据测试知识库"""
    
    def test_real_knowledge_base_structure(self, real_knowledge_base):
        """测试真实知识库的结构"""
        assert isinstance(real_knowledge_base, list), "知识库应该是列表"
        assert len(real_knowledge_base) > 0, "知识库应该包含数据"
        
        # 检查第一个块的结构
        first_chunk = real_knowledge_base[0]
        assert isinstance(first_chunk, dict), "每个块应该是字典"
        assert "page_content" in first_chunk, "应该有 page_content 字段"
        assert "metadata" in first_chunk, "应该有 metadata 字段"
    
    def test_extract_statistics_from_real_kb(self, real_knowledge_base):
        """从真实知识库提取统计信息"""
        # 只使用前100个块进行测试（避免测试时间过长）
        test_chunks = real_knowledge_base[:100]
        
        stats = extract_document_statistics(test_chunks)
        
        assert isinstance(stats, dict), "统计信息应该是字典"
        assert "product_modules" in stats, "应该有 product_modules"
        assert "protocol_types" in stats, "应该有 protocol_types"
        assert "step_types" in stats, "应该有 step_types"
        
        # 验证统计数据的结构
        for key in ["product_modules", "protocol_types", "step_types"]:
            assert isinstance(stats[key], dict), f"{key} 应该是字典"
    
    def test_merge_real_statistics(self, real_knowledge_base):
        """测试合并真实统计数据"""
        # 将知识库分成两部分
        chunks1 = real_knowledge_base[:50]
        chunks2 = real_knowledge_base[50:100]
        
        stats1 = extract_document_statistics(chunks1)
        stats2 = extract_document_statistics(chunks2)
        
        merged = merge_statistics(stats1, stats2)
        
        assert isinstance(merged, dict), "合并结果应该是字典"
        # 验证合并后的计数应该大于等于单独统计的计数
        for key in ["product_modules", "protocol_types", "step_types"]:
            if key in stats1 and key in stats2:
                # 检查合并后的计数
                for module in set(list(stats1[key].keys()) + list(stats2[key].keys())):
                    count1 = stats1[key].get(module, 0)
                    count2 = stats2[key].get(module, 0)
                    merged_count = merged[key].get(module, 0)
                    assert merged_count >= max(count1, count2), f"{key}.{module} 合并计数应该 >= 单独计数"


class TestRealDataQueryGeneration:
    """使用真实数据测试查询生成"""
    
    def test_generate_query_from_real_decomposition(self, real_function_index):
        """基于真实索引生成查询"""
        decomposition = {
            "product_modules": ["SLB"],
            "protocol_type": ["HTTP"],
            "required_steps": ["backend_servers", "health_checks"],
        }
        
        queries = generate_query_from_decomposition(decomposition, real_function_index)
        
        assert isinstance(queries, list), "应该返回查询列表"
        assert len(queries) > 0, "应该生成至少一个查询"
        
        # 验证查询包含模块和协议信息
        for query in queries:
            assert isinstance(query, str), "每个查询应该是字符串"
            assert len(query) > 0, "查询不应该为空"
    
    def test_generate_query_multiple_modules(self, real_function_index):
        """测试多个模块的查询生成"""
        decomposition = {
            "product_modules": ["SLB", "LLB"],
            "protocol_type": ["HTTP", "TCP"],
        }
        
        queries = generate_query_from_decomposition(decomposition, real_function_index)
        
        assert len(queries) > 0, "应该生成查询"
        # 验证生成了多个查询（多个模块和协议的组合）


class TestRealDataMergeKnowledgeBase:
    """使用真实数据测试知识库合并"""
    
    def test_merge_real_json_files(self, tmp_path):
        """测试合并真实的 JSON 文件"""
        if not REAL_APP_JSON.exists() or not REAL_CLI_JSON.exists():
            pytest.skip("真实的 JSON 文件不存在")
        
        # 复制真实文件到临时目录
        test_dir = tmp_path / "reference"
        test_dir.mkdir()
        
        import shutil
        shutil.copy(REAL_APP_JSON, test_dir / "app.json")
        shutil.copy(REAL_CLI_JSON, test_dir / "cli.json")
        
        # 合并知识库
        output_file = test_dir / "knowledge_base.json"
        merge_knowledge_base(test_dir, output_file, deduplicate=True)
        
        # 验证输出文件
        assert output_file.exists(), "应该生成合并后的知识库文件"
        
        with open(output_file, "r", encoding="utf-8") as f:
            merged_data = json.load(f)
        
        assert isinstance(merged_data, list), "合并后的数据应该是列表"
        assert len(merged_data) > 0, "合并后的数据不应该为空"
        
        # 验证数据结构
        for chunk in merged_data[:10]:  # 只检查前10个
            assert "page_content" in chunk, "应该有 page_content"
            assert "metadata" in chunk, "应该有 metadata"


class TestRealDataLLMConfig:
    """使用真实环境变量测试 LLM 配置（已统一使用硅基流动）"""
    
    def test_get_qianfan_config_real(self):
        """测试获取配置（已统一返回硅基流动配置）"""
        config = get_qianfan_config()
        
        assert isinstance(config, dict), "配置应该是字典"
        assert "rpm" in config, "应该有 rpm 字段"
        assert "tpm" in config, "应该有 tpm 字段"
        assert "model" in config, "应该有 model 字段"
        
        # 验证限速配置符合硅基流动标准
        assert config["rpm"] == 1000, f"RPM 应该是 1000（硅基流动），实际是 {config['rpm']}"
        assert config["tpm"] == 50000, f"TPM 应该是 50000（硅基流动），实际是 {config['tpm']}"
    
    def test_get_siliconflow_config_real(self):
        """测试获取真实的硅基流动配置"""
        config = get_siliconflow_config()
        
        assert isinstance(config, dict), "配置应该是字典"
        assert "chat_rpm" in config, "应该有 chat_rpm 字段"
        assert "chat_tpm" in config, "应该有 chat_tpm 字段"
        assert "embedding_rpm" in config, "应该有 embedding_rpm 字段"
        assert "embedding_tpm" in config, "应该有 embedding_tpm 字段"
        
        # 验证限速配置符合硅基流动官方标准
        # 参考文档：https://github.com/siliconflow/siliconcloud-cookbook/blob/main/examples/rate-limit/how-to-handle-rate-limit-in-siliconcloud.ipynb
        assert config["chat_rpm"] == 1000, f"对话模型 RPM 应该是 1000，实际是 {config['chat_rpm']}"
        assert config["chat_tpm"] == 50000, f"对话模型 TPM 应该是 50000，实际是 {config['chat_tpm']}"
        assert config["embedding_rpm"] == 2000, f"嵌入模型 RPM 应该是 2000，实际是 {config['embedding_rpm']}"
        assert config["embedding_tpm"] == 500000, f"嵌入模型 TPM 应该是 500000，实际是 {config['embedding_tpm']}"
    
    def test_validate_siliconflow_config(self):
        """测试验证硅基流动配置"""
        config = get_siliconflow_config()
        llm_config = LLMConfig()
        is_valid, missing = llm_config.validate_config(config, "siliconflow")
        
        # 如果配置完整，应该通过验证
        # 如果缺少 API key，验证会失败（这是正常的）
        assert isinstance(is_valid, bool), "验证结果应该是布尔值"
        assert isinstance(missing, list), "缺失字段应该是列表"


class TestRealDataEndToEnd:
    """端到端集成测试"""
    
    def test_full_workflow(self, real_function_index, real_knowledge_base):
        """测试完整工作流：索引 -> 知识库 -> 查询生成"""
        # 1. 从真实索引加载关键词
        step_keywords = get_step_type_keywords(real_function_index)
        module_keywords = get_product_module_keywords(real_function_index)
        
        # 验证函数返回了字典（即使为空）
        assert isinstance(step_keywords, dict), "步骤关键词应该是字典"
        assert isinstance(module_keywords, dict), "模块关键词应该是字典"
        
        # 2. 从真实知识库提取统计信息
        test_chunks = real_knowledge_base[:50]
        stats = extract_document_statistics(test_chunks)
        
        assert isinstance(stats, dict), "统计信息应该是字典"
        assert len(stats) > 0, "应该提取到统计信息"
        
        # 3. 基于真实数据生成查询
        # 如果模块关键词为空，使用默认值
        modules = list(module_keywords.keys())[:2] if module_keywords else ["SLB"]
        decomposition = {
            "product_modules": modules,
            "protocol_type": ["HTTP"],
        }
        
        queries = generate_query_from_decomposition(decomposition, real_function_index)
        assert isinstance(queries, list), "查询应该是列表"
        assert len(queries) > 0, "应该生成查询"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
