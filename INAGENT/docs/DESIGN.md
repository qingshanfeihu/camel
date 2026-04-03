# INAGENT 项目设计文档

## 1. 项目概述

INAGENT 是一个基于 CAMEL 多智能体框架的自动化测试生成与执行系统，集成了 GraphRAG（图检索增强生成）和统一 RAG（检索增强生成）技术，用于自动化测试用例的生成、评审、执行和验证。

### 核心特性

- **多智能体协作**：基于 CAMEL Workforce 实现分布式任务处理
- **图检索增强**：集成 Microsoft GraphRAG 进行结构化知识检索
- **统一检索管道**：GraphRAG + 向量检索 + Rerank + 协议加权
- **自动化测试流水线**：从需求到测试执行的端到端自动化

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INAGENT 系统架构                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    知识层 (Knowledge Layer)                    │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────┐  │  │
│  │  │  Knowledge     │  │  GraphRAG     │  │    Unified RAG     │  │  │
│  │  │  Base          │  │  Index        │  │    Pipeline        │  │  │
│  │  │  (JSON)        │  │  (Parquet)    │  │    (Retrieval)     │  │  │
│  │  └──────────────┘  └──────────────┘  └─────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    检索层 (Retrieval Layer)                    │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────┐  │  │
│  │  │  Hybrid        │  │  GraphRAG     │  │    Reranker        │  │  │
│  │  │  Retriever     │  │  Retriever    │  │    (SiliconFlow)   │  │  │
│  │  │  (Qdrant)      │  │               │  │                    │  │  │
│  │  └──────────────┘  └──────────────┘  └─────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    智能体层 (Agent Layer)                      │  │
│  │  ┌─────────────────────────────────────────────────────────┐  │  │
│  │  │  Workforce (CAMEL Multi-Agent Framework)                │  │  │
│  │  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐  │  │  │
│  │  │  │ Coordinator│  │ Task     │  │ Workers  │  │ Pipeline│  │  │  │
│  │  │  │ Agent      │  │ Planner  │  │ (Single) │  │ Mode    │  │  │  │
│  │  │  └──────────┘  └──────────┘  └──────────┘  └─────────┘  │  │  │
│  │  └─────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    应用层 (Application Layer)                  │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────┐  │  │
│  │  │  Test Review   │  │  Config      │  │    Test Execution  │  │  │
│  │  │  Pipeline      │  │  Generation  │  │    Pipeline        │  │  │
│  │  └──────────────┘  └──────────────┘  └─────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 核心组件详解

### 3.1 知识库层 (Knowledge Base Layer)

#### 3.1.1 知识库结构

知识库以 `knowledge_base.json` 为核心，包含以下结构：

```json
{
  "chunks": [
    {
      "id": "doc_000001",
      "text": "配置HTTP类型的SLB服务...",
      "metadata": {
        "product_module": "SLB",
        "protocol_type": ["HTTP", "HTTPS"],
        "step_type": "configuration",
        "section_title": "HTTP服务配置",
        "document_category": "spec/design"
      }
    }
  ]
}
```

**关键元数据字段**：

- `product_module`: 产品模块（SLB/LLB/GSLB/基础网络）
- `protocol_type`: 协议类型（HTTP/HTTPS/TCP/UDP等）
- `step_type`: 步骤类型（configuration/validation/testing）
- `section_title`: 章节标题
- `document_category`: 文档分类（spec/design/test/test_list）

#### 3.1.2 知识库构建流程

知识库构建通过 `initialize_pipeline.py` 执行以下步骤：

