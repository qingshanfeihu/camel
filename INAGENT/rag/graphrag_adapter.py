# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
GraphRAG 适配器

将 Microsoft GraphRAG 适配到 INFOAGEN 项目，使用 LLM Gateway 作为 LLM 提供商。

主要功能：
1. 配置生成：生成适配 SiliconFlow 的 GraphRAG settings.yaml
2. 实体提取：从文档中提取实体和关系
3. 图索引构建：构建知识图谱索引
4. 检索集成：提供统一的检索接口

参考文档：
- https://microsoft.github.io/graphrag/config/overview/
- https://microsoft.github.io/graphrag/prompt_tuning/manual_prompt_tuning/
"""
import json
import logging
import os
import re
import yaml
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# GraphRAG 工作目录（位于 INAGENT/graphrag_index）
GRAPHRAG_INDEX_DIR = Path(__file__).resolve().parent.parent / "graphrag_index"

# 实体类型定义（网络配置领域 + 测试领域）
DEFAULT_ENTITY_TYPES = [
    # 产品知识层
    "product_module",      # 产品功能模块：SLB, LLB, GSLB, 基础网络
    "protocol",            # 协议类型：HTTP, HTTPS, TCP, UDP, HTTP/2
    "feature",             # 产品功能特性：HTTP2 多路复用, 头部压缩
    "design_knowledge",    # 设计知识：数据结构, 状态机, API, 设计决策
    # CLI/配置层
    "command",             # CLI 命令完整语法
    "parameter",           # 命令参数及范围/默认值
    "step_type",           # 配置步骤类型
    "configuration",       # 配置项或配置块
    "config_example",      # 配置实例（完整命令序列示例）
    # 需求/规格层
    "requirement",         # 功能需求/规格要求
    "scenario",            # 配置场景
    # 测试层
    "test_case",           # 测试用例概要
    "test_standard",       # 测试标准定义
]


@dataclass
class GraphRAGConfig:
    """GraphRAG 配置类"""
    # LLM 配置
    api_key: str = ""
    api_base: str = "https://api.siliconflow.cn/v1"
    chat_model: str = "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"
    embedding_model: str = "BAAI/bge-m3"
    
    # 处理配置
    chunk_size: int = 16000
    chunk_overlap: int = 400
    max_gleanings: int = 0
    
    # 实体类型
    entity_types: List[str] = field(default_factory=lambda: DEFAULT_ENTITY_TYPES.copy())
    
    # 输出配置
    workspace: Path = GRAPHRAG_INDEX_DIR
    
    # 限速配置（SiliconFlow）
    # 说明：
    # - 官方示例配额为 RPM=1000, TPM=50000，但实际账号往往配额更低；
    # - GraphRAG 在构建索引时会高并发+长上下文调用，很容易在真实配额下打满 TPM；
    # - 这里采用「保守默认值」，优先保证不触发 429，而不是极限压榨吞吐量。
    #
    # 如需提速，可在确认自己账号真实配额后，再逐步调大下面三个参数。
    # 建议调参顺序：先提高 tokens_per_minute，再提高 requests_per_minute，最后再考虑并发。
    requests_per_minute: int = 1000    # 每分钟请求数，LiteLLM 已做全局限速，此处放开
    tokens_per_minute: int = 500000    # 每分钟 token 数，LiteLLM 已做全局限速，此处放开
    concurrent_requests: int = 50      # 并发调整为50，避免 Windows 上的 aiohttp 连接池耗尽
    max_retries: int = 6


def load_siliconflow_config() -> GraphRAGConfig:
    """从环境变量加载 LLM Gateway 配置"""
    from INAGENT.utils.llm_config import get_siliconflow_config
    
    config = get_siliconflow_config()
    
    return GraphRAGConfig(
        api_key=config.get("api_key", ""),
        api_base=config.get("base_url", "https://api.siliconflow.cn/v1"),
        chat_model=config.get("chat_model", "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"),
        embedding_model=config.get("embedding_model", "BAAI/bge-m3"),
    )


def generate_graphrag_settings(config: GraphRAGConfig, output_dir: Path = None) -> Path:
    """
    生成 GraphRAG settings.yaml 配置文件
    
    适配 LLM Gateway 作为 OpenAI 兼容的 LLM 提供商
    
    Args:
        config: GraphRAG 配置
        output_dir: 输出目录，默认为 graphrag_index
    
    Returns:
        生成的 settings.yaml 路径
    """
    output_dir = output_dir or config.workspace
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 生成 settings.yaml（适配 SiliconFlow）
    settings = {
        "models": {
            "default_chat_model": {
                "type": "chat",
                "model_provider": "openai",  # 使用 OpenAI 兼容接口
                "auth_type": "api_key",
                "api_key": "${GRAPHRAG_API_KEY}",
                "api_base": config.api_base,
                "model": config.chat_model,
                "model_supports_json": False,  # Gateway不支持structured JSON schema output，依靠prompt获取JSON
                "concurrent_requests": config.concurrent_requests,
                "request_timeout": 600.0,
                "max_tokens": 16384,
                "async_mode": "threaded",
                "retry_strategy": "exponential_backoff",
                "max_retries": config.max_retries,
                "tokens_per_minute": config.tokens_per_minute,
                "requests_per_minute": config.requests_per_minute,
            },
            "default_embedding_model": {
                "type": "embedding",
                "model_provider": "openai",
                "auth_type": "api_key",
                "api_key": "${GRAPHRAG_API_KEY}",
                "api_base": config.api_base,
                "model": config.embedding_model,
                "concurrent_requests": config.concurrent_requests,
                "request_timeout": 360.0,
                "async_mode": "threaded",
                "retry_strategy": "exponential_backoff",
                "max_retries": config.max_retries,
            },
        },
        "input": {
            "storage": {
                "type": "file",
                "base_dir": "input",
            },
            "file_type": "json",  # 使用 JSON 格式输入
        },
        "chunks": {
            "size": config.chunk_size,
            "overlap": config.chunk_overlap,
            "group_by_columns": ["id"],
        },
        "output": {
            "type": "file",
            "base_dir": "output",
        },
        "cache": {
            "type": "file",
            "base_dir": "cache",
        },
        "reporting": {
            "type": "file",
            "base_dir": "logs",
        },
        "vector_store": {
            "default_vector_store": {
                "type": "lancedb",
                "db_uri": "output/lancedb",
                "container_name": "default",
            },
        },
        "embed_text": {
            "model_id": "default_embedding_model",
            "vector_store_id": "default_vector_store",
            "batch_max_tokens": 4000,
        },
        "extract_graph": {
            "model_id": "default_chat_model",
            "prompt": "prompts/extract_graph.txt",
            "entity_types": config.entity_types,
            "max_gleanings": config.max_gleanings,
        },
        "summarize_descriptions": {
            "model_id": "default_chat_model",
            "prompt": "prompts/summarize_descriptions.txt",
            "max_length": 500,
        },
        "extract_graph_nlp": {
            "text_analyzer": {
                "extractor_type": "regex_english",
            },
            "async_mode": "threaded",
        },
        "cluster_graph": {
            "max_cluster_size": 10,
        },
        "extract_claims": {
            "enabled": False,  # 禁用 claim 提取，简化流程
        },
        "community_reports": {
            "model_id": "default_chat_model",
            "graph_prompt": "prompts/community_report_graph.txt",
            "text_prompt": "prompts/community_report_text.txt",
            "max_length": 2000,
            "max_input_length": 8000,
        },
        "embed_graph": {
            "enabled": False,  # 暂时禁用 node2vec
        },
        "umap": {
            "enabled": False,
        },
        "snapshots": {
            "graphml": True,  # 启用 GraphML 导出
            "embeddings": False,
        },
        "local_search": {
            "chat_model_id": "default_chat_model",
            "embedding_model_id": "default_embedding_model",
            "prompt": "prompts/local_search_system_prompt.txt",
        },
        "global_search": {
            "chat_model_id": "default_chat_model",
            "map_prompt": "prompts/global_search_map_system_prompt.txt",
            "reduce_prompt": "prompts/global_search_reduce_system_prompt.txt",
            "knowledge_prompt": "prompts/global_search_knowledge_system_prompt.txt",
        },
        "drift_search": {
            "chat_model_id": "default_chat_model",
            "embedding_model_id": "default_embedding_model",
            "prompt": "prompts/drift_search_system_prompt.txt",
            "reduce_prompt": "prompts/drift_reduce_prompt.txt",
        },
    }
    
    settings_path = output_dir / "settings.yaml"
    with open(settings_path, "w", encoding="utf-8") as f:
        yaml.dump(settings, f, default_flow_style=False, allow_unicode=True)
    
    # 生成 .env 文件
    env_path = output_dir / ".env"
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(f"GRAPHRAG_API_KEY={config.api_key}\n")
    
    logger.info(f"已生成 GraphRAG 配置: {settings_path}")
    return settings_path


def generate_extract_graph_prompt(entity_types: List[str] = None) -> str:
    """
    生成实体/关系提取 Prompt（针对网络配置领域优化）
    
    参考 Microsoft GraphRAG 手动调优文档：
    https://microsoft.github.io/graphrag/prompt_tuning/manual_prompt_tuning/
    
    Tokens:
    - {input_text}: 输入文本
    - {entity_types}: 实体类型列表
    - {tuple_delimiter}: 元组分隔符
    - {record_delimiter}: 记录分隔符
    - {completion_delimiter}: 完成标记
    """
    entity_types = entity_types or DEFAULT_ENTITY_TYPES
    
    prompt = f"""你是网络设备配置与测试领域的知识图谱构建专家。
