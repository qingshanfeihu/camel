# LLM Gateway 限速机制详解

## 一、SiliconFlow 官方限速规则

根据 [SiliconFlow 官方文档](https://docs.siliconflow.cn/cn/userguide/rate-limits/rate-limit-and-upgradation)：

### 1.1 限速是按模型单独设置的

每个模型有独立的限速配额，**一个模型请求超出限速不影响其他模型正常使用**。

| 模型类型 | 模型名称 | RPM | TPM |
|---------|---------|-----|-----|
| 对话 | THUDM/GLM-Z1-9B-0414 | 1,000 | 50,000 |
| 对话 | Qwen/Qwen3-8B | 1,000 | 50,000 |
| 对话 | deepseek-ai/DeepSeek-R1-0528-Qwen3-8B | 1,000 | 50,000 |
| 嵌入 | BAAI/bge-m3 | 2,000 | 500,000 |
| 嵌入 | netease-youdao/bce-embedding-base_v1 | 2,000 | 500,000 |
| 重排序 | BAAI/bge-reranker-v2-m3 | 2,000 | 500,000 |

### 1.2 限速触发条件

**限制可能会因在任一选项中达峰而触发，取决于哪个先发生：**

- **RPM（Requests Per Minute）**：每分钟请求数
- **TPM（Tokens Per Minute）**：每分钟 Token 数

**例如**：RPM 限制为 1000，TPM 限制为 50000 时
- 场景 1：一分钟内发送了 1000 个请求（即使每个请求只有 10 tokens，总共 10K tokens）→ **RPM 限制触发**
- 场景 2：一分钟内发送了 100 个请求，但每个请求 600 tokens，总共 60K tokens → **TPM 限制触发**

### 1.3 限速主体：账户级别

- Rate Limit 在**用户账户**维度定义，不是 API Key 维度
- 意味着所有 API Key 共享同一个账户限速配额

---

## 二、Gateway 的分层限速机制

### 2.1 两层限速结构

```
Provider 级别限速（整个 SiliconFlow 账户）
  ├─ Chat 模型 1: RPM=1000, TPM=50000
  ├─ Chat 模型 2: RPM=1000, TPM=50000
  ├─ Chat 模型 3: RPM=1000, TPM=50000
  ├─ Embedding 模型 1: RPM=2000, TPM=500000
  ├─ Embedding 模型 2: RPM=2000, TPM=500000
  └─ Reranker 模型: RPM=2000, TPM=500000
```

### 2.2 当前配置

**Provider 级别**（SiliconFlow 账户总限制）：
```yaml
limits:
  rpm: 5000      # 保守估计：3×1000(chat) + 2000(buffer) = 5000
  tpm: 800000    # 保守估计：50K×3(chat) + 500K(embedding) + buffer = 800000
```

**模型级别**（各模型独立限制）：
```yaml
chat_models:
  - glm-z1-9b: RPM=1000, TPM=50000      ✓ 与官方一致
  - qwen3-8b: RPM=1000, TPM=50000       ✓ 与官方一致
  - deepseek-r1-qwen3-8b: RPM=1000, TPM=50000 ✓ 与官方一致

embedding_models:
  - bge-m3: RPM=2000, TPM=500000        ✓ 与官方一致
  - bce-embedding-base: RPM=2000, TPM=500000 ✓ 与官方一致

reranker_models:
  - bge-reranker-v2-m3: RPM=2000, TPM=500000 ✓ 与官方一致
```

---

## 三、为什么会触发限速？

### 3.1 真实场景分析（来自日志 16:18:35）

**请求日志**：
```
[INFO] [Gateway] Received embedding request: model=BAAI/bge-m3 (共 30+ 次)
[INFO] [Gateway] ✓ Embedding generated: 50 vectors (每次 50 个向量)
[WARNING] Rate limit reached, waiting 29.30s (RPM=29/3000, TPM=149811/150000)
```

**触发原因分析**：

1. **单个 Embedding 请求的 Token 消耗**
   - 输入：假设平均 200 个字符 ≈ 60-80 tokens
   - 输出：50 个向量 × (1 token/向量) ≈ 50 tokens
   - 单个请求总计：~100-150 tokens

2. **累积计算**
   - 30 个请求 × 150 tokens = 4,500 tokens（低估）
   - 实际上可能 30 个请求 × 4,000-5,000 tokens = 120,000-150,000 tokens
   - **达到 Provider TPM 限制：150,000 tokens/分钟（旧配置）**

3. **触发条件**
   ```
   当前状态：RPM=29/3000 (99.67%)，TPM=149,811/150,000 (99.87%)
   ┌─────────────────────────────────────────────┐
   │ 两个条件都接近限制，TPM 先达到 100% 触发   │
   │ Gateway 等待 29.30 秒直到计数窗口重置      │
   └─────────────────────────────────────────────┘
   ```

### 3.2 问题根源

**旧配置的 TPM 限制太低**：
- 只有 150,000 tokens/分钟
- 一个嵌入模型的官方限制是 500,000 tokens/分钟
- 矛盾 ❌：单个模型的限制 > 整个 Provider 的限制

**新配置解决**：
- Provider TPM 提升到 800,000
- 允许多个嵌入模型并发工作
- 与实际使用场景匹配 ✓

---

## 四、限速等待机制

当达到限速时，Gateway 会：

1. **检测**：识别是 RPM 还是 TPM 达到限制
2. **计算等待时间**：基于当前消耗占比计算需要等待多长时间直到计数窗口重置
3. **等待**：暂停请求处理，等待指定时间
4. **重试**：计数窗口重置后继续处理请求

**日志示例**：
```
[WARNING] Rate limit reached, waiting 29.30s (RPM=29/3000, TPM=149811/150000)
↑                                      ↑                      ↑           ↑
│                                      │                      └───────────┘
│                                      │                      已消耗 / 限制
│                                      等待 29.30 秒
触发限速警告
```

---

## 五、最佳实践

### 5.1 如何避免频繁触发限速

1. **批量处理**：将多个请求合并
   ```python
   # ❌ 不好：逐个发送 30 个请求
   for text in texts:
       response = client.embeddings.create(input=text)
   
   # ✓ 好：批量发送
   response = client.embeddings.create(input=texts)  # 批量可以包含多个文本
   ```

2. **监控 TPM**：关注 Token 消耗而不仅仅是请求数
   - 长输入文本消耗更多 token
   - 合理分页和批量化输入

3. **使用异步/并发**：充分利用 RPM 限额
   - RPM 还有余量时，可以提升并发度
   - 同时发送多个请求利用 RPM 配额

### 5.2 监控限速事件

查看 Gateway 日志找出限速事件：
```bash
grep "Rate limit reached" llm_gateway/logs/gateway_*.log
```

---

## 六、配置总结

| 维度 | RPM | TPM | 说明 |
|------|-----|-----|------|
| **Provider** (SiliconFlow 账户) | 5000 | 800000 | 账户总限制 |
| 对话模型（单个） | 1000 | 50000 | 官方限制 |
| 嵌入模型（单个） | 2000 | 500000 | 官方限制 |
| 重排序模型（单个） | 2000 | 500000 | 官方限制 |

**关键点**：
- ✓ Provider 限制 ≥ 所有单个模型限制的合理组合
- ✓ 每个模型有独立的限速，互不干扰
- ✓ 触发限制时会自动等待并重试

---

## 七、常见问题

**Q: 为什么一个模型触发限速不影响其他模型？**  
A: 因为每个模型的计数器独立维护。模型 A 达到 RPM 限制不会阻止模型 B 发送请求。

**Q: RPM 和 TPM 哪个更容易触发？**  
A: 取决于使用场景：
- 短请求 + 高并发 → 容易触发 RPM
- 长请求 + 大量数据 → 容易触发 TPM

**Q: 限速计数如何重置？**  
A: 采用滑动窗口（1 分钟）：最早的请求超过 1 分钟后自动从计数中移除。