```
┌─────────────────────────────────────────────────────────────────────┐
│                    知识库构建流程                                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  输入：                                                               │
│  └─ knowledge_base/ 目录（包含 PDF、Word、Markdown 等文档）          │
│                                                                       │
│  流程：                                                               │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ 步骤1: PDF识别和导入                                          │   │
│  │ ┌─────────────────────────────────────────────────────────┐ │   │
│  │ │ • 使用 MinerU 提取 PDF 内容                             │ │   │
│  │ │ • 识别文档结构（章节、表格、图片）                       │ │   │
│  │ │ • 生成 blocks（文本块）                                  │ │   │
│  │ └─────────────────────────────────────────────────────────┘ │   │
│  │                                                               │   │
│  │ 步骤2: 文档分类                                               │   │
│  │ ┌─────────────────────────────────────────────────────────┐ │   │
│  │ │ • 基于文件名模式匹配                                      │ │   │
│  │ │ • 基于内容关键词匹配                                      │ │   │
│  │ │ • 输出 document_category 和 confidence                  │ │   │
│  │ └─────────────────────────────────────────────────────────┘ │   │
│  │                                                               │   │
│  │ 步骤3: LLM元数据提取                                          │   │
│  │ ┌─────────────────────────────────────────────────────────┐ │   │
│  │ │ • 提取 product_module (产品模块)                        │ │   │
│  │ │ • 提取 protocol_type (协议类型)                         │ │   │
│  │ │ • 提取 step_type (步骤类型)                             │ │   │
│  │ │ • 提取 section_title (章节标题)                         │ │   │
│  │ │ • 增强 scenario_id (基于功能结构索引)                   │ │   │
│  │ └─────────────────────────────────────────────────────────┘ │   │
│  │                                                               │   │
│  │ 步骤4: 自动模块识别和索引更新                                 │   │
│  │ ┌─────────────────────────────────────────────────────────┐ │   │
│  │ │ • 从文件路径推断模块 (infer_module_from_path)           │ │   │
│  │ │ • 提取文档统计信息 (extract_document_statistics)        │ │   │
│  │ │ • 增量更新功能结构索引 (function_structure_index.json)  │ │   │
│  │ └─────────────────────────────────────────────────────────┘ │   │
│  │                                                               │   │
│  │ 步骤5: 生成 reference/*.json                                 │   │
│  │ ┌─────────────────────────────────────────────────────────┐ │   │
│  │ │ • 每个源文档生成独立的 JSON 文件                         │ │   │
│  │ │ • 包含 chunks 和 metadata                               │ │   │
│  │ │ • 输出到 knowledge_base/reference/                      │ │   │
│  │ └─────────────────────────────────────────────────────────┘ │   │
│  │                                                               │   │
│  │ 步骤6: 合并知识库                                             │   │
│  │ ┌─────────────────────────────────────────────────────────┐ │   │
│  │ │ • 合并所有 reference/*.json 为 knowledge_base.json      │ │   │
│  │ │ • 去重 (基于 content hash)                              │ │   │
│  │ │ • 生成最终知识库                                          │ │   │
│  │ └─────────────────────────────────────────────────────────┘ │   │
│  │                                                               │   │
│  │ 步骤7: RAG索引构建                                            │   │
│  │ ┌─────────────────────────────────────────────────────────┐ │   │
│  │ │ • 初始化 Hybrid Retriever (Qdrant)                      │ │   │
│  │ │ • 初始化 Reranker (SiliconFlow)                         │ │   │
│  │ │ • 可选构建 GraphRAG 索引                                 │ │   │
│  │ └─────────────────────────────────────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

**详细步骤说明**：

##### 步骤1: PDF识别和导入 (`auto_convert.py`)

**MinerU 提取流程**：

```python
# 使用 MinerU 提取 PDF 内容
reader = LocalMinerUReader()
blocks = reader.load_data(file_path="document.pdf")
```

**输出结构**：
```python
[
    {
        "page_idx": 0,
        "text": "第一章 产品概述",
        "type": "heading",
        "bbox": [x1, y1, x2, y2],
    },
    {
        "page_idx": 1,
        "text": "1.1 功能介绍\n\nSLB (Server Load Balancer) 是...",
        "type": "text",
        "bbox": [x1, y1, x2, y2],
    },
    # ... 更多 blocks
]
```

**前置页过滤**：
- 使用 LLM 识别目录、版权、声明、前言等前置页
- 自动过滤这些对检索无价值的页面

##### 步骤2: 文档分类 (`document_classifier.py`)

**分类体系**：

| 分类 | 描述 | 文件名模式 | 内容关键词 |
|------|------|-----------|-----------|
| `spec/prd` | 产品需求文档 | `*prd*`, `*requirement*` | Product Requirement, 产品需求 |
| `spec/func_spec` | 功能规格书 | `*func_spec*`, `*功能规格*` | Function Specification, 功能规格 |
| `spec/design` | 设计文档 | `*design*`, `*设计文档*` | Software Design, 架构设计 |
| `test/test_list` | 测试列表 | `*test*list*`, `*测试*列表*` | Test Types, Expected Result |
| `test/test_strategy` | 测试策略 | `*test*strateg*`, `*测试*策略*` | Test Strategy, 测试计划 |
| `test/test_template` | 测试模板 | `*test*template*`, `*测试*模板*` | 测试用例模板 |
| `cli/reference` | CLI命令参考 | `*cli*`, `*command*ref*` | CLI命令语法 |
| `app/reference` | 应用参考 | `*app*`, `*application*ref*` | Application API |

**分类策略**（按优先级）：

1. **文件名模式匹配**（高置信度 0.9）
   ```python
   # 示例
   "Test List HTTP2.0.json" → "test/test_list" (0.9)
   "HTTP2 Design Document.pdf" → "spec/design" (0.9)
   ```

2. **扩展名启发**（中等置信度 0.8）
   ```python
   # .xlsx/.xls 且文件名包含 test → test/test_list (0.8)
   ```

3. **内容关键词匹配**（中等置信度 0.7）
   ```python
   # 内容包含多个关键词 → 匹配分类 (0.7)
   ```

4. **扩展名回退**（低置信度 0.3-0.4）
   ```python
   # .xlsx/.xls → test/test_list (0.4)
   # .docx/.doc → spec/func_spec (0.3)
   ```

**分类函数**：
```python
def classify_document(
    file_path: Path,
    content_preview: str = "",
) -> Tuple[str, float]:
    """
    Returns:
        (document_category, confidence)  confidence 0.0~1.0
    """
```

##### 步骤3: LLM元数据提取

**提取内容**：

1. **product_module**（产品模块）
   - 从文件路径推断（如 `HTTP2/...` → HTTP2）
   - 从文档内容提取（使用 LLM）
   - 从功能结构索引匹配

2. **protocol_type**（协议类型）
   - HTTP/HTTPS/TCP/UDP/FTP/DNS/SIP/SMTP 等
   - 从文档内容提取
   - 支持多个协议（数组）

3. **step_type**（步骤类型）
   - configuration（配置步骤）
   - validation（验证步骤）
   - testing（测试步骤）

4. **section_title**（章节标题）
   - 提取文档的章节结构
   - 用于构建层级索引

5. **scenario_id**（场景ID）
   - 基于功能结构索引自动推断
   - 用于测试场景匹配

**提取流程**：
```python
# 1. 从 MinerU blocks 提取文本
blocks = mineru_reader.load_data(pdf_path)

# 2. 使用 LLM 提取元数据
metadata = llm_extractor.extract_metadata(blocks)

# 3. 增强 metadata（基于功能结构索引）
metadata = enhance_metadata_with_function_index(metadata)
```

##### 步骤4: 自动模块识别和索引更新 (`auto_document_integration.py`)

**功能结构索引** (`function_structure_index.json`)：

```json
{
  "modules": {
    "SLB": {
      "keywords": ["slb", "server load balancer", "负载均衡"],
      "commands": ["slb real", "slb group", "slb virtual"],
      "protocols": ["HTTP", "HTTPS", "TCP", "UDP"]
    },
    "HTTP2": {
      "keywords": ["http2", "http 2.0", "h2"],
      "commands": ["http2 enable", "http2 max_streams"],
      "protocols": ["HTTP2"]
    }
  },
  "metadata_statistics": {
    "product_modules": {
      "SLB": {"count": 150, "keywords": ["slb", "lb"]},
      "HTTP2": {"count": 50, "keywords": ["http2", "h2"]}
    },
    "protocol_types": {
      "HTTP": 200,
      "HTTPS": 180,
      "HTTP2": 50
    },
    "step_types": {
      "configuration": 300,
      "validation": 100
    }
  }
}
```

**模块推断**：
```python
def infer_module_from_path(
    pdf_path: Path,
    function_index: Optional[Dict[str, Any]] = None
) -> List[str]:
    """
    从文件路径推断模块（动态加载模块关键词，不硬编码）
    
    策略：
    1. 从功能结构索引加载所有已知模块及其关键词
    2. 基于这些关键词匹配路径
    3. 如果无法匹配，返回空列表
    """
