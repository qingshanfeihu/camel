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

# 实体类型定义 — 仅保留 CLI/配置领域相关类型
# CLI 叶子节点是结构化命令数据，无需 test_case/scenario/requirement 等类型
DEFAULT_ENTITY_TYPES = [
    "product_module",      # 产品功能模块：SLB, LLB, GSLB, 基础网络, AAA
    "protocol",            # 协议类型：HTTP, HTTPS, TCP, UDP, SIP, RTSP
    "feature",             # 产品功能特性：会话保持, 健康检查, SSL 卸载
    "command",             # CLI 命令（完整语法，如 slb virtual http）
    "parameter",           # 命令参数（名称/类型/范围/默认值）
    "configuration",       # 配置项或配置块（如 虚拟服务配置, 健康检查配置）
]

# 实体类型推断规则 — 供 post-processing 修复脏实体时使用
# 规则按优先级从高到低排列；新增类型时在此同步维护匹配模式
# pattern 匹配对象：entity description 文本（中英文混合、小写匹配）
ENTITY_TYPE_INFER_RULES: list[tuple[str, str]] = [
    # (regex_pattern, entity_type)
    (r'功能模块|产品模块|\bmodule\b',                              "product_module"),
    (r'网络协议|\bprotocol\b(?!\s*type)',                          "protocol"),
    (r'配置(项|块|集合|段)|configuration\s*block|配置集',          "configuration"),
    (r'功能(特性|特征)|\bfeature\b',                               "feature"),
    (r'CLI\s*命令|\b命令\b(?!参数)|\bcli\s+command\b',             "command"),
    (r'(命令)?参数|取值范围|默认值|必填|可选参数',                  "parameter"),
]

# 当 description 和 title 均无法判定类型时的兜底类型
ENTITY_TYPE_FALLBACK = "parameter"


@dataclass
class GraphRAGConfig:
    """GraphRAG 配置类"""
    # LLM 配置
    api_key: str = ""
    api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_model: str = "qwen-plus"
    embedding_model: str = "text-embedding-v4"
    
    # 处理配置
    # Qwen-Plus 128K context，chunk.size 单位是 tokens（tiktoken cl100k_base 编码）
    # CLI 混合文档 ~2 chars/token → 16000 tokens ≈ 32KB = ~80 条命令/chunk
    # 约 22 次 extract_graph LLM 调用（1M chars / 32KB = ~31 chunks × 并发处理）
    chunk_size: int = 16000
    chunk_overlap: int = 300
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


def load_gateway_config() -> GraphRAGConfig:
    """从环境变量加载 LLM Gateway 配置"""
    from INAGENT.utils.llm_config import get_gateway_config
    
    config = get_gateway_config()
    
    return GraphRAGConfig(
        api_key=config.get("api_key", ""),
        api_base=config.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        chat_model=config.get("chat_model", "qwen-plus"),
        embedding_model=config.get("embedding_model", "text-embedding-v4"),
    )


