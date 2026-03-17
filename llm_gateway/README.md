# INFOAGEN LLM Gateway

统一的LLM网关服务，支持多模型并发调用、统一限速和OpenAI兼容接口。

## 在 INFOAGEN 流程中的位置

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ LLM Gateway ──→ PDF Processing ──→ Unified RAG + GraphRAG ──→ Workflow Jobs│
│ (端口 9000)     (MinerU 提取)       (可选, 有则增强/无则回退)  (生成/执行/测试) │
│     ▲                                                                       │
│     └── 当前组件: 多模型 Race 模式, OpenAI 兼容 API                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

> **启动方式**: `llm_gateway\start_gateway.bat` 或 `llm_gateway\deploy.bat`
> **默认端口**: 9000 (对外服务) / 8000 (内部容器)

## 功能特性

- ✅ **多模型并发调用**：同时使用3个对话模型（GLM-Z1-9B、Qwen3-8B、DeepSeek-R1-Qwen3-8B）
- ✅ **Race模式**：并发调用所有模型，返回最快响应
- ✅ **三级限速**：全局、提供商、模型三级速率限制
- ✅ **OpenAI兼容**：完全兼容OpenAI API格式
- ✅ **统一管理**：集中配置和监控
- ✅ **详细日志**：完整的请求和响应日志

## 架构说明

```
┌─────────────┐
│   应用层     │  (INAGENT模块)
└──────┬──────┘
       │ OpenAI API
┌──────▼──────┐
│  网关层      │  (LLM Gateway)
│  - 限速      │
│  - 路由      │
│  - 监控      │
└──────┬──────┘
       │ 并发调用 (Race Mode)
┌──────▼──────┐
│  模型层      │  (SiliconFlow)
│  - GLM-Z1-9B │
│  - Qwen3-8B  │
│  - DeepSeek  │
└─────────────┘
```

## 快速开始

### 1. 部署网关服务

```bash
# Windows
llm_gateway\deploy.bat

# Linux/Mac
cd llm_gateway && ./deploy.sh
```

### 2. 测试服务

```bash
# Windows
llm_gateway\test.bat

# Linux/Mac
python llm_gateway/test_gateway.py
```

### 3. 查看日志

```bash
podman logs -f infoagen-llm-gateway
```

### 4. 查看指标

```bash
curl http://localhost:8000/metrics
```

## API接口

### 1. 健康检查

```bash
GET http://localhost:8000/health
```

响应：
```json
{
  "status": "healthy",
  "mode": "race",
  "chat_models": 3,
  "embedding_models": 1,
  "reranker_models": 1
}
```

### 2. 对话补全 (OpenAI兼容)

```bash
POST http://localhost:8000/v1/chat/completions
Content-Type: application/json

{
  "model": "glm-z1-9b",
  "messages": [
    {"role": "user", "content": "Hello!"}
  ],
  "temperature": 0.7,
  "max_tokens": 100
}
```

响应头：
- `X-Model-ID`: 实际使用的模型ID
- `X-Model-Name`: 实际使用的模型名称
- `X-Provider`: 提供商名称
- `X-Elapsed-Time`: 模型响应时间

### 3. 向量嵌入 (OpenAI兼容)

```bash
POST http://localhost:8000/v1/embeddings
Content-Type: application/json

{
  "model": "BAAI/bge-m3",
  "input": "Your text here"
}
```

### 4. 速率限制指标

```bash
GET http://localhost:8000/metrics
```

响应：
```json
{
  "global": null,
  "providers": {
    "siliconflow": {
      "rpm": {"current": 10, "limit": 3000, "available": 2990},
      "tpm": {"current": 1500, "limit": 150000, "available": 148500}
    }
  },
  "models": {
    "glm-z1-9b": {
      "rpm": {"current": 5, "limit": 1000, "available": 995},
      "tpm": {"current": 800, "limit": 50000, "available": 49200}
    }
  }
}
```

## 配置说明

### config.yaml