```

**文档统计**：
```python
def extract_document_statistics(
    content_blocks: List[Dict]
) -> Dict[str, Any]:
    """
    从文档块中提取统计信息（模块、协议、步骤类型）
    
    Returns:
        {
            "product_modules": {"SLB": 10, "HTTP2": 5},
            "protocol_types": {"HTTP": 15, "HTTPS": 8},
            "step_types": {"configuration": 18},
            "keywords": ["slb", "http", "config", ...]
        }
    """
```

##### 步骤5: 生成 reference/*.json

**输出格式**：
```json
{
  "chunks": [
    {
      "page_content": "配置HTTP类型的SLB服务...",
      "metadata": {
        "source_file": "HTTP2 Design Document.json",
        "page_idx": 15,
        "block_id": "block_001",
        "product_module": "HTTP2",
        "protocol_type": ["HTTP", "HTTPS"],
        "step_type": "configuration",
        "section_title": "HTTP服务配置",
        "document_category": "spec/design"
      }
    }
  ]
}
```

**生成流程**：
```python
# 1. 读取 MinerU blocks
blocks = load_mineru_output(pdf_path)

# 2. 提取元数据
metadata = extract_metadata(blocks)

# 3. 生成 chunks
chunks = []
for i, block in enumerate(blocks):
    chunks.append({
        "page_content": block.text,
        "metadata": {
            **metadata,
            "page_idx": block.page_idx,
            "block_id": f"block_{i:03d}",
        }
    })

# 4. 保存为 JSON
save_json(output_path, {"chunks": chunks})
```

##### 步骤6: 合并知识库 (`merge_knowledge_base.py`)

**去重策略**：
```python
def merge_knowledge_base(
    reference_dir: Path,
    output_file: Path,
    deduplicate: bool = True,
) -> None:
    """
    Merge all JSON files under *reference_dir* into one knowledge_base.json.
    
    Args:
        deduplicate: Whether to skip duplicate chunks (based on content hash).
    """
    # 基于以下字段计算 hash
    dedup_parts = [
        str(chunk.get("page_content", "")),
        str(chunk["metadata"].get("source_file", "")),
        str(chunk["metadata"].get("page_idx", "")),
        str(chunk["metadata"].get("block_id", "")),
    ]
    h = hashlib.md5("|".join(dedup_parts).encode("utf-8")).hexdigest()
```

**输出结构**：
```json
{
  "chunks": [
    {
      "id": "doc_000001",
      "text": "配置HTTP类型的SLB服务...",
      "metadata": {
        "product_module": "SLB",
        "protocol_type": ["HTTP", "HTTPS"],
        "step_type": "configuration",
        "section_title": "HTTP服务配置",
        "document_category": "spec/design",
        "source_file": "HTTP2 Design Document.json",
        "page_idx": 15,
        "block_id": "block_001"
      }
    }
  ]
}
```

**统计信息**：
```
[merge] Done
  Total blocks: 15234
  Deduplicated: 1234
  Output: knowledge_base.json
  Metadata coverage:
    product_module: 14500 (95.2%)
    protocol_type: 13800 (90.6%)
    step_type: 12000 (78.8%)
    scenario_id: 11500 (75.5%)
```

##### 步骤7: RAG索引构建

**HybridRetriever 初始化**：
```python
from camel.retrievers import HybridRetriever
from camel.storages import QdrantStorage
from camel.embeddings import OpenAICompatibleEmbedding

# 初始化嵌入模型
embedding_model = OpenAICompatibleEmbedding(
    model_name="BAAI/bge-m3",
    model_provider="siliconflow",
)

# 初始化存储
storage = QdrantStorage(
    collection_name="inagent_knowledge",
    url="http://localhost:6333",
)

# 初始化混合检索器
hybrid_retriever = HybridRetriever(
    storage=storage,
    embedding_model=embedding_model,
)
```

**Reranker 初始化**：
```python
from INAGENT.rag.rerank_retriever import SiliconFlowRerankRetriever

reranker = SiliconFlowRerankRetriever(
    model_name="BAAI/bge-reranker-large",
    api_key=api_key,
    api_base=api_base,
)
```

**GraphRAG 索引构建**（可选）：
```python
from INAGENT.rag.graphrag_adapter import initialize_graphrag_index

# 构建 GraphRAG 索引
await initialize_graphrag_index(
    knowledge_base_path=knowledge_base_path,
    workspace_dir=graphrag_index_dir,
)
```

---

### 3.2 检索层 (Retrieval Layer)

---

### 3.2 检索层 (Retrieval Layer)

#### 3.2.1 HybridRetriever（混合检索器）

**实现位置**：`INAGENT/rag/` 相关模块

**功能**：结合 BM25 和向量检索的混合检索器

```python
from camel.retrievers import HybridRetriever
from camel.storages import QdrantStorage

