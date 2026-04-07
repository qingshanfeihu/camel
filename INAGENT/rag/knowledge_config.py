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
知识分类配置 — 基于 command tree 的树层级检索策略

所有模式对 GraphRAG 的检索范围由此处的树层级策略统一管控。
workflow（pipeline、interactive_cli、web 等调用方）不需要知道底层用的是
GraphRAG / 向量 / BM25，只需声明 mode，路由器会自动约束检索范围。

树层级体系（由 knowledge_linker 在入库时标注）：
  leaf     — 直接匹配到 command tree 叶子节点的知识块（CLI 命令等）
  new_leaf — LLM 判定需要在树上新增的叶子节点
  branch   — 匹配到模块/功能层级分支的知识块（应用配置等）
  trunk    — 匹配到产品规格/设计层的知识块
  root     — 架构/顶层设计知识块
"""
from __future__ import annotations

from typing import Dict, List

# ── 合法树层级 ────────────────────────────────────────────────────
TREE_LEVELS = ["leaf", "new_leaf", "branch", "trunk", "root"]

# ── 模式 → 允许的树层级策略 ────────────────────────────────────────
# 每个模式只会检索对应层级的文档。
# 检索时由 KnowledgeRouter 将层级列表传递给 UnifiedRAGRetriever，
# 后者将其作为 tree_position.tree_level 过滤条件。
MODE_TREE_STRATEGY: Dict[str, List[str]] = {
    "config": [
        "leaf",
        "new_leaf",
        "branch",
    ],
    "test_write": [
        "leaf",
        "new_leaf",
        "branch",
        "trunk",
    ],
    "test_review": [
        "leaf",
        "new_leaf",
        "branch",
        "trunk",
        "root",
    ],
    "explain": [
        "branch",
        "trunk",
        "root",
    ],
}

# ── 模式 → 检索上下文字符预算权重 ─────────────────────────────────
MODE_BUDGET_WEIGHTS: Dict[str, int] = {
    "config":      10,
    "test_write":  10,
    "test_review": 10,
    "explain":     10,
}

# ── 旧分类 → 树层级映射（向后兼容迁移） ───────────────────────────
# 用于运行时将旧 document_category 元数据映射到 tree_level。
# 新文档不再使用 document_category，此映射仅服务于未重新入库的历史数据。
CATEGORY_TO_TREE_LEVEL: Dict[str, str] = {
    "cli/reference": "leaf",
    "app/reference": "branch",
    "architecture/design": "root",
    "spec/prd": "trunk",
    "spec/func_spec": "trunk",
    "spec/design": "trunk",
    "test/test_list": "branch",
    "test/test_strategy": "trunk",
    "test/test_template": "branch",
    "review/rules": "trunk",
    "review/bug_fix": "leaf",
}

# ── 向后兼容别名 ──────────────────────────────────────────────────
# 旧代码可能引用这些名称；新代码应使用 MODE_TREE_STRATEGY。
DOCUMENT_CATEGORIES = list(CATEGORY_TO_TREE_LEVEL.keys())
MODE_CATEGORY_WHITELIST = MODE_TREE_STRATEGY
LAYER_TO_CATEGORIES: Dict[str, List[str]] = {
    "cli":    ["cli/reference", "app/reference"],
    "rules":  ["review/rules", "test/test_template"],
    "design": ["spec/prd", "spec/func_spec", "spec/design", "architecture/design"],
    "test":   ["test/test_list", "test/test_strategy", "review/bug_fix"],
}