```yaml
# 调用模式
calling_mode: "race"  # race(并发最快), balance(轮询), hybrid(智能)

# 提供商配置
providers:
  siliconflow:
    enabled: true
    base_url: "${SILICONFLOW_API_BASE_URL}"
    api_key: "${SILICONFLOW_API_KEY}"
    
    # 对话模型
    chat_models:
      - id: "glm-z1-9b"
        model: "THUDM/GLM-Z1-9B-0414"
        limits: {rpm: 1000, tpm: 50000}
      
      - id: "qwen3-8b"
        model: "Qwen/Qwen3-8B"
        limits: {rpm: 1000, tpm: 50000}
      
      - id: "deepseek-r1-qwen3-8b"
        model: "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"
        limits: {rpm: 1000, tpm: 50000}
    
    # 向量模型
    embedding_models:
      - id: "bge-m3"
        model: "BAAI/bge-m3"
        limits: {rpm: 2000, tpm: 500000}
    
    # 重排序模型
    reranker_models:
      - id: "bge-reranker-v2-m3"
        model: "BAAI/bge-reranker-v2-m3"
        limits: {rpm: 2000, tpm: 500000}
```

## 速率限制

### 模型限制（每个模型）
- **对话模型**：RPM=1000, TPM=50000
- **向量模型**：RPM=2000, TPM=500000
- **重排序模型**：RPM=2000, TPM=500000

### 提供商限制（SiliconFlow总和）
- RPM=3000 (3个对话模型共享)
- TPM=150000

### 限速算法
- 滑动窗口算法（60秒窗口）
- 线程安全实现
- 自动等待和重试

## 集成方式

### Python客户端

```python
from openai import OpenAI

# 使用网关而不是直接调用SiliconFlow
client = OpenAI(
    api_key="dummy",  # 网关会自动处理认证
    base_url="http://localhost:8000/v1"
)

# 对话补全 - 自动使用race模式
response = client.chat.completions.create(
    model="glm-z1-9b",  # 任意一个模型ID即可
    messages=[
        {"role": "user", "content": "Hello!"}
    ]
)

print(response.choices[0].message.content)

# 向量嵌入
embedding = client.embeddings.create(
    model="BAAI/bge-m3",
    input="Your text here"
)
```

### 修改现有代码

将原有代码中的：
```python
# 旧代码
client = OpenAI(
    api_key=os.getenv("SILICONFLOW_API_KEY"),
    base_url="https://api.siliconflow.cn/v1"
)
```

改为：
```python
# 新代码 - 使用网关
client = OpenAI(
    api_key="dummy",  # 网关内部处理
    base_url="http://localhost:8000/v1"
)
```

## 监控和管理

### 查看实时日志
```bash
podman logs -f infoagen-llm-gateway
```

### 查看容器状态
```bash
podman ps --filter "name=infoagen-llm-gateway"
```

### 查看速率限制统计
```bash
curl http://localhost:8000/metrics | python -m json.tool
```

### 重启服务
```bash
podman restart infoagen-llm-gateway
```

### 停止服务
```bash
podman-compose -f llm_gateway/podman-compose.yml down
```

## 调用模式说明

### 重要：Gateway 自动路由

**LLM Gateway 完全忽略客户端传入的 `model` 参数**，由 Gateway 根据配置的模式自动选择模型。这意味着：

- GraphRAG、Workflow 等客户端无需关心具体使用哪个模型
- 客户端配置的 `model` 字段仅作占位符（如 `gateway-auto`）
- Gateway 统一管理模型选择、负载均衡和故障转移

### Balance模式（推荐用于批量处理）

Balance 模式使用轮询策略，实现真正的多模型并行处理：

```
多个并发请求时：
请求1 ──→ Gateway ──→ GLM-Z1-9B   (队列1)
请求2 ──→ Gateway ──→ Qwen3-8B    (队列2)
请求3 ──→ Gateway ──→ DeepSeek    (队列3)
请求4 ──→ Gateway ──→ GLM-Z1-9B   (队列1, 轮询)
...
```