hybrid_retriever = HybridRetriever(
    storage=QdrantStorage(),
    embedding_model=embedding_model,
)
```

**检索流程**：
1. BM25 关键词检索
2. 向量语义检索
3. RRF (Reciprocal Rank Fusion) 融合结果

#### 3.2.2 GraphRAGRetriever（图检索器）

**实现位置**：`INAGENT/rag/graphrag_integration.py`

**功能**：集成 Microsoft GraphRAG 进行图结构检索

**支持的检索类型**：

1. **Local Search**（本地搜索）
   - 适用于特定实体及其关系的查询
   - 检索实体、关系、社区报告、文本单元
   - 适合需要理解特定功能点的场景

2. **Global Search**（全局搜索）
   - 适用于高层次的概括性问题
   - 基于社区报告进行聚合回答
   - 适合需要理解整体架构的场景

3. **Drift Search**（DRIFT搜索）
   - 结合本地和全局搜索的优势
   - 适用于复杂的多跳推理查询

4. **Basic Search**（基础搜索）
   - 纯向量检索，不依赖图结构

**索引结构**：
- `entities.parquet`: 实体数据
- `relationships.parquet`: 关系数据
- `communities.parquet`: 社区划分
- `community_reports.parquet`: 社区报告
- `text_units.parquet`: 文本单元

**配置** (`graphrag_adapter.py`)：
```python
{
    "models": {
        "default_chat_model": {
            "type": "chat",
            "model_provider": "openai",
            "api_key": "${GRAPHRAG_API_KEY}",
            "api_base": "https://api.siliconflow.cn/v1",
            "model": "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
        },
        "default_embedding_model": {
            "type": "embedding",
            "model": "BAAI/bge-m3",
        },
    },
    "input": {
        "file_type": "json",
    },
    "vector_store": {
        "default_vector_store": {
            "type": "lancedb",
            "db_uri": "output/lancedb",
        },
    },
}
```

#### 3.2.3 UnifiedRAGRetriever（统一检索器）

**实现位置**：`INAGENT/rag/unified_rag.py`

**核心设计理念**：GraphRAG 优先 + 向量检索 + Rerank + 协议加权

**检索流水线**：

```
┌─────────────────────────────────────────────────────────────────┐
│                    UnifiedRAGRetriever 流水线                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. 构建查询列表                                                  │
│     ├─ rag_queries (任务分解生成的精确查询)                       │
│     ├─ 主 query (job_content 宽泛查询)                           │
│     └─ 补丁查询 (特殊场景自动注入)                                │
│                                                                  │
│  2. GraphRAG 图检索 (优先执行)                                    │
│     ├─ 对每条 query 执行 local_search                           │
│     ├─ 提取 entities/relationships/communities/text_units       │
│     └─ 合并去重                                                  │
│                                                                  │
│  3. 向量检索                                                     │
│     ├─ 对每条 query 执行 hybrid_retriever.query                 │
│     ├─ 提取 BM25 + 向量混合结果                                  │
│     └─ 合并去重                                                  │
│                                                                  │
│  4. 分类白名单硬过滤                                             │
│     └─ 仅保留 document_category 在 whitelist 中的文档           │
│                                                                  │
│  5. Rerank 重排序                                                │
│     └─ 使用主 query 进行重排序                                   │
│                                                                  │
│  6. 协议加权                                                     │
│     ├─ 提取查询和文档的 protocol_type                           │
│     ├─ 精准匹配 +0.05                                           │
│     ├─ 部分匹配 +0.02/-0.02                                     │
│     └─ 重排序                                                   │
│                                                                  │
│  7. 构建上下文与约束                                             │
│     ├─ 提取最终 top_k_final 文档                                │
│     ├─ 构建 context 文本                                         │
│     └─ 提取 constraints 约束字典                                 │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**关键改进点**：

1. **GraphRAG 优先**：先执行图检索获取结构化知识
2. **多查询合并**：将 rag_queries 逐条执行，获取更精确的候选
3. **分数修复**：正确读取 `rrf_score`（HybridRetriever 的真实分数）
4. **分类白名单**：硬过滤机制，确保只返回指定分类的文档
5. **协议加权**：动态计算协议类型匹配度，提升相关性

**API 接口**：

```python
def retrieve(
    self,
    query: str,
    top_k_retrieval: int = 50,
    top_k_final: int = 8,
    use_graphrag: bool = True,
    decomposition_result: Optional[Dict[str, Any]] = None,
    document_category_filter: Optional[str] = None,
    category_whitelist: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Returns:
        (context 文本, constraints 约束字典, decomposition_result)
    """
```

---

### 3.3 智能体层 (Agent Layer)

#### 3.3.1 Workforce（工作队列）

**实现位置**：`camel/societies/workforce/`

**核心概念**：基于 CAMEL 的多智能体协作框架

**工作模式**：

1. **PIPELINE 模式**：流水线模式
   - 顺序执行任务
   - 支持 fork-join 并行化
   - 适用于测试评审、测试执行等场景

2. **DISTRIBUTED 模式**：分布式模式
   - 任务自动分发给空闲 Worker
   - 适用于大规模并行任务

**关键组件**：

- **Coordinator Agent**：协调者，监控 Worker 进度和质量
- **Task Agent**：任务分派员，根据任务内容分派给合适的 Worker
- **Worker**：执行具体任务的智能体（SingleAgentWorker）

**Pipeline 编排**：

```python
workforce.pipeline_add(task1) \
         .pipeline_fork([task2, task3, task4]) \
         .pipeline_join(task5) \
         .pipeline_build()
```

**拓扑示例**：

```
Task1 → fork [Task2, Task3, Task4] → join Task5
```

#### 3.3.2 NoteTakingToolkit（笔记工具）

**功能**：提供智能体间的共享笔记功能

**核心方法**：

```python
note_tk = NoteTakingToolkit(working_directory=str(work_dir))

# 写入笔记
note_tk.write_note(
    note_name="product_knowledge",
    note_content="产品知识内容...",
)

# 读取笔记
content = note_tk.read_note("product_knowledge")

# 删除笔记
note_tk.delete_note("product_knowledge")
```

**使用场景**：
- KnowledgeResearchWorker 将检索结果写入笔记
- 其他 Worker 读取笔记获取上下文
- 实现智能体间的异步协作

#### 3.3.3 KnowledgeToolkit（知识工具）

**实现位置**：`INAGENT/toolkits/knowledge_toolkit.py`

**功能**：将 INAGENT 知识库暴露为 CAMEL FunctionTool

**提供的工具**：

1. **search_product_knowledge**
   - 通过 UnifiedRAGRetriever 检索产品知识
   - 自动携带 mode 对应的 category_whitelist
   - 支持 category_filter 进一步过滤

2. **get_review_rules**
   - 获取测试用例编写规范
   - 获取评审检查清单（R01-R14）
   - 返回确定性的规则文本

3. **search_similar_tests**
   - 通过 TestRulesEngine 搜索已有测试用例
   - 用于去重和参考

**Mode 配置** (`knowledge_config.py`)：

```python
MODE_CATEGORY_WHITELIST = {
    "config": ["spec/design", "cli/reference"],
    "test_write": ["spec/design", "test/test_list"],
    "test_review": ["spec/design", "test/test_list", "cli/reference"],
    "explain": ["spec/design", "cli/reference", "test/test_list"],
}
```

---

### 3.3.4 TestRulesEngine（测试规则引擎）

**实现位置**：`INAGENT/rag/test_rules.py`

**功能**：管理测试用例的规范、模板、评审检查清单

**核心组件**：

#### 3.3.4.1 测试类型 (Test Type)

