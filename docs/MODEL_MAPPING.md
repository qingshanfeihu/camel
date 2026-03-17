# 模型对应关系与流程说明（INFOAGEN）

## 一、总体流程（按执行顺序）
1. 文档输入：INAGENT/doc_local
2. Auto Convert 文档处理（MinerU PDF 解析 + LLM 元数据提取）
3. GraphRAG 建索引/检索（可选）
4. Workflow：任务分解 → RAG 检索 → 配置生成
5. 输出：doc_local/mineru_output、workflow 日志与配置结果

## 二、各阶段模型对应关系（当前配置）

| 阶段 | 组件 | 使用模型类型 | 模型名来源 | 实际调用通道 | 配置位置 |
|---|---|---|---|---|---|
| Auto Convert | MinerU PDF 解析 | 多模态/视觉模型 | vLLM /v1/models 返回的模型 ID（当前为 mineru-vlm） | 本地 vLLM（8000） | INAGENT/auto_convert.py + MinerU 服务 |
| Auto Convert | 元数据提取/前置页过滤 | 对话模型 | LLM_GATEWAY_CHAT_MODEL | LLM Gateway（9000） | INAGENT/auto_convert.py + INAGENT/.env |
| Auto Convert | 统一重试与限速 | N/A | N/A | LLM Gateway | INAGENT/auto_convert.py |
| GraphRAG | 图构建/摘要/查询 | 对话模型 | LLM_GATEWAY_CHAT_MODEL | LLM Gateway（9000） | INAGENT/graphrag_test/settings.yaml |
| GraphRAG | 向量化 | 嵌入模型 | LLM_GATEWAY_EMBEDDING_MODEL | LLM Gateway（9000） | INAGENT/graphrag_test/settings.yaml |
| Workflow | 任务分解 Agent | 对话模型 | LLM_GATEWAY_CHAT_MODEL | LLM Gateway（9000） | INAGENT/workflow_config_generator.py + INAGENT/.env |
| Workflow | RAG 向量检索 | 嵌入模型 | LLM_GATEWAY_EMBEDDING_MODEL | LLM Gateway（9000） | INAGENT/workflow_config_generator.py + INAGENT/.env |
| Workflow | Rerank | 重排序模型 | LLM_GATEWAY_RERANK_MODEL | LLM Gateway（9000） | INAGENT/siliconflow_rerank_retriever.py + INAGENT/.env |

## 三、LLM Gateway 负责的模型路由
LLM Gateway 只负责“LLM/Embedding/Rerank”统一转发，不参与 MinerU 的 PDF 解析。

- LLM Gateway 配置入口：llm_gateway/config.yaml- 当前支持的模型：
  - **对话模型 (3个)**：THUDM/GLM-Z1-9B-0414, Qwen/Qwen3-8B, deepseek-ai/DeepSeek-R1-0528-Qwen3-8B
  - **嵌入模型 (2个)**：BAAI/bge-m3 (1024维), netease-youdao/bce-embedding-base_v1 (768维)
  - **重排序模型 (1个)**：BAAI/bge-reranker-v2-m3
- 限速配置（与 SiliconFlow 官方一致）：
  - 对话模型：RPM=1000, TPM=50000
  - 嵌入模型：RPM=2000, TPM=500000
  - 重排序模型：RPM=2000, TPM=500000- 关键环境变量（统一从 INAGENT/.env 读取）：
  - LLM_GATEWAY_BASE_URL
  - LLM_GATEWAY_API_KEY
  - LLM_GATEWAY_CHAT_MODEL
  - LLM_GATEWAY_EMBEDDING_MODEL
  - LLM_GATEWAY_RERANK_MODEL

## 四、关键文件清单（建议核对）
- Auto Convert：INAGENT/auto_convert.py
- 统一 LLM 配置：INAGENT/llm_config.py
- Workflow 主流程：INAGENT/workflow_config_generator.py
- GraphRAG 配置：INAGENT/graphrag_test/settings.yaml
- Gateway 配置：llm_gateway/config.yaml
- 全局环境变量：INAGENT/.env

## 五、让 LLM Gateway 承担所有模型转发的落地动作（已完成）
1. GraphRAG 的 api_base/api_key/model 改为 LLM Gateway 变量（已改）
2. Workflow 的 LLM 创建改为 OpenAI 兼容平台（已改）
3. LLM Gateway 禁用 vLLM provider，仅保留上游 LLM provider（已改）
4. LLM_GATEWAY_* 变量在 INAGENT/.env 明确指定（已改）

## 六、注意事项
- vLLM 仅用于 MinerU PDF 解析，不应在 LLM Gateway 中注册或参与路由。
- 如需切换 LLM 模型，只需修改 INAGENT/.env 的 LLM_GATEWAY_* 变量。
- 修改 llm_gateway/config.yaml 后，需要重启 Gateway 才会生效。
- **Embedding 格式**：Gateway 强制使用 `encoding_format="float"` 请求上游 API，确保返回浮点数组而非 base64 编码字符串，避免 Pydantic 序列化警告（gateway.py line 512）。

## 七、建议的验证路径
1. 确保 MinerU vLLM 正常：
   - 访问 http://127.0.0.1:8000/v1/models
2. 确保 LLM Gateway 正常：
   - 访问 http://127.0.0.1:9000/v1/models
3. 重新运行主流程并观察日志