你需要从技术文档（包括配置文档、功能规格书、测试列表）中提取实体和关系，构建知识图谱。

# 实体类型定义

请识别以下类型的实体：
{chr(10).join(f"- {t}: " + _get_entity_type_description(t) for t in entity_types)}

# 关系类型定义

请识别以下关系：
- DEPENDS_ON: 配置依赖关系（如：健康检查依赖后端服务器）
- SUPPORTS_PROTOCOL: 协议支持关系（如：虚拟服务支持HTTP协议）
- BELONGS_TO: 归属关系（如：slb_health_checks 归属于 SLB 模块）
- CONFIGURES: 配置关系（如：命令配置某个参数）
- IS_ADVANCED_FEATURE: 高级功能关系（如：会话保持是虚拟服务的高级功能）
- PART_OF: 组成关系（如：参数是命令的一部分）
- TESTS: 测试关系（如：测试用例测试某个功能特性）
- COVERS: 覆盖关系（如：测试集覆盖某个需求）
- VERIFIES: 验证关系（如：测试用例验证某个配置项的正确性）

# 输出格式

对于每个识别的实体，输出一个元组：
("entity"{{tuple_delimiter}}<entity_name>{{tuple_delimiter}}<entity_type>{{tuple_delimiter}}<entity_description>){{record_delimiter}}

