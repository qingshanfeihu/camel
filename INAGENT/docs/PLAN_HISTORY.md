# Plan: 统一测试评审流水线（自主 Agent Pipeline）

> **⚠ 历史文档 (v1/v2 设计)**：此文档描述了 ReviewPipeline 的初始两步式设计
> （plan → knowledge → review，3 次 LLM 调用）。实际实现已演进至 **v4 — Workforce
> 多 Agent 架构**，使用 5 个并行 Worker (Coverage / CLI Syntax / Spec Compliance /
> Load-Stress / Synthesis) + Manager 统一编排。原始的 `_plan()` 和 `_acquire_knowledge()`
> 已被 `_build_deterministic_plan()` + `ReviewInputBuilder.build()` 取代。
>
> 当前架构参见: [ARCHITECTURE.md](ARCHITECTURE.md) § Pipeline B
>
> **v0.8.1 (2026-03-29) 基线验证结果**:
> - Bug 121100 (Cookie会话保持加密): 9 项人工评审基线，AI 命中率 **83%** (7.5/9)
> - 修复: 横切面模块 Scope 保护 (Fix G) + 配置并存检查 (Fix H)
> - 详见 [CHANGELOG.md](CHANGELOG.md) v0.8.1

## TL;DR
当前 Bug 定向评审和全模块评审都存在：产品知识缺失(DESIGN层被弱化)、协议深度不够(LLM通用知识未引导)、prompt hardcode(NSAE/InfosecOS)。
方案：设计一个两步式自主 Agent Pipeline，第一步让 LLM 自由分析输入内容并输出结构化"评审计划"（包括需要查什么知识、从什么维度评审），第二步系统自动执行计划（查 RAG、执行评审）。没有任何硬编码的阶段或 query，一切由 LLM 根据输入内容自主决定。

## 设计原则
- 零硬编码：pipeline 本身不含任何领域关键字、固定 query、预设评审维度
- 输入驱动：LLM 阅读用例内容后自己决定需要什么知识、从什么角度评审
- 可跳过：如果 LLM 判断不需要 RAG（已有足够知识），knowledge_queries 为空数组，系统直接跳过检索
- 参照模式：复用 task_decomposition_agent.py 的 JSON 输出 -> 驱动 RAG 查询模式

## 用户决策
- 范围: 统一框架（Bug定向 + 全模块评审共用）
- 通用化: chat.py + 评审脚本的 prompt 全部从配置读取产品名
- LLM成本: 3次调用可接受
- 自主性: 两步式——LLM 先自由分析，再根据分析结果自动决定后续步骤

## Steps

### Phase A: 产品名通用化（前置，独立）

1. 在 INAGENT/utils/env_utils.py 新增 get_product_name() 函数，读环境变量 INAGENT_PRODUCT_NAME，默认 "NSAE (InfosecOS) 负载均衡器"
2. 修改 INAGENT/web/routers/chat.py 中 system prompt，用 get_product_name() 替换硬编码
3. 修改 run_review.py 中的 prompt，同理

### Phase B: 自主评审 Pipeline 核心框架（重新设计）

4. 新建 INAGENT/review/__init__.py + INAGENT/review/pipeline.py
   - class ReviewPipeline
   - __init__(self, router, model, product_name: str)
   - run(self, test_cases_text: str, bug_profile: Optional[Dict] = None) -> ReviewResult

5. Step 1: 规划（Planning） — _plan(self, input_text: str) -> ReviewPlan

   LLM 作为测试评审规划者，阅读完整输入（测试用例文本 + 变更描述），自由分析后输出 JSON：

   {
     "understanding": "（自由文本）LLM 对这些用例涉及的技术领域、产品功能的分析",
     "scope_analysis": {
       "change_summary": "LLM 对本次变更内容的理解（从 bug_profile 或用例上下文推断）",
       "in_scope": "与本次变更直接关联的测试范围描述",
       "out_of_scope": "已有基础功能用例，本次无需重新评审的范围描述",
       "scope_rationale": "范围划定的依据"
     },
     "knowledge_queries": [
       {
         "query": "LLM 自己决定的搜索文本",
         "scope": "product | test | command",
         "reason": "为什么需要查这个"
       }
     ],
     "review_dimensions": [
       "LLM 自己决定的评审维度（如：功能覆盖完整性、协议状态验证、异常场景、边界条件...）"
     ]
   }

   Planning prompt 的关键：完全通用，无领域关键词。

   scope 到 router mode 的映射（pipeline 内部的唯一映射表）：
   - "product" -> mode="explain"
   - "test" -> mode="test_review"
   - "command" -> mode="config"

   解析：复用 re.search(r"\{[\s\S]*\}", text) + json.loads() 模式。
   容错：如果 JSON 解析失败，fallback 为空 knowledge_queries + 默认 review_dimensions=["功能覆盖", "用例规范性"]。

6. Step 1.5: 知识获取（Knowledge Acquisition） — _acquire_knowledge(self, plan: ReviewPlan) -> str

   系统自动执行，不调用 LLM。

   A. LLM 请求的知识（可选）
   - 遍历 plan.knowledge_queries
   - 每条 query 按 scope->mode 映射调用 router.retrieve(query=q["query"], mode=mapped_mode)
   - 如果 knowledge_queries 为空 -> 跳过

   B. 用例规范（必选，Pipeline 结构性保障）
   - 始终调用 rules_engine.get_rules_context("review")
   - 始终调用 rules_engine.search_similar_tests(query=输入摘要)

7. Step 2: 评审（Review） — _review(self, input_text: str, plan: ReviewPlan, knowledge: str) -> str

   LLM 作为测试评审执行者，使用 plan 中信息执行评审：
   - 角色："你是 {product_name} 的测试用例评审专家。"
   - 范围：如 scope_analysis 非空，聚焦 in_scope，out_of_scope 仅做规范快检
   - 维度：注入 plan.review_dimensions
   - 背景：注入 plan.understanding + knowledge_context

8. 数据模型
   - ReviewPlan: understanding, scope_analysis, knowledge_queries, review_dimensions
   - ReviewResult: plan, knowledge, review, elapsed_seconds

### Phase C: 适配现有脚本

9. 重写 run_review.py 的 call_agent()/review_module()，改为调用 ReviewPipeline

### Phase D: 验证

11. 运行 Bug 139213 / L4 Faststack，检查计划、检索、评审质量
12. 检查 Step 1.5 检索命中
13. 检查最终评审质量提升

## Relevant files
- INAGENT/web/routers/chat.py
- INAGENT/rag/knowledge_router.py
- INAGENT/rag/unified_rag.py
- INAGENT/rag/test_rules.py
- INAGENT/agents/task_decomposition_agent.py
- INAGENT/workflow_config_generator.py
- INAGENT/web/deps.py
- INAGENT/utils/env_utils.py
- INAGENT/review/__init__.py
- INAGENT/review/pipeline.py
- run_review.py

## Verification
1. run_review.py 输出 Step 1 ReviewPlan JSON
2. knowledge_queries 为空时跳过 RAG 且不报错
3. Step 2 报告引用 Step 1.5 知识与理解
4. review_dimensions 驱动评审角度合理
5. 消除硬编码产品名
6. run_review.py bug_profile=None 路径可工作