def load_siliconflow_config() -> GraphRAGConfig:
    """兼容旧命名：等价于 load_gateway_config。"""
    return load_gateway_config()


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
            "batch_max_tokens": 8000,  # text-embedding-v4 支持 8192 tokens/batch
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
            # CLI 模块通常包含 20-100 条命令，max_cluster_size=10 会过度分裂
            # 设为 25 使模块级 community 保持完整
            "max_cluster_size": 25,
        },
        "prune_graph": {
            # 移除极低频节点：只出现1次的实体可能是提取噪声
            "min_node_freq": 2,
            "min_node_degree": 1,
            # 移除占边权重极低的边（最低 5% 分位数）
            "min_edge_weight_pct": 0.05,
        },
        "extract_claims": {
            "enabled": False,  # 禁用 claim 提取，简化流程
        },
        "community_reports": {
            "model_id": "default_chat_model",
            "graph_prompt": "prompts/community_report_graph.txt",
            "text_prompt": "prompts/community_report_text.txt",
            "max_length": 2000,
            "max_input_length": 16000,  # Qwen 可处理更长的社区报告输入
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
    
    prompt = f"""你是网络设备 CLI 配置领域的知识图谱构建专家。
输入文档是由 XML 命令树生成的结构化 CLI 参考文档，每个文本块描述一个命令叶子节点及其参数。

# 实体类型定义

请识别以下类型的实体：
{chr(10).join(f"- {t}: " + _get_entity_type_description(t) for t in entity_types)}

# 关系类型定义

请识别以下关系：
- BELONGS_TO: 归属关系（命令/配置项 归属于 模块）
- PART_OF: 组成关系（参数 是 命令 的一部分）
- CONFIGURES: 配置关系（命令 配置 某个功能特性）
- DEPENDS_ON: 配置依赖关系（如：健康检查 依赖 后端服务器配置）
- SUPPORTS_PROTOCOL: 协议支持关系（如：虚拟服务 支持 HTTP 协议）
- IS_VARIANT_OF: 变体关系（如：`no` 形式 是 某命令的 否定变体）
- PRECEDES: 操作前置关系（配置步骤的正确顺序，如 create 先于 bind）

# 输出格式

对于每个识别的实体，输出一个元组：
("entity"{{tuple_delimiter}}<entity_name>{{tuple_delimiter}}<entity_type>{{tuple_delimiter}}<entity_description>){{record_delimiter}}

对于每个识别的关系，输出一个元组：
("relationship"{{tuple_delimiter}}<source_entity>{{tuple_delimiter}}<target_entity>{{tuple_delimiter}}<relationship_type>{{tuple_delimiter}}<relationship_description>{{tuple_delimiter}}<relationship_strength>){{record_delimiter}}

其中：
- entity_name: 实体名称（使用规范化名称，如 slb_virtual_http）
- entity_type: 实体类型（从上述类型中选择）
- entity_description: 实体描述（简洁明了）
- relationship_type: 关系类型（从上述类型中选择）
- relationship_description: 关系描述
- relationship_strength: 关系强度（1-10，10 表示最强）

# 重要规则

1. **模块归属**：entity_name 使用 XML func 名称或命令前缀（如 `slb_virtual_http`）
2. **协议识别**：从文本中动态识别协议（HTTP, HTTPS, TCP, UDP, SIP, RTSP 等），不要硬编码
3. **层级结构**：识别 模块 → 配置项 → 命令 → 参数 的层级实体
4. **配置顺序**：识别命令间的前置依赖（create 先于 bind）
5. **克制提取**：只提取文本中明确出现的实体，不要推断或补全
6. **类型必填**：entity_type 必须是上述6种类型之一（product_module/protocol/feature/command/parameter/configuration），不加引号、不允许留空；命令语法 `<参数名>` 或 `[参数名]` 中的参数归类为 `parameter`；无法确定时缺省归为 `parameter`
7. **忽略内部函数**：文档中 `内部函数:` 字段仅供参考，**不要**将内部函数名（如 `clear_cache_settings`）单独提取为实体，也不要将 `function` 作为实体类型输出
8. **类型格式**：entity_type 写英文小写原始名称，不加引号（正确: `command`，错误: `"command"`）

# 输入文本

{{input_text}}

# 输出

{{completion_delimiter}}
"""
    return prompt


def _get_entity_type_description(entity_type: str) -> str:
    """获取实体类型描述"""
    descriptions = {
        "product_module": "产品功能模块（如 SLB、LLB、GSLB、AAA、基础网络）",
        "protocol":       "网络协议类型（如 HTTP、HTTPS、TCP、UDP、SIP、RTSP）",
        "feature":        "产品功能特性（如 会话保持、健康检查、SSL 卸载、负载均衡）",
        "command":        "CLI 命令（完整语法，如 slb virtual http、aaa ldap server）",
        "parameter":      "命令参数（名称/类型/取值范围/默认值）",
        "configuration":  "配置项或配置块（如 虚拟服务配置、健康检查配置）",
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


# 新策略：默认不排除来源文件，避免关系知识仅停留在专用检索器而不进入图谱。
# 如需恢复旧行为，可通过配置注入来源文件名单。
_DEDICATED_RETRIEVER_SOURCES: set[str] = set()

# 当前 knowledge_base.json 全部是 XML 生成的 cli/reference 叶子节点
# knowledge_layer 固定为 "cli"，无需分类映射


def prepare_input_documents(
    knowledge_base_path: Path,
    output_dir: Path,
    max_documents: int = None
) -> Path:
    """
    准备 GraphRAG 输入文档

    将 knowledge_base.json 按命令族（command family）分组，每族生成一个文档。
    命令族 = 剥去 clear/no/show 操作前缀后的前 2 个实义词，例如：
      - "aaa samlsp sp acs"      → aaa_samlsp
      - "ha consistency on"      → ha_consistency
      - "clear slb group member" → slb_group
      - "clear ha group port"    → ha_group

    相比原模块级分组（aaa / ha / slb 等），命令族分组产生更细粒度的文档：
      - 原模块级：~129 个文档，text unit 大小 8000-16000 token（超出 source 预算）
      - 命令族级：~300-500 个文档，text unit 大小 200-2000 token（可进入 local context source）

    排除规则：
    - 默认不排除来源（_DEDICATED_RETRIEVER_SOURCES 为空），确保 CLI/功能描述也可进入图谱关系层
    - 如需排除，可在 _DEDICATED_RETRIEVER_SOURCES 中显式声明来源文件名

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

    # 按命令族分组：剥去 clear/no/show/display 操作前缀，取前 2 个实义词构成命令族 key。
    #
    # 示例:
    #   "aaa samlsp sp acs"      → aaa_samlsp   (set/show/no/clear 同族)
    #   "ha consistency on"      → ha_consistency
    #   "clear slb group member" → slb_group     (剥去 "clear")
    #   "clear ha group port"    → ha_group      (剥去 "clear")
    #   "show aaa method bind"   → aaa_method    (剥去 "show")
    #
    # 动机：原模块级分组产生 text unit 达 8000-16000 token，超出 local_context_build
    # source 预算（text_unit_prop=0.5 × max_context_tokens=12000 ≈ 6000 token），
    # 导致 sources 始终为 0，local context 只剩合成摘要句。
    # 命令族分组后每族约 2-15 条命令，text unit 约 200-2000 token，可完整进入 source 预算。
    #
    # 第二道保险：首轮 2-word 分组后若族>_SPLIT_THRESHOLD 条，自动下探到 3-word 前缀，
    # 防止 slb_group / slb_real 等大族依然超出 source 预算。
    _OP_FIRST_WORDS = {"clear", "no", "show", "display"}
    _SPLIT_THRESHOLD = 40  # 2-word 族超过此数量时继续拆到 3-word

    def _sig_words(chunk: dict) -> list:
        meta = chunk.get("metadata") or {}
        cmd = (meta.get("command_prefix") or "").strip()
        words = cmd.split()
        if words and words[0].lower() in _OP_FIRST_WORDS:
            words = words[1:]
        return words or [(meta.get("product_module") or "unknown").split("_")[0]]

    def _family_key(words: list, depth: int = 2) -> str:
        return "_".join(words[:depth]) if words else "unknown"

    # 第一遍：2-word 命令族分组
    first_pass: dict = {}
    excluded_blocks = 0
    for chunk in chunks:
        src = (chunk.get("metadata") or {}).get("source_file", "unknown")
        if src in _DEDICATED_RETRIEVER_SOURCES:
            excluded_blocks += 1
            continue
        words = _sig_words(chunk)
        key = f"{src}#{_family_key(words, depth=2)}"
        first_pass.setdefault(key, []).append((src, chunk, words))

    # 第二遍：过大族（>_SPLIT_THRESHOLD）下探到 3-word
    module_groups: dict = {}
    for two_w_key, items in first_pass.items():
        if len(items) > _SPLIT_THRESHOLD:
            for src, chunk, words in items:
                key = f"{src}#{_family_key(words, depth=3)}"
                module_groups.setdefault(key, []).append(chunk)
        else:
            for src, chunk, words in items:
                module_groups.setdefault(two_w_key, []).append(chunk)

    _m("prepare_input_documents: grouped into %d command-family docs (was module-level)", len(module_groups))

    documents = []
    for group_key, group_chunks in module_groups.items():
        source_file, _, mod_id = group_key.partition("#")
        # 所有数据均为 cli/reference，knowledge_layer 固定为 cli
        category = "cli/reference"
        layer = "cli"

        parts: list[str] = []
        for chunk in group_chunks:
            text = chunk.get("text", "") or chunk.get("page_content", "")
            if not text.strip():
                continue
            metadata = chunk.get("metadata", {})

            # 构建带元数据前缀的文本（仅保留通用字段）
            meta_prefix = []
            document_category = metadata.get("document_category", "") or category
            product_module = metadata.get("product_module", "unknown")

            if document_category:
                meta_prefix.append(f"[分类: {document_category}]")
            if product_module and product_module != "unknown":
                meta_prefix.append(f"[模块: {product_module}]")

            enriched = " ".join(meta_prefix) + "\n" + text if meta_prefix else text
            parts.append(enriched)

        merged_text = "\n\n".join(parts)
        if not merged_text.strip():
            continue

        doc_idx = len(documents)
        documents.append({
            "id": f"src_{doc_idx:04d}",
            "text": f"[模块: {mod_id}] [来源: {source_file}]\n\n{merged_text}",
            "title": f"{source_file}#{mod_id}",
            "metadata": {
                "source_file": source_file,
                "product_module": mod_id,
                "document_category": category,
                "knowledge_layer": layer,
                "block_count": len(group_chunks),
            }
        })

    output_path = input_dir / "documents.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(documents, f, ensure_ascii=False, indent=2)
    _m("prepare_input_documents DONE | %d module-docs written (excluded %d blocks), path=%s",
       len(documents), excluded_blocks, str(output_path))
    logger.info("已准备 %d 个模块文档到 %s（排除 %d blocks，原始 %d 个 block）",
                len(documents), input_dir, excluded_blocks, len(chunks))
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

        _invalidate_stale_output(workspace_dir, _m)

    (workspace_dir / "output").mkdir(exist_ok=True)
    (workspace_dir / "cache").mkdir(exist_ok=True)
    (workspace_dir / "logs").mkdir(exist_ok=True)
    _m("initialize_graphrag_index DONE | %s", str(workspace_dir))
    logger.info("GraphRAG 工作空间已初始化: %s", workspace_dir)
    return workspace_dir


def _input_fingerprint(workspace_dir: Path) -> str:
    import hashlib
    docs_path = workspace_dir / "input" / "documents.json"
    if not docs_path.exists():
        return ""
    h = hashlib.sha256()
    h.update(docs_path.read_bytes())
    return h.hexdigest()


def _invalidate_stale_output(workspace_dir: Path, _m=None) -> None:
    """Compare input fingerprint with last build; clear output/cache if input changed."""
    import shutil

    fp_path = workspace_dir / "output" / ".input_fingerprint"
    current_fp = _input_fingerprint(workspace_dir)
    if not current_fp:
        return

    if fp_path.exists():
        old_fp = fp_path.read_text(encoding="utf-8").strip()
        if old_fp == current_fp:
            return

    output_dir = workspace_dir / "output"
    has_parquets = any(output_dir.glob("*.parquet")) if output_dir.exists() else False
    if has_parquets:
        if _m:
            _m("Input fingerprint changed, clearing stale output/ and cache/")
        else:
            logger.info("Input fingerprint changed, clearing stale output/ and cache/")
        for f in output_dir.glob("*.parquet"):
            f.unlink()
        lancedb_dir = output_dir / "lancedb"
        if lancedb_dir.exists():
            shutil.rmtree(lancedb_dir, ignore_errors=True)
        cache_dir = workspace_dir / "cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_dir.mkdir(exist_ok=True)

    output_dir.mkdir(exist_ok=True)
    fp_path.write_text(current_fp, encoding="utf-8")


def _check_output_referential_integrity(output_dir: Path) -> Dict[str, Any]:
    import pandas as pd

    integrity: Dict[str, Any] = {
        "ok": True,
        "problems": [],
        "details": {},
    }

    try:
        entities_path = output_dir / "entities.parquet"
        relationships_path = output_dir / "relationships.parquet"
        text_units_path = output_dir / "text_units.parquet"
        documents_path = output_dir / "documents.parquet"

        if not (entities_path.exists() and relationships_path.exists() and text_units_path.exists()):
            integrity["skipped"] = True
            return integrity

        entities = pd.read_parquet(entities_path)
        relationships = pd.read_parquet(relationships_path)
        text_units = pd.read_parquet(text_units_path)
        documents = pd.read_parquet(documents_path) if documents_path.exists() else None

        def _normalize_refs(value: Any) -> List[str]:
            if value is None:
                return []
            if isinstance(value, str):
                raw_items = [value]
            else:
                if hasattr(value, "tolist"):
                    value = value.tolist()
                if isinstance(value, dict):
                    raw_items = list(value.values())
                elif isinstance(value, (list, tuple, set)):
                    raw_items = list(value)
                else:
                    raw_items = [value]

            normalized: List[str] = []
            for item in raw_items:
                if item is None:
                    continue
                try:
                    if pd.isna(item):
                        continue
                except Exception:
                    pass
                normalized.append(str(item))
            return normalized

        def _collect_refs(frame: Any, column: str) -> Tuple[set[str], int]:
            if column not in frame.columns:
                return set(), 0
            refs: set[str] = set()
            total = 0
            for value in frame[column]:
                items = _normalize_refs(value)
                refs.update(items)
                total += len(items)
            return refs, total

        def _record_problem(name: str, missing_refs: set[str], source_count: int, target_count: int) -> None:
            integrity["ok"] = False
            integrity["problems"].append({
                "name": name,
                "missing_count": len(missing_refs),
                "sample_missing": sorted(missing_refs)[:5],
                "source_count": source_count,
                "target_count": target_count,
            })

        text_unit_ids = {str(item) for item in text_units["id"].dropna().astype(str)} if "id" in text_units.columns else set()
        entity_text_unit_refs, entity_ref_total = _collect_refs(entities, "text_unit_ids")
        relationship_text_unit_refs, relationship_ref_total = _collect_refs(relationships, "text_unit_ids")

        integrity["details"].update({
            "entity_count": len(entities),
            "relationship_count": len(relationships),
            "text_unit_count": len(text_units),
            "entity_text_unit_ref_total": entity_ref_total,
            "relationship_text_unit_ref_total": relationship_ref_total,
        })

        missing_entity_text_unit_refs = entity_text_unit_refs - text_unit_ids
        if missing_entity_text_unit_refs:
            _record_problem(
                "entities.text_unit_ids_missing_in_text_units",
                missing_entity_text_unit_refs,
                len(entity_text_unit_refs),
                len(text_unit_ids),
            )

        missing_relationship_text_unit_refs = relationship_text_unit_refs - text_unit_ids
        if missing_relationship_text_unit_refs:
            _record_problem(
                "relationships.text_unit_ids_missing_in_text_units",
                missing_relationship_text_unit_refs,
                len(relationship_text_unit_refs),
                len(text_unit_ids),
            )

        if documents is not None and "id" in documents.columns:
            document_ids = {str(item) for item in documents["id"].dropna().astype(str)}
            text_unit_document_refs, text_unit_document_ref_total = _collect_refs(text_units, "document_ids")
            document_text_unit_refs, document_text_unit_ref_total = _collect_refs(documents, "text_unit_ids")

            integrity["details"].update({
                "document_count": len(documents),
                "text_unit_document_ref_total": text_unit_document_ref_total,
                "document_text_unit_ref_total": document_text_unit_ref_total,
            })

            missing_text_unit_document_refs = text_unit_document_refs - document_ids
            if missing_text_unit_document_refs:
                _record_problem(
                    "text_units.document_ids_missing_in_documents",
                    missing_text_unit_document_refs,
                    len(text_unit_document_refs),
                    len(document_ids),
                )

            missing_document_text_unit_refs = document_text_unit_refs - text_unit_ids
            if missing_document_text_unit_refs:
                _record_problem(
                    "documents.text_unit_ids_missing_in_text_units",
                    missing_document_text_unit_refs,
                    len(document_text_unit_refs),
                    len(text_unit_ids),
                )

        return integrity

    except Exception as exc:
        integrity["ok"] = False
        integrity["error"] = str(exc)
        integrity["problems"].append({
            "name": "referential_integrity_check_failed",
            "message": str(exc),
        })
        return integrity


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
        "errors": [],
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
        if result["index_built"]:
            integrity = _check_output_referential_integrity(output_dir)
            result["checks"]["referential_integrity"] = integrity
            if not integrity.get("ok", True):
                result["valid"] = False
                for problem in integrity.get("problems", []):
                    name = problem.get("name")
                    if name:
                        result["errors"].append(name)
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

    integrity = validation.get("checks", {}).get("referential_integrity")
    if integrity is not None:
        status["integrity_ok"] = integrity.get("ok", True)
        status["integrity_problems"] = integrity.get("problems", [])
    
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