对于每个识别的关系，输出一个元组：
("relationship"{{tuple_delimiter}}<source_entity>{{tuple_delimiter}}<target_entity>{{tuple_delimiter}}<relationship_type>{{tuple_delimiter}}<relationship_description>{{tuple_delimiter}}<relationship_strength>){{record_delimiter}}

其中：
- entity_name: 实体名称（使用规范化名称，如 slb_virtual_services）
- entity_type: 实体类型（从上述类型中选择）
- entity_description: 实体描述（简洁明了）
- relationship_type: 关系类型（从上述类型中选择）
- relationship_description: 关系描述
- relationship_strength: 关系强度（1-10，10 表示最强）

# 重要规则

1. **动态协议识别**：从文本中动态识别所有协议类型（HTTP, HTTPS, TCP, UDP, SIP, RTSP 等），不要硬编码
2. **模块前缀**：步骤类型应使用模块前缀（如 slb_health_checks 而非 health_checks）
3. **层级关系**：识别模块层级（基础网络 → 应用层 → 高级功能）
4. **配置顺序**：识别配置依赖关系，确保正确的配置顺序
5. **测试关联**：识别测试用例与功能需求/配置项之间的 TESTS/COVERS/VERIFIES 关系
6. **需求追踪**：从功能规格书中提取 requirement 实体，建立需求到测试的链路

# 输入文本

{{input_text}}

# 输出

{{completion_delimiter}}
"""
    return prompt


def _get_entity_type_description(entity_type: str) -> str:
    """获取实体类型描述"""
    descriptions = {
        "product_module": "产品功能模块（如 SLB、LLB、GSLB、基础网络、安全）",
        "protocol": "网络协议类型（如 HTTP、HTTPS、TCP、UDP、SIP）",
        "step_type": "配置步骤类型（如 slb_virtual_services、slb_health_checks）",
        "configuration": "配置项或配置块（如 虚拟服务配置、健康检查配置）",
        "command": "CLI 命令（如 slb virtual http、slb real）",
        "parameter": "配置参数（如 vip、vport、max_connection）",
        "scenario": "配置场景（如 HTTP_SLB_CONFIG、HTTPS_SSL_CONFIG）",
        "test_case": "测试用例（如 HTTP2_Stream_Multiplexing_Test、Health_Check_Test）",
        "requirement": "功能需求/规格要求（如 SW Functional Spec 中的具体功能要求）",
        "feature": "产品功能特性（如 HTTP2 多路复用、头部压缩、服务器推送）",
    }
    return descriptions.get(entity_type, "其他实体")


def generate_prompts(output_dir: Path, entity_types: List[str] = None) -> Dict[str, Path]:
    """
    生成所有 GraphRAG 需要的 Prompt 文件
    
    Returns:
        Prompt 文件路径字典
    """
    prompts_dir = output_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    
    entity_types = entity_types or DEFAULT_ENTITY_TYPES
    
    # 1. 实体/关系提取 Prompt
    extract_graph_prompt = generate_extract_graph_prompt(entity_types)
    extract_graph_path = prompts_dir / "extract_graph.txt"
    with open(extract_graph_path, "w", encoding="utf-8") as f:
        f.write(extract_graph_prompt)
    
    # 2. 描述摘要 Prompt
    summarize_prompt = """请为以下实体或关系生成简洁的描述摘要。

实体/关系名称: {entity_name}

描述列表:
{description_list}