```python
TEST_TYPES = {
    "正向测试": "验证功能正常工作",
    "负向测试": "验证异常输入被正确处理",
    "边界测试": "验证边界条件",
    "压力测试": "验证高负载下的稳定性",
    "兼容性测试": "验证不同配置下的兼容性",
    "回归测试": "验证已有功能未被破坏",
}
```

#### 3.3.4.2 优先级 (Priority Level)

```python
PRIORITY_LEVELS = {
    "P0": "最高优先级，核心功能必须覆盖",
    "P1": "高优先级，重要功能必须覆盖",
    "P2": "中优先级，常规功能建议覆盖",
    "P3": "低优先级，边缘场景可选覆盖",
}
```

#### 3.3.4.3 Case ID 格式

```python
CASE_ID_FORMAT = {
    "pattern": "SLB-HTTP2-XXX",
    "description": "产品模块-协议-序号",
    "fields": {
        "产品模块": "SLB/LLB/GSLB/基础网络",
        "协议": "HTTP/HTTPS/TCP/UDP 等",
        "序号": "三位数字，按功能点编号",
    },
}
```

#### 3.3.4.4 测试用例模板

```python
TEST_CASE_TEMPLATE = {
    "fields": [
        {
            "name": "Case ID",
            "required": True,
            "description": "唯一标识符，格式: SLB-HTTP2-001",
        },
        {
            "name": "Description",
            "required": True,
            "description": "测试场景描述，标准格式: [功能点] [操作] [预期]",
        },
        {
            "name": "Expected Result",
            "required": True,
            "description": "预期结果，必须可验证",
        },
        {
            "name": "Test Type",
            "required": True,
            "description": "测试类型（正向/负向/边界/压力/兼容性/回归）",
        },
        {
            "name": "Priority",
            "required": True,
            "description": "优先级（P0/P1/P2/P3）",
        },
        {
            "name": "Product Module",
            "required": True,
            "description": "产品模块（SLB/LLB/GSLB/基础网络）",
        },
        {
            "name": "Protocol",
            "required": True,
            "description": "协议类型（HTTP/HTTPS/TCP/UDP/HTTP2等）",
        },
        {
            "name": "CLI Command",
            "required": False,
            "description": "涉及的CLI命令",
        },
        {
            "name": "Tags",
            "required": False,
            "description": "标签（用于分类和检索）",
        },
    ],
    "description_format": "[功能点] [操作] [预期]",
}
```

#### 3.3.4.5 评审检查清单 (Review Checklist)

```python
REVIEW_CHECKLIST = [
    {
        "id": "R01",
        "check": "Case ID 格式正确",
        "severity": "必须",
    },
    {
        "id": "R02",
        "check": "Description 格式符合标准",
        "severity": "必须",
    },
    {
        "id": "R03",
        "check": "Expected Result 可验证",
        "severity": "必须",
    },
    {
        "id": "R04",
        "check": "Test Type 合理",
        "severity": "必须",
    },
    {
        "id": "R05",
        "check": "Priority 设置合理",
        "severity": "必须",
    },
    {
        "id": "R06",
        "check": "Product Module 正确",
        "severity": "必须",
    },
    {
        "id": "R07",
        "check": "Protocol 正确",
        "severity": "必须",
    },
    {
        "id": "R08",
        "check": "CLI Command 正确",
        "severity": "必须",
    },
    {
        "id": "R09",
        "check": "字段完整性（必填字段不缺失）",
        "severity": "必须",
    },
    {
        "id": "R10",
        "check": "无重复用例",
        "severity": "必须",
    },
    {
        "id": "R11",
        "check": "覆盖度分析（功能点、边界、异常）",
        "severity": "建议",
    },
    {
        "id": "R12",
        "check": "测试类型分布合理",
        "severity": "建议",
    },
    {
        "id": "R13",
        "check": "CLI 命令语法正确",
        "severity": "必须",
    },
    {
        "id": "R14",
        "check": "Expected Result 可执行",
        "severity": "必须",
    },
]
```

#### 3.3.4.6 规则识别机制

**输入识别流程**：

```
待评审测试用例文本
    │
    ▼
TestRulesEngine.get_rules_context(purpose="review")
    │
    ├─ 提取测试类型定义
    ├─ 提取优先级定义
    ├─ 提取 Case ID 格式
    ├─ 提取测试用例字段模板
    ├─ 提取 Description 标准格式
    └─ 提取评审检查清单 (R01-R14)
    │
    ▼
构建规则上下文文本
    │
    ▼
发送给 LLM Prompt
    │
    ▼
LLM 基于规则进行评审
```

**规则上下文示例**：

```markdown
# 测试用例规范

## 测试类型 (Test Type)
- **正向测试**: 验证功能正常工作
- **负向测试**: 验证异常输入被正确处理
- **边界测试**: 验证边界条件
- **压力测试**: 验证高负载下的稳定性
- **兼容性测试**: 验证不同配置下的兼容性
- **回归测试**: 验证已有功能未被破坏

## 优先级 (Priority)
- **P0**: 最高优先级，核心功能必须覆盖
- **P1**: 高优先级，重要功能必须覆盖
- **P2**: 中优先级，常规功能建议覆盖
- **P3**: 低优先级，边缘场景可选覆盖

## Case ID 格式
格式: SLB-HTTP2-XXX
说明: 产品模块-协议-序号
  - 产品模块: SLB/LLB/GSLB/基础网络
  - 协议: HTTP/HTTPS/TCP/UDP等
  - 序号: 三位数字，按功能点编号

## 测试用例字段
- **Case ID** (必填): 唯一标识符，格式: SLB-HTTP2-001
- **Description** (必填): 测试场景描述，标准格式: [功能点] [操作] [预期]
- **Expected Result** (必填): 预期结果，必须可验证
- **Test Type** (必填): 测试类型（正向/负向/边界/压力/兼容性/回归）
- **Priority** (必填): 优先级（P0/P1/P2/P3）
- **Product Module** (必填): 产品模块（SLB/LLB/GSLB/基础网络）
- **Protocol** (必填): 协议类型（HTTP/HTTPS/TCP/UDP/HTTP2等）
- **CLI Command** (选填): 涉及的CLI命令
- **Tags** (选填): 标签（用于分类和检索）

## Description 格式
标准格式: [功能点] [操作] [预期]

## 评审检查清单
- [必须] R01: Case ID 格式正确
- [必须] R02: Description 格式符合标准
- [必须] R03: Expected Result 可验证
- [必须] R04: Test Type 合理
- [必须] R05: Priority 设置合理
- [必须] R06: Product Module 正确
- [必须] R07: Protocol 正确
- [必须] R08: CLI Command 正确
- [必须] R09: 字段完整性（必填字段不缺失）
- [必须] R10: 无重复用例
- [建议] R11: 覆盖度分析（功能点、边界、异常）
- [建议] R12: 测试类型分布合理
- [必须] R13: CLI 命令语法正确
- [必须] R14: Expected Result 可执行
```

