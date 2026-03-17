# LLM Gateway 故障排查

## 500 Internal Server Error：Request timed out

### 现象

```
POST /v1/chat/completions HTTP/1.1" 500 Internal Server Error
[MultiModelCaller] ✗ Model qwen3-8b failed after 60.14s: Request timed out.
Exception: Model call failed: Request timed out.
```

### 触发原因

1. **上游 API 超时**：对上游（如 SiliconFlow）的 chat 请求在 **60 秒**内未返回，httpx/OpenAI 客户端抛出超时。
2. **Balance 模式**：当前仅启用一个 chat 模型（如 qwen3-8b）时，该模型超时后没有可切换的下一个模型，Gateway 抛出异常并返回 500（或 504，见下）。

可能的上游慢/超时原因：

- 网络延迟或抖动
- 上游服务负载高、排队
- 单次请求体大（长上下文、多 token）
- 模型冷启动或长推理

### 处理建议

1. **增大 chat 超时**（`config.yaml`）  
   - 将 `request_timeout.chat` 从 60 改为 120 或 180（秒），适用于长文本、复杂推理场景。

2. **区分超时与其它错误**  
   - Gateway 对「请求超时」类错误会返回 **504 Gateway Timeout**（不再用 500），便于客户端区分并做重试/降级。

3. **多模型容错**  
   - 在 `config.yaml` 中启用多个 chat 模型（如 qwen3-8b + glm-z1-9b），balance 模式在某一模型超时后会尝试下一个。

4. **观察上游**  
   - 若频繁超时，检查上游服务状态、限流、以及单次请求的 token 数/上下文长度是否过大。