请生成一个统一的、简洁的描述（不超过100字）。
"""
    summarize_path = prompts_dir / "summarize_descriptions.txt"
    with open(summarize_path, "w", encoding="utf-8") as f:
        f.write(summarize_prompt)
    
    # 3. 社区报告 Prompt（图模式）- 使用 graphrag_test 中已验证的英文 JSON 格式 prompt
    community_graph_path = prompts_dir / "community_report_graph.txt"
    _ref_graph = Path(__file__).parent / "graphrag_test" / "prompts" / "community_report_graph.txt"
    if _ref_graph.exists():
        import shutil
        shutil.copy2(_ref_graph, community_graph_path)
    else:
        logger.warning("参考 prompt 不存在: %s，跳过覆盖", _ref_graph)
    
    # 4. 社区报告 Prompt（文本模式）- 使用 graphrag_test 中已验证的英文 JSON 格式 prompt
    community_text_path = prompts_dir / "community_report_text.txt"
    _ref_text = Path(__file__).parent / "graphrag_test" / "prompts" / "community_report_text.txt"
    if _ref_text.exists():
        import shutil
        shutil.copy2(_ref_text, community_text_path)
    else:
        logger.warning("参考 prompt 不存在: %s，跳过覆盖", _ref_text)
    
    # 5. 本地搜索 Prompt（使用 Microsoft GraphRAG 默认英文模板，必须保留占位符格式）
    local_search_prompt = """---Role---

You are a helpful assistant responding to questions about data in the tables provided.


---Goal---

Generate a response of the target length and format that responds to the user's question, summarizing all information in the input data tables appropriate for the response length and format, and incorporating any relevant general knowledge.

If you don't know the answer, just say so. Do not make anything up.

Points supported by data should list their data references as follows:

"This is an example sentence supported by multiple data references [Data: <dataset name> (record ids); <dataset name> (record ids)]."

Do not list more than 5 record ids in a single reference. Instead, list the top 5 most relevant record ids and add "+more" to indicate that there are more.

For example:

"Person X is the owner of Company Y and subject to many allegations of wrongdoing [Data: Sources (15, 16), Reports (1), Entities (5, 7); Relationships (23); Claims (2, 7, 34, 46, 64, +more)]."

where 15, 16, 1, 5, 7, 23, 2, 7, 34, 46, and 64 represent the id (not the index) of the relevant data record.

Do not include information where the supporting evidence for it is not provided.


---Target response length and format---

{response_type}


---Data tables---

{context_data}


---Goal---

Generate a response of the target length and format that responds to the user's question, summarizing all information in the input data tables appropriate for the response length and format, and incorporating any relevant general knowledge.

If you don't know the answer, just say so. Do not make anything up.

Points supported by data should list their data references as follows:

"This is an example sentence supported by multiple data references [Data: <dataset name> (record ids); <dataset name> (record ids)]."

Do not list more than 5 record ids in a single reference. Instead, list the top 5 most relevant record ids and add "+more" to indicate that there are more.

Do not include information where the supporting evidence for it is not provided.


---Target response length and format---

{response_type}

Add sections and commentary to the response as appropriate for the length and format. Style the response in markdown.
"""
    local_search_path = prompts_dir / "local_search_system_prompt.txt"
    with open(local_search_path, "w", encoding="utf-8") as f:
        f.write(local_search_prompt)
    
    # 6. 全局搜索 Map Prompt（必须要求 JSON 输出！global_search 的 map 阶段强制使用 json_mode）
    global_map_prompt = """---Role---

You are a helpful assistant responding to questions about data in the tables provided.


---Goal---

Generate a response consisting of a list of key points that responds to the user's question, summarizing all relevant information in the input data tables.

You should use the data provided in the data tables below as the primary context for generating the response.
If you don't know the answer or if the input data tables do not contain sufficient information to provide an answer, just say so. Do not make anything up.

Each key point in the response should have the following element:
- Description: A comprehensive description of the point.
- Importance Score: An integer score between 0-100 that indicates how important the point is in answering the user's question. An 'I don't know' type of response should have a score of 0.

The response should be JSON formatted as follows:
{{
    "points": [
        {{"description": "Description of point 1 [Data: Reports (report ids)]", "score": score_value}},
        {{"description": "Description of point 2 [Data: Reports (report ids)]", "score": score_value}}
    ]
}}

The response shall preserve the original meaning and use of modal verbs such as "shall", "may" or "will".

Points supported by data should list the relevant reports as references as follows:
"This is an example sentence supported by data references [Data: Reports (report ids)]"

**Do not list more than 5 record ids in a single reference**. Instead, list the top 5 most relevant record ids and add "+more" to indicate that there are more.

For example:
"Person X is the owner of Company Y and subject to many allegations of wrongdoing [Data: Reports (2, 7, 64, 46, 34, +more)]. He is also CEO of company X [Data: Reports (1, 3)]"

where 1, 2, 3, 7, 34, 46, and 64 represent the id (not the index) of the relevant data report in the provided tables.