#### 3.3.4.7 测试用例模板应用

**模板用途**：

1. **编写测试用例** (`purpose="write"`)
   - 提供字段定义和格式要求
   - 提供 Description 标准格式
   - 不包含评审检查清单（避免干扰）

2. **评审测试用例** (`purpose="review"`)
   - 包含所有编写规则
   - 额外包含 R01-R14 评审检查清单
   - 用于 SpecComplianceWorker 进行规范检查

**使用场景**：

```python
# 在 ReviewPipeline 中使用
engine = TestRulesEngine()
rules_context = engine.get_rules_context(purpose="review")
# 返回包含评审检查清单的完整规则
```

#### 3.3.4.8 相似测试搜索

**功能**：从已有测试列表中搜索相似项

**算法**：
```python
def search_similar_tests(
    self,
    query: str,
    product_module: str = "",
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """
    1. 将 query 分词
    2. 遍历所有测试项
    3. 计算匹配关键词数量
    4. 按匹配度排序
    5. 返回 top_k 个结果
    """
```

**搜索字段**：
- `text`: 测试项全文（包含 Description、Expected Result 等）
- `product_module`: 产品模块过滤

**输出格式**：
```markdown
[参考测试项 1] (Test List HTTP_2_new_cli.json)
Description: 验证HTTP2多路复用功能
Expected Result: 连接建立成功，多路复用生效

[参考测试项 2] (Test List HTTP_2_new_cli.json)
Description: 测试HTTP2头部压缩
Expected Result: 头部被压缩，传输效率提升
```

**应用场景**：
1. **去重**：评审时检查是否已有相似用例
2. **参考**：编写时参考已有用例的格式
3. **补充**：发现已有用例未覆盖的场景

---

### 3.4 应用层 (Application Layer)

#### 3.4.1 测试评审流水线 (Test Review Pipeline)

**实现位置**：`INAGENT/review/pipeline.py`

**版本**: v0.8.1 (2026-03-29)

**Pipeline 拓扑** (v0.7.0+ Workforce 架构):

```
fork ─┬─ CoverageAnalysisWorker [11 tools + ProductMemory]
      ├─ CLISyntaxCheckWorker [5 tools]
      ├─ SpecComplianceWorker [5 tools]
      └─ LoadStressWorker [5 tools]
join → ReviewSynthesisWorker [6 tools]
```

**v0.8.1 关键增强**:
- CoverageWorker 新增"配置并存/隔离"分析维度
- 全局审计横切面模块保护：`_CROSS_CUTTING_KEYWORDS` code guard
- Bug 121100 基线验证: 83% 命中率 (9 项人工评审)

**Worker 详解**：

1. **KnowledgeResearchWorker**（知识研究员）
   - 检索评审规范（review_rules）
   - 检索产品设计/功能规格（product_knowledge）
   - 检索已有类似测试用例（similar_tests）
   - 检索 CLI 命令语法（cli_reference）
   - 将检索结果写入共享笔记

2. **CoverageAnalysisWorker**（覆盖度分析专家）
   - 读取 product_knowledge 和 similar_tests 笔记
   - 分析功能覆盖度
   - 检查边界条件、异常场景、测试类型分布
   - 写入 coverage_analysis 笔记

3. **CLISyntaxCheckWorker**（CLI语法检查专家）
   - 读取 cli_reference 笔记
   - 验证 CLI 命令语法正确性
   - 检查命令是否存在、参数格式、命令层级
   - 写入 cli_syntax_check 笔记

4. **SpecComplianceWorker**（规范符合度检查专家）
   - 读取 review_rules 笔记
   - 检查格式规范（必填字段、Case ID 格式）
   - 检查 Description 格式
   - 检查 Expected Result 可验证性
   - 检查重复用例
   - 写入 spec_compliance 笔记

5. **ReviewSynthesisWorker**（综合报告撰写者）
   - 读取三个专项分析笔记
   - 综合生成最终评审报告
   - 输出 [问题摘要] 区块

**API 接口**：

```python
class ReviewPipeline:
    def __init__(self, router, model, product_name: str):
        self.router = router
        self.model = model
        self.product_name = product_name

    def run(
        self,
        test_cases_text: str,
        bug_profile: Optional[Dict[str, Any]] = None,
    ) -> ReviewResult:
        """执行测试评审"""
```

#### 3.4.2 配置生成流水线 (Config Generation Pipeline)

**实现位置**：`INAGENT/workflow_config_generator.py`

**功能**：根据需求生成配置命令

**流程**：

```
1. 读取 jobs 目录下的需求文件
2. 任务分解 Agent 分析需求
   ├─ 提取关键功能点
   ├─ 生成 rag_queries (精确查询列表)
   └─ 识别高级特性（健康检查、故障注入等）
3. RAG 检索相关配置文档
   ├─ 使用 UnifiedRAGRetriever
   ├─ GraphRAG + 向量检索 + Rerank
   └─ 提取配置示例
4. LB Ops Agent 生成配置命令
   ├─ 基于检索到的配置示例
   ├─ 生成完整的配置命令序列
   └─ 输出 config_commands
5. 输出配置结果
```

**任务分解**：

```python
{
    "rag_queries": [
        {"query": "HTTP2.0 HPACK 头部压缩配置", "priority": 1},
        {"query": "HTTP2.0 多路复用配置", "priority": 2},
    ],
    "advanced_features": [
        "content_based_health_check",
        "fault_injection",
    ],
}
```

#### 3.4.3 测试执行流水线 (Test Execution Pipeline)

**实现位置**：`INAGENT/workforce_pipeline.py`

**Pipeline 拓扑**：