**优势**
- ✅ 多个请求并行处理，不相互阻塞
- ✅ 每个请求只消耗一个模型的配额（节省 RPM/TPM）
- ✅ 自动故障转移，某模型失败自动切换到下一个
- ✅ 适合 GraphRAG 批量索引构建等高并发场景

### Race模式（适用于低延迟场景）

Race 模式并发调用所有模型，返回最快响应：

1. **并发调用**：3个模型同时开始处理请求
2. **最快获胜**：哪个模型先返回结果就使用哪个
3. **取消剩余**：获得第一个结果后取消其他请求

**优势**
- ✅ 显著降低响应延迟
- ✅ 自动容错

**成本考虑**
- ⚠️ 令牌消耗增加（最多3倍）
- ⚠️ API调用次数增加
- ⚠️ 需要更高的速率限制配额

## GraphRAG 并行处理配置

### 推荐配置（2026-01-30）

```yaml
# INAGENT/graphrag_workspace/settings.yaml
default_chat_model:
  api_base: http://127.0.0.1:9000/v1
  model: gateway-auto           # 占位符，Gateway 会忽略
  concurrent_requests: 8        # 充分利用 3 个模型的并行能力
  requests_per_minute: 100      # 客户端并发限制
  tokens_per_minute: 50000
```

### 并行处理架构

```
┌─────────────────────────────────────────────────────────┐
│               GraphRAG (concurrent_requests: 8)         │
│                                                         │
│    ┌────────┐ ┌────────┐ ┌────────┐ ... ┌────────┐     │
│    │Chunk 1 │ │Chunk 2 │ │Chunk 3 │     │Chunk 8 │     │
│    └───┬────┘ └───┬────┘ └───┬────┘     └───┬────┘     │
│        │          │          │              │          │
└────────┼──────────┼──────────┼──────────────┼──────────┘
         │          │          │              │
         ▼          ▼          ▼              ▼
    ┌─────────────────────────────────────────────┐
    │          LLM Gateway (Balance Mode)         │
    │                                             │
    │   ┌──────────┐ ┌──────────┐ ┌──────────┐   │
    │   │GLM-Z1-9B │ │ Qwen3-8B │ │ DeepSeek │   │
    │   │  队列1   │ │  队列2   │ │  队列3   │   │
    │   └──────────┘ └──────────┘ └──────────┘   │
    │        ▲            ▲            ▲         │
    │        └─── 轮询分配请求 ────────┘         │
    └─────────────────────────────────────────────┘
```

### 理论性能

| 配置 | 说明 |
|------|------|
| 3 个模型 | 最多 3 个请求同时处理 |
| concurrent_requests: 8 | 充分利用模型队列 |
| 总吞吐量 | ~3x 单模型吞吐量 |

## 故障排查

### 1. 网关无法启动

检查日志：
```bash
podman logs infoagen-llm-gateway
```

常见原因：
- 环境变量未设置（检查 `INAGENT/.env`）
- 端口被占用（修改 `config.yaml` 中的 `port`）
- 配置文件格式错误（验证 YAML 语法）

### 2. 模型调用失败

检查指标：
```bash
curl http://localhost:8000/metrics
```

常见原因：
- API密钥无效
- 速率限制超限
- 网络连接问题

### 3. Race模式不生效

检查配置：
```yaml
calling_mode: "race"  # 确保设置为race
```

检查模型状态：
```yaml
chat_models:
  - enabled: true  # 确保所有模型都启用
```

## 性能优化

### 1. 调整超时时间

修改 `gateway.py`：
```python
self.client = httpx.AsyncClient(timeout=60.0)  # 调整超时
```

### 2. 调整速率限制

修改 `config.yaml`：
```yaml
limits:
  rpm: 2000  # 提高限制
  tpm: 100000
```

### 3. 切换调用模式

```yaml
calling_mode: "balance"  # 降低成本，轮询使用模型
```

## 许可证

Apache License 2.0

## 支持

如有问题，请联系开发团队或提交Issue。