Do not include information where the supporting evidence for it is not provided.

---Data tables---

{context_data}

---Goal---

Generate a response consisting of a list of key points that responds to the user's question, summarizing all relevant information in the input data tables.

You should use the data provided in the data tables below as the primary context for generating the response.
If you don't know the answer or if the input data tables do not contain sufficient information to provide an answer, just say so. Do not make anything up.

Each key point in the response should have the following element:
- Description: A comprehensive description of the point.
- Importance Score: An integer score between 0-100 that indicates how important the point is in answering the user's question. An 'I don't know' type of response should have a score of 0.

The response shall preserve the original meaning and use of modal verbs such as "shall", "may" or "will".

Points supported by data should list the relevant reports as references as follows:
"This is an example sentence supported by data references [Data: Reports (report ids)]"

**Do not list more than 5 record ids in a single reference**. Instead, list the top 5 most relevant record ids and add "+more" to indicate that there are more.

Do not include information where the supporting evidence for it is not provided.

The response should be JSON formatted as follows:
{{
    "points": [
        {{"description": "Description of point 1 [Data: Reports (report ids)]", "score": score_value}},
        {{"description": "Description of point 2 [Data: Reports (report ids)]", "score": score_value}}
    ]
}}
"""
    global_map_path = prompts_dir / "global_search_map_system_prompt.txt"
    with open(global_map_path, "w", encoding="utf-8") as f:
        f.write(global_map_prompt)
    
    # 7. 全局搜索 Reduce Prompt
    global_reduce_prompt = """---Role---

You are a helpful assistant responding to questions about a dataset by synthesizing perspectives from multiple analysts.


---Goal---

Generate a response of the target length and format that responds to the user's question, summarize all the reports from multiple analysts who focused on different parts of the dataset.

Note that the analysts' reports provided below are ranked in the **descending order of importance**.

If you don't know the answer or if the provided reports do not contain sufficient information to provide an answer, just say so. Do not make anything up.

The final response should remove all irrelevant information from the analysts' reports and merge the cleaned information into a comprehensive answer that provides explanations of all the key points and implications appropriate for the response length and format.

Add sections and commentary to the response as appropriate for the length and format. Style the response in markdown.

The response shall preserve the original meaning and use of modal verbs such as "shall", "may" or "will".

The response should also preserve all the data references previously included in the analysts' reports, but do not mention the roles of multiple analysts in the analysis process.

**Do not list more than 5 record ids in a single reference**. Instead, list the top 5 most relevant record ids and add "+more" to indicate that there are more.

For example:

"Person X is the owner of Company Y and subject to many allegations of wrongdoing [Data: Reports (2, 7, 34, 46, 64, +more)]. He is also CEO of company X [Data: Reports (1, 3)]"

where 1, 2, 3, 7, 34, 46, and 64 represent the id (not the index) of the relevant data record.

Do not include information where the supporting evidence for it is not provided.

---Target response length and format---

{response_type}


---Analyst Reports---

{report_data}


---Goal---

Generate a response of the target length and format that responds to the user's question, summarize all the reports from multiple analysts who focused on different parts of the dataset.

Note that the analysts' reports provided below are ranked in the **descending order of importance**.

If you don't know the answer or if the provided reports do not contain sufficient information to provide an answer, just say so. Do not make anything up.

The response shall preserve the original meaning and use of modal verbs such as "shall", "may" or "will".

The response should also preserve all the data references previously included in the analysts' reports, but do not mention the roles of multiple analysts in the analysis process.

**Do not list more than 5 record ids in a single reference**. Instead, list the top 5 most relevant record ids and add "+more" to indicate that there are more.

Do not include information where the supporting evidence for it is not provided.

---Target response length and format---

{response_type}

Add sections and commentary to the response as appropriate for the length and format. Style the response in markdown.
"""
    global_reduce_path = prompts_dir / "global_search_reduce_system_prompt.txt"
    with open(global_reduce_path, "w", encoding="utf-8") as f:
        f.write(global_reduce_prompt)
    
    # 8. 全局搜索知识 Prompt
    global_knowledge_prompt = """
The response may also include relevant real-world knowledge outside the dataset, but it must be explicitly annotated with a verification tag [LLM: verify]. For example:
"This is an example sentence supported by real-world knowledge [LLM: verify]."
"""
    global_knowledge_path = prompts_dir / "global_search_knowledge_system_prompt.txt"
    with open(global_knowledge_path, "w", encoding="utf-8") as f:
        f.write(global_knowledge_prompt)
    
    # 9. DRIFT 搜索 Prompt
    drift_prompt = """---Role---

You are a helpful assistant responding to questions about data in the tables provided.

---Goal---

Generate a response of the target length and format that responds to the user's question, summarizing all information in the input data tables appropriate for the response length and format.

If you don't know the answer, just say so. Do not make anything up.

---Data tables---