```
Deploy ──► fork ─┬── VerifyShow ──┐
                 └── Traffic ─────┘── join → Analysis ──► Cleanup
```

**Worker 详解**：

1. **DeployWorker**（配置下发专家）
   - 使用 execute_config_commands 下发配置
   - 查询 CLI 语法（search_product_knowledge）
   - 写入 deploy_result 笔记

2. **VerifyShowWorker**（状态验证专家）
   - 执行 show 命令验证配置
   - 补充查询（如果输出不足）
   - 写入 verify_result 笔记

3. **TrafficWorker**（流量探测专家）
   - verify_vip_traffic 探测 VIP 连通性
   - run_fault_injection_step 执行故障注入
   - wait_for_health_convergence 等待收敛
   - check_device_health_status 查询状态

4. **AnalysisWorker**（结果分析专家）
   - 综合 Deploy、VerifyShow、Traffic 输出
   - 判定测试是否通过
   - 输出 Verdict: PASS/FAIL

5. **CleanupWorker**（环境清理专家）
   - 生成 no/delete 删除命令
   - 停止 VM HTTP 服务

**API 接口**：

```python
async def run_workforce_pipeline_async(
    job_content: str,
    config_commands: List[str],
    verify_commands: List[str],
    env_plan: Dict[str, Any],
    model: object,
) -> Dict[str, Any]:
    """执行阶段 5-9 的测试执行流水线"""
```

---

## 4. GraphRAG 集成详解

### 4.1 集成架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                      GraphRAG 集成架构                               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  知识库构建阶段：                                                     │
│                                                                       │
│  knowledge_base.json                                                 │
│       │                                                               │
│       ▼                                                               │
│  convert_knowledge_base_to_graphrag_input()                         │
│       │                                                               │
│       ▼                                                               │
│  input/documents.json (增强格式)                                     │
│       │                                                               │
│       ▼                                                               │
│  GraphRAG build_index()                                              │
│       │                                                               │
│       ├─ extract_graph (实体/关系提取)                              │
│       ├─ summarize_descriptions (描述汇总)                          │
│       ├─ create_communities (社区划分)                              │
│       ├─ create_community_reports (社区报告生成)                    │
│       ├─ create_base_text_units (文本单元创建)                      │
│       └─ create_final_text_units (最终文本单元)                     │
│       │                                                               │
│       ▼                                                               │
│  graphrag_index/output/                                              │
│   ├─ entities.parquet                                                │
│   ├─ relationships.parquet                                           │
│   ├─ communities.parquet                                             │
│   ├─ community_reports.parquet                                       │
│   ├─ text_units.parquet                                              │
│   └─ lancedb/ (向量索引)                                             │
│                                                                       │
│  检索阶段：                                                           │
│                                                                       │
│  查询请求                                                             │
│       │                                                               │
│       ▼                                                               │
│  GraphRAGRetriever.local_search()                                    │
│       │                                                               │
│       ├─ 读取 entities.parquet                                       │
│       ├─ 读取 relationships.parquet                                  │
│       ├─ 读取 communities.parquet                                    │
│       ├─ 读取 community_reports.parquet                             │
│       └─ 读取 text_units.parquet                                    │
│       │                                                               │
│       ▼                                                               │
│  graphrag.api.local_search()                                         │
│       │                                                               │
│       ▼                                                               │
│  响应文本 + 检索结果                                                  │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 实体类型定义

```python
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
```

### 4.3 配置生成

`graphrag_adapter.py` 提供配置生成功能：

```python
def generate_graphrag_settings(config: GraphRAGConfig, output_dir: Path) -> Path:
    """生成 GraphRAG settings.yaml 配置文件"""
    settings = {
        "models": {
            "default_chat_model": {
                "model_provider": "openai",
                "api_base": config.api_base,
                "model": config.chat_model,
            },
            "default_embedding_model": {
                "model": config.embedding_model,
            },
        },
        "vector_store": {
            "default_vector_store": {
                "type": "lancedb",
                "db_uri": "output/lancedb",
            },
        },
    }
```

---

## 5. 流水线设计模式

### 5.1 PipelineTaskBuilder

**功能**：简化 Pipeline 任务构建的辅助类

**核心方法**：

```python
class PipelineTaskBuilder:
    def add(
        self,
        content: str,
        task_id: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
        additional_info: Optional[Dict[str, Any]] = None,
        auto_depend: bool = True,
    ) -> PipelineTaskBuilder:
        """添加任务"""
    
    def fork(
        self,
        tasks: List[Union[Task, Dict[str, Any]]],
        dependencies: Optional[List[str]] = None,
    ) -> PipelineTaskBuilder:
        """Fork 任务（并行化）"""
    
    def join(
        self,
        task: Union[Task, Dict[str, Any]],
        dependencies: Optional[List[str]] = None,
    ) -> PipelineTaskBuilder:
        """Join 任务（合并）"""
```

### 5.2 Task 属性传递

**关键改进**：保留 Task 的完整属性

```python
# 修复前：只提取 content，丢失 id 和 additional_info
Task(content=task.content)

# 修复后：保留所有属性
Task(
    content=task.content,
    id=task.id,
    dependencies=task.dependencies,
    additional_info=task.additional_info,
)
```

**使用场景**：
- PipelineTaskBuilder 在 fork/join 时保留 Task 属性
- Worker 可以通过 additional_info 获取结构化上下文
- 实现任务间的精确依赖和数据传递

---

## 6. 数据流设计

### 6.1 测试评审数据流

```
用户输入
   │
   ├─ test_cases_text (待评审测试用例)
   └─ bug_profile (变更描述，可选)
   │
   ▼
ReviewPipeline.run()
   │
   ├─ KnowledgeResearchWorker
   │   ├─ search_product_knowledge (检索产品知识)
   │   ├─ get_review_rules (获取评审规范)
   │   ├─ search_similar_tests (搜索相似用例)
   │   └─ write_note (写入共享笔记)
   │
   ├─ fork
   │   ├─ CoverageAnalysisWorker (读笔记 → 分析覆盖度 → 写笔记)
   │   ├─ CLISyntaxCheckWorker (读笔记 → 检查语法 → 写笔记)
   │   └─ SpecComplianceWorker (读笔记 → 检查规范 → 写笔记)
   │
   └─ join
       └─ ReviewSynthesisWorker (读笔记 → 综合报告)
           │
           ▼
        ReviewResult
            ├─ plan (评审计划)
            ├─ knowledge (知识摘要)
            ├─ review (评审报告)
            └─ elapsed_seconds (耗时)
```

