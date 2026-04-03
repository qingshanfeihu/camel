# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
知识分类配置 — 定义文档分类体系与模式白名单

所有模式对 GraphRAG 的检索范围由此处的白名单统一管控。
workflow（pipeline、interactive_cli、web 等调用方）不需要知道底层用的是
GraphRAG / 向量 / BM25，只需声明 mode，路由器会自动约束检索范围。
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List

# ── 一级文档分类（document_category） ──────────────────────────────
# 这些值必须与 auto_document_integration / graphrag_adapter 中
# 写入元数据的 document_category 字段严格一致。
DOCUMENT_CATEGORIES = [
    "cli/reference",           # CLI 命令语法手册
    "app/reference",           # 应用配置参考(WebUI 等)
    "architecture/design",     # 产品架构设计文档
    "spec/prd",                # 产品需求文档 (PRD)
    "spec/func_spec",          # 软件功能规格书
    "spec/design",             # 软件设计文档
    "test/test_list",          # 测试用例清单
    "test/test_strategy",      # 测试策略/计划
    "test/test_template",      # 测试用例模板
    "review/rules",            # 测试评审规则/标准
    "review/bug_fix",          # Bug 修复说明/根因与修复细节
]

# ── 模式 → 允许的文档分类白名单 ────────────────────────────────────
# 每个模式只会检索白名单中的分类。
# 检索时由 KnowledgeRouter 将白名单传递给 UnifiedRAGRetriever，
# 后者将其作为 GraphRAG/向量检索的 document_category 过滤条件。
MODE_CATEGORY_WHITELIST: Dict[str, List[str]] = {
    "config": [
        "cli/reference",
        "app/reference",
        "spec/design",
    ],
    "test_write": [
        "spec/prd",
        "spec/func_spec",
        "test/test_template",
        "test/test_strategy",
        "test/test_list",
        "review/rules",
    ],
    "test_review": [
        "review/rules",
        "review/bug_fix",
        "test/test_list",
        "spec/prd",
        "spec/func_spec",
        "spec/design",
        "architecture/design",
        "cli/reference",
        "app/reference",
    ],
    "explain": [
        "architecture/design",
        "spec/design",
        "spec/prd",
        "spec/func_spec",
        "app/reference",
        "cli/reference",
    ],
}

# ── 模式 → 检索上下文字符预算权重 ─────────────────────────────────
# 权重越高，分配到的上下文字符数越多。总量由调用方的 max_context_chars 决定。
MODE_BUDGET_WEIGHTS: Dict[str, int] = {
    "config":      10,
    "test_write":  10,
    "test_review": 10,
    "explain":     10,
}

# ── 知识层 → document_category 映射 ───────────────────────────────
# 旧的四层（CLI/RULES/DESIGN/TEST）到新分类的映射。
# 保留是为了兼容过渡期；新代码应直接使用 MODE_CATEGORY_WHITELIST。
LAYER_TO_CATEGORIES: Dict[str, List[str]] = {
    "cli":    ["cli/reference", "app/reference"],
    "rules":  ["review/rules", "test/test_template"],
    "design": ["spec/prd", "spec/func_spec", "spec/design", "architecture/design"],
    "test":   ["test/test_list", "test/test_strategy", "review/bug_fix"],
}