{context_data}

---Community Reports---

{community_reports}

---Target response length and format---

{response_type}
"""
    drift_path = prompts_dir / "drift_search_system_prompt.txt"
    with open(drift_path, "w", encoding="utf-8") as f:
        f.write(drift_prompt)
    
    # 10. DRIFT Reduce Prompt
    drift_reduce_prompt = """---Role---

You are a helpful assistant responding to questions about a dataset by synthesizing multiple rounds of search results.

---Goal---

Generate a comprehensive response that synthesizes all the search results provided.
If you don't know the answer, just say so. Do not make anything up.

---Search Results---

{context_data}
"""
    drift_reduce_path = prompts_dir / "drift_reduce_prompt.txt"
    with open(drift_reduce_path, "w", encoding="utf-8") as f:
        f.write(drift_reduce_prompt)
    
    logger.info(f"已生成 GraphRAG Prompts: {prompts_dir}")
    
    return {
        "extract_graph": extract_graph_path,
        "summarize_descriptions": summarize_path,
        "community_report_graph": community_graph_path,
        "community_report_text": community_text_path,
        "local_search": local_search_path,
        "global_search_map": global_map_path,
        "global_search_reduce": global_reduce_path,
        "global_search_knowledge": global_knowledge_path,
        "drift_search": drift_path,
        "drift_reduce": drift_reduce_path,
    }


# 已有专用检索器处理的来源文件，不纳入 GraphRAG 索引
_DEDICATED_RETRIEVER_SOURCES = {"cli.json", "app.json", "cli.pdf"}

# document_category → knowledge_layer 映射
_CATEGORY_TO_LAYER = {
    "spec/prd": "design",
    "spec/func_spec": "design",
    "spec/design": "design",
    "test/test_list": "test",
    "test/test_strategy": "test",
    "test/test_template": "rules",
}


def _classify_source_file(source_file: str, blocks: list) -> str:
    """使用 document_classifier 对来源文件进行分类。"""
    # 优先使用 block 级别已有的 document_category
    for block in blocks:
        cat = block.get("metadata", {}).get("document_category", "")
        if cat:
            return cat
    # 回退到文件名 + 内容分类
    try:
        from INAGENT.data_tools.document_classifier import classify_document
        preview = ""
        for block in blocks[:5]:
            text = block.get("text", "") or block.get("page_content", "")
            preview += text[:400] + "\n"
        category, _ = classify_document(Path(source_file), content_preview=preview)
        return category
    except Exception:
        return "unknown"


def prepare_input_documents(
    knowledge_base_path: Path,
    output_dir: Path,
    max_documents: int = None
) -> Path:
    """
    准备 GraphRAG 输入文档

    将 knowledge_base.json 按来源文件合并为大文档，供 GraphRAG 分块和提取。
    合并后每个来源文件的所有 block 会拼接为一个文档，由 GraphRAG 的 text_splitter
    按 chunk_size 自动切分为 text_unit，从而最大化利用 LLM 的上下文窗口。

    排除规则：
    - cli.json / app.json / cli.pdf 由 CLIReferenceRetriever 专门处理，不纳入 GraphRAG

    元数据增强：
    - document_category: 自动分类（spec/prd, spec/func_spec, spec/design, test/test_list 等）
    - knowledge_layer: 知识层归属（design / test / rules）

    Args:
        knowledge_base_path: knowledge_base.json 路径
        output_dir: 输出目录
        max_documents: 最大文档数（用于测试）

    Returns:
        输入目录路径
    """
    import sys
    from datetime import datetime
    from collections import OrderedDict

    def _m(msg: str, *args: object) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if args:
            logger.info("[MILESTONE] %s | " + msg, ts, *args)
        else:
            logger.info("[MILESTONE] %s | %s", ts, msg)
        sys.stdout.flush()
        sys.stderr.flush()

    _m("prepare_input_documents START | kb=%s", str(knowledge_base_path))
    input_dir = output_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    with open(knowledge_base_path, "r", encoding="utf-8") as f:
        kb_data = json.load(f)
    chunks = kb_data if isinstance(kb_data, list) else kb_data.get("chunks", [])
    if max_documents:
        chunks = chunks[:max_documents]
    _m("prepare_input_documents: loaded %d chunks", len(chunks))

    # --- 按来源文件合并 block ---
    source_groups: OrderedDict[str, list] = OrderedDict()
    for chunk in chunks:
        src = (chunk.get("metadata") or {}).get("source_file", "unknown")
        source_groups.setdefault(src, []).append(chunk)

    _m("prepare_input_documents: grouped into %d source files", len(source_groups))

    documents = []
    excluded_blocks = 0
    for source_file, group_chunks in source_groups.items():
        # 跳过已有专用检索器处理的来源
        if source_file in _DEDICATED_RETRIEVER_SOURCES:
            excluded_blocks += len(group_chunks)
            _m("prepare_input_documents: SKIP %s (%d blocks, dedicated retriever)",
               source_file, len(group_chunks))
            continue

        # 分类
        category = _classify_source_file(source_file, group_chunks)
        layer = _CATEGORY_TO_LAYER.get(category, "design")

        parts: list[str] = []
        for chunk in group_chunks:
            text = chunk.get("text", "") or chunk.get("page_content", "")
            if not text.strip():
                continue
            metadata = chunk.get("metadata", {})

            # 构建带元数据前缀的文本
            meta_prefix = []
            document_category = metadata.get("document_category", "") or category
            product_module = metadata.get("product_module", "unknown")
            protocol_type = metadata.get("protocol_type", [])
            feature_name = metadata.get("feature_name", "")
            step_type = metadata.get("step_type", "unknown")
            section_title = metadata.get("section_title", "")

            if document_category:
                meta_prefix.append(f"[分类: {document_category}]")
            if product_module and product_module != "unknown":
                meta_prefix.append(f"[模块: {product_module}]")
            if protocol_type:
                if isinstance(protocol_type, list):
                    meta_prefix.append(f"[协议: {', '.join(protocol_type)}]")
                else:
                    meta_prefix.append(f"[协议: {protocol_type}]")
            if feature_name:
                meta_prefix.append(f"[功能: {feature_name}]")
            if step_type and step_type != "unknown":
                meta_prefix.append(f"[步骤: {step_type}]")
            if section_title:
                meta_prefix.append(f"[章节: {section_title}]")

            enriched = " ".join(meta_prefix) + "\n" + text if meta_prefix else text
            parts.append(enriched)

        merged_text = "\n\n".join(parts)
        if not merged_text.strip():
            continue

        doc_idx = len(documents)
        documents.append({
            "id": f"src_{doc_idx:04d}",
            "text": f"[来源: {source_file}]\n\n{merged_text}",
            "title": source_file,
            "metadata": {
                "source_file": source_file,
                "document_category": category,
                "knowledge_layer": layer,
                "block_count": len(group_chunks),
            }
        })

    output_path = input_dir / "documents.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(documents, f, ensure_ascii=False, indent=2)
    _m("prepare_input_documents DONE | %d sources -> %d docs (excluded %d blocks from dedicated retrievers), wrote to %s",
       len(source_groups), len(documents), excluded_blocks, str(output_path))
    logger.info("已准备 %d 个合并文档到 %s（来源 %d 个文件，排除 %d blocks，原始 %d 个 block）",
                len(documents), input_dir, len(source_groups), excluded_blocks, len(chunks))
    return input_dir


def initialize_graphrag_index(
    knowledge_base_path: Path = None,
    workspace_dir: Path = None,
    config: GraphRAGConfig = None
) -> Path:
    """
    初始化 GraphRAG 工作空间
    
    创建完整的 GraphRAG 工作目录结构：
    - settings.yaml
    - prompts/
    - input/
    - output/
    - cache/
    - logs/
    
    Args:
        knowledge_base_path: knowledge_base.json 路径
        workspace_dir: 工作空间目录
        config: GraphRAG 配置
    
    Returns:
        工作空间路径
    """
    import sys
    from datetime import datetime
    def _m(msg: str, *args: object) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if args:
            logger.info("[MILESTONE] %s | " + msg, ts, *args)
        else:
            logger.info("[MILESTONE] %s | %s", ts, msg)
        sys.stdout.flush()
        sys.stderr.flush()

    _m("initialize_graphrag_index START | workspace=%s", str(workspace_dir))
    workspace_dir = workspace_dir or GRAPHRAG_INDEX_DIR
    workspace_dir.mkdir(parents=True, exist_ok=True)

    config = config or load_siliconflow_config()
    config.workspace = workspace_dir
    _m("initialize_graphrag_index: generate_graphrag_settings START")
    generate_graphrag_settings(config, workspace_dir)
    _m("initialize_graphrag_index: generate_graphrag_settings DONE")
    _m("initialize_graphrag_index: generate_prompts START")
    generate_prompts(workspace_dir, config.entity_types)
    _m("initialize_graphrag_index: generate_prompts DONE")
    if knowledge_base_path and knowledge_base_path.exists():
        _m("initialize_graphrag_index: prepare_input_documents START")
        prepare_input_documents(knowledge_base_path, workspace_dir)
        _m("initialize_graphrag_index: prepare_input_documents DONE")
    (workspace_dir / "output").mkdir(exist_ok=True)
    (workspace_dir / "cache").mkdir(exist_ok=True)
    (workspace_dir / "logs").mkdir(exist_ok=True)
    _m("initialize_graphrag_index DONE | %s", str(workspace_dir))
    logger.info("GraphRAG 工作空间已初始化: %s", workspace_dir)
    return workspace_dir


def validate_graphrag_index(workspace_dir: Path = None) -> Dict[str, Any]:
    """
    验证 GraphRAG 工作空间是否完整
    
    检查以下内容：
    - settings.yaml 配置文件
    - prompts/ 目录及 Prompt 文件
    - input/ 目录及输入文档
    - output/ 目录及索引文件（如果已构建）
    
    Returns:
        验证结果字典
    """
    workspace_dir = workspace_dir or GRAPHRAG_INDEX_DIR
    
    result = {
        "valid": True,
        "workspace": str(workspace_dir),
        "checks": {},
        "missing": [],
    }
    
    # 检查 settings.yaml
    settings_path = workspace_dir / "settings.yaml"
    result["checks"]["settings.yaml"] = settings_path.exists()
    if not settings_path.exists():
        result["missing"].append("settings.yaml")
        result["valid"] = False
    
    # 检查 .env
    env_path = workspace_dir / ".env"
    result["checks"][".env"] = env_path.exists()
    if not env_path.exists():
        result["missing"].append(".env")
    
    # 检查 prompts 目录
    prompts_dir = workspace_dir / "prompts"
    result["checks"]["prompts/"] = prompts_dir.exists()
    if prompts_dir.exists():
        required_prompts = [
            "extract_graph.txt",
            "summarize_descriptions.txt",
            "local_search_system_prompt.txt",
        ]
        for prompt in required_prompts:
            if not (prompts_dir / prompt).exists():
                result["missing"].append(f"prompts/{prompt}")
                result["valid"] = False
    else:
        result["missing"].append("prompts/")
        result["valid"] = False
    
    # 检查 input 目录
    input_dir = workspace_dir / "input"
    result["checks"]["input/"] = input_dir.exists()
    if input_dir.exists():
        input_files = list(input_dir.glob("*.json")) + list(input_dir.glob("*.txt"))
        result["checks"]["input_files"] = len(input_files) > 0
        if len(input_files) == 0:
            result["missing"].append("input/*.json or input/*.txt")
    else:
        result["missing"].append("input/")
    
    # 检查 output 目录（索引结果）
    output_dir = workspace_dir / "output"
    result["checks"]["output/"] = output_dir.exists()
    if output_dir.exists():
        index_files = {
            "entities.parquet": (output_dir / "entities.parquet").exists(),
            "relationships.parquet": (output_dir / "relationships.parquet").exists(),
            "communities.parquet": (output_dir / "communities.parquet").exists(),
            "community_reports.parquet": (output_dir / "community_reports.parquet").exists(),
            "text_units.parquet": (output_dir / "text_units.parquet").exists(),
        }
        result["checks"]["index_files"] = index_files
        result["index_built"] = all(index_files.values())
    else:
        result["index_built"] = False
    
    return result


def get_graphrag_status() -> Dict[str, Any]:
    """
    获取 GraphRAG 状态信息
    
    Returns:
        状态信息字典
    """
    validation = validate_graphrag_index()
    
    status = {
        "available": validation["valid"],
        "index_built": validation.get("index_built", False),
        "workspace": validation["workspace"],
        "validation": validation,
    }
    
    # 统计索引信息
    if status["index_built"]:
        output_dir = Path(validation["workspace"]) / "output"
        try:
            import pandas as pd
            
            entities_path = output_dir / "entities.parquet"
            if entities_path.exists():
                entities = pd.read_parquet(entities_path)
                status["entity_count"] = len(entities)
            
            relationships_path = output_dir / "relationships.parquet"
            if relationships_path.exists():
                relationships = pd.read_parquet(relationships_path)
                status["relationship_count"] = len(relationships)
            
            communities_path = output_dir / "communities.parquet"
            if communities_path.exists():
                communities = pd.read_parquet(communities_path)
                status["community_count"] = len(communities)
            
            text_units_path = output_dir / "text_units.parquet"
            if text_units_path.exists():
                text_units = pd.read_parquet(text_units_path)
                status["text_unit_count"] = len(text_units)
                
        except Exception as e:
            logger.warning(f"读取索引统计信息失败: {e}")
    
    return status


if __name__ == "__main__":
    # 测试配置生成
    logging.basicConfig(level=logging.INFO)
    
    # 加载配置
    config = load_siliconflow_config()
    print(f"Chat Model: {config.chat_model}")
    print(f"Embedding Model: {config.embedding_model}")
    
    # 初始化工作空间
    kb_path = Path(__file__).parent / "knowledge_base" / "reference" / "knowledge_base.json"
    workspace = initialize_graphrag_index(kb_path)
    print(f"Workspace: {workspace}")
    
    # 验证工作空间
    validation = validate_graphrag_index(workspace)
    print(f"\n工作空间验证结果:")
    print(f"  - 有效: {validation['valid']}")
    print(f"  - 索引已构建: {validation.get('index_built', False)}")
    if validation["missing"]:
        print(f"  - 缺失: {validation['missing']}")