### 6.2 测试执行数据流

```
用户输入
   │
   └─ job_content (测试任务)
   │
   ▼
pipeline_runner.py: run_test_job()
   │
   ├─ 阶段1: plan_network_environment
   │   └─ env_setup_agent → env_plan (IP规划)
   │
   ├─ 阶段2: deploy_test_environment
   │   └─ VMController → 部署VM服务
   │
   ├─ 阶段3: generate_config_commands
   │   └─ workflow_config_generator → config_commands
   │
   ├─ 阶段4: vip_precheck
   │   └─ 预检查配置
   │
   └─ 阶段5-9: Workforce Pipeline
       │
       ├─ DeployWorker (下发配置)
       ├─ fork
       │   ├─ VerifyShowWorker (验证配置)
       │   └─ TrafficWorker (流量探测)
       │
       └─ join
           └─ AnalysisWorker (分析结果)
               │
               ▼
            Verdict: PASS/FAIL
```

---

## 7. 工具包详解

### 7.1 NSAEDeviceToolkit

**功能**：NSAE 设备操作工具

**核心方法**：

```python
class NSAEDeviceToolkit:
    def execute_config_commands(self, commands: str) -> str:
        """执行配置命令"""
    
    def execute_show_commands(self, commands: str) -> str:
        """执行 show 命令"""
    
    def get_module_config(self, module: str) -> str:
        """获取模块配置"""
```

### 7.2 TrafficVerifyToolkit

**功能**：流量验证工具

**核心方法**：

```python
class TrafficVerifyToolkit:
    def verify_vip_traffic(
        self,
        url: str,
        keyword: Optional[str] = None,
    ) -> Dict[str, Any]:
        """验证 VIP 流量"""
    
    def run_fault_injection_step(
        self,
        step: Dict[str, Any],
    ) -> Dict[str, Any]:
        """执行故障注入"""
    
    def wait_for_health_convergence(
        self,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """等待健康检查收敛"""
    
    def check_device_health_status(self) -> Dict[str, Any]:
        """查询设备健康状态"""
```

### 7.3 VMControllerToolkit

**功能**：VM 控制工具

**核心方法**：

```python
class VMControllerToolkit:
    def deploy_http_server(
        self,
        html_content: str,
        port: int,
        bind_ip: str,
    ) -> Dict[str, Any]:
        """部署 HTTP 服务"""
    
    def stop_http_server(self, port: int) -> Dict[str, Any]:
        """停止 HTTP 服务"""
```

---

## 8. 配置管理

### 8.1 环境变量

```bash
# LLM 配置
GRAPHRAG_API_KEY=your_api_key
GRAPHRAG_API_BASE=https://api.siliconflow.cn/v1

# GraphRAG 配置
GRAPHRAG_CONCURRENT_REQUESTS=50
GRAPHRAG_TOKENS_PER_MINUTE=500000

# 超时配置
INAGENT_VERIFY_WORKER_TIMEOUT=420
INAGENT_TRAFFIC_WORKER_TIMEOUT=600
```

### 8.2 知识库配置

```python
# knowledge_config.py
MODE_CATEGORY_WHITELIST = {
    "config": ["spec/design", "cli/reference"],
    "test_write": ["spec/design", "test/test_list"],
    "test_review": ["spec/design", "test/test_list", "cli/reference"],
    "explain": ["spec/design", "cli/reference", "test/test_list"],
}
```

---

## 9. 性能优化

### 9.1 检索优化

1. **动态候选池策略**
   - top_k_retrieval = top_k_rerank × retrieval_multiplier (默认10倍)
   - 扩大初始候选池，提高召回率

2. **GraphRAG 优先**
   - 先执行图检索获取结构化知识
   - 再执行向量检索补充

3. **多查询合并**
   - 任务分解生成的 rag_queries 逐条执行
   - 确保精确命中

4. **分类白名单硬过滤**
   - 避免不相关文档进入后续流程
   - 提升检索效率

### 9.2 并行化优化

1. **Workforce PIPELINE 模式**
   - fork-join 并行化
   - 多 Worker 并行执行

2. **异步检索**
   - GraphRAG local_search 异步执行
   - 提高检索效率

---

## 10. 扩展性设计

### 10.1 新增 Worker

```python
# 1. 定义 Worker Agent
new_agent = ChatAgent(
    system_message=BaseMessage.make_assistant_message(
        role_name="NewWorker",
        content="你的职责是...",
    ),
    model=model,
    tools=note_tk.get_tools(),
)

# 2. 注册 Worker
workforce.add_single_agent_worker(
    "NewWorker: 描述",
    new_agent,
)

# 3. 添加 Pipeline 任务
new_task = Task(
    content="任务内容",
    id="new_task",
)
workforce.pipeline_add(new_task)
```

### 10.2 新增检索策略

```python
# 实现新的 Retriever 类
class NewRetriever:
    def retrieve(self, query: str) -> List[Dict[str, Any]]:
        # 实现检索逻辑
        pass

# 集成到 UnifiedRAGRetriever
unified_rag = UnifiedRAGRetriever(
    hybrid_retriever=hybrid_retriever,
    new_retriever=NewRetriever(),
)
```

---

## 11. 运行方式

### 11.1 初始化知识库

```bash
python -m INAGENT.initialize_pipeline
```

### 11.2 运行测试评审

```bash
python -m INAGENT.run_review
```

### 11.3 运行配置生成

```bash
python -m INAGENT.workflow_config_generator
```

### 11.4 运行测试执行

```bash
python -m INAGENT.pipeline_runner
```

---

## 12. 总结

INAGENT 项目通过以下核心特性实现了自动化测试的智能化：

1. **多智能体协作**：基于 CAMEL Workforce 实现分布式任务处理
2. **图检索增强**：集成 GraphRAG 进行结构化知识检索
3. **统一检索管道**：GraphRAG + 向量检索 + Rerank + 协议加权
4. **自动化流水线**：从需求到测试执行的端到端自动化
5. **工具化设计**：模块化的工具包和智能体设计

该架构具有良好的扩展性和可维护性，支持快速添加新的功能和优化。
