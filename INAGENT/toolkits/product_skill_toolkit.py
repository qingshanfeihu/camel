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
ProductSkillToolkit — 赋予 Agent 理解产品架构、推导覆盖面的技能。

底层调用 CLIGraphStore + KnowledgeToolkit + GraphRAG，将产品架构知识
暴露为 4 个工具函数供 Worker Agent 在评审过程中主动调用。

工具列表:
- query_module_tech_profile: 查询模块技术画像
- discover_cross_cutting_concerns: 推导横切面测试维度
- check_spec_constraints: 查询产品规格约束
- query_module_relationships: 查询模块实体关系
"""
from __future__ import annotations

import logging
from typing import List, Optional

from camel.toolkits.base import BaseToolkit
from camel.toolkits.function_tool import FunctionTool

logger = logging.getLogger(__name__)


class ProductSkillToolkit(BaseToolkit):
    r"""Toolkit that gives review agents product-architecture reasoning skills.

    Each tool queries the enriched CLI keyword graph, product knowledge base,
    and (optionally) GraphRAG index to return structured product context.

    Args:
        cli_graph_store: A pre-initialised ``CLIGraphStore`` instance.
            When *None*, the singleton is obtained lazily.
        knowledge_toolkit: A pre-initialised ``KnowledgeToolkit`` instance
            (mode ``"test_review"``).  When *None*, one is created lazily.
        timeout: Timeout threshold inherited from BaseToolkit.
    """

    def __init__(
        self,
        cli_graph_store=None,
        knowledge_toolkit=None,
        timeout: Optional[float] = None,
    ):
        super().__init__(timeout=timeout)
        self._cli_graph = cli_graph_store
        self._knowledge_tk = knowledge_toolkit

    # ── lazy singletons ─────────────────────────────────────────────

    def _get_cli_graph(self):
        if self._cli_graph is None:
            from INAGENT.rag.cli_graph_store import get_cli_graph_store
            self._cli_graph = get_cli_graph_store()
        return self._cli_graph

    def _get_knowledge_tk(self):
        if self._knowledge_tk is None:
            from INAGENT.toolkits.knowledge_toolkit import KnowledgeToolkit
            self._knowledge_tk = KnowledgeToolkit(mode="test_review")
        return self._knowledge_tk

    # ── get_tools (required by BaseToolkit) ──────────────────────

    def get_tools(self) -> List[FunctionTool]:
        r"""Return the list of product skill tools."""
        return [
            FunctionTool(self.query_module_tech_profile),
            FunctionTool(self.discover_cross_cutting_concerns),
            FunctionTool(self.check_spec_constraints),
            FunctionTool(self.query_module_relationships),
        ]

    # ── Tool 1: Module Tech Profile ─────────────────────────────────

    def query_module_tech_profile(self, module_name: str) -> str:
        r"""Query the technical profile of a product module.

        Returns protocol stack, interface types, address families, OSI layer,
        related modules, and feature tags for the specified module.

        Use this tool before reviewing a module's test cases to understand
        what technical dimensions (protocols, interfaces, address families)
        should be covered by the test suite.

        Args:
            module_name: Module name (case-insensitive), e.g. ``"SLB"``,
                ``"HTTP"``, ``"SSL"``.

        Returns:
            A structured text block with the module's technical profile,
            or a message if the module is not found.
        """
        store = self._get_cli_graph()
        store._ensure_loaded()
        # Find the module node by label (case-insensitive)
        module_node = None
        for node in store._module_nodes.values():
            if node.get("label", "").upper() == module_name.upper():
                module_node = node
                break
            if node.get("id", "").lower() == module_name.lower():
                module_node = node
                break

        if not module_node:
            return f"(模块 '{module_name}' 未在CLI功能树中找到)"

        label = module_node.get("label", module_name)
        parts = [f"# {label} 模块技术画像"]

        proto = module_node.get("protocol_stack", [])
        if proto:
            parts.append(f"协议栈: {', '.join(proto)}")
        else:
            parts.append("协议栈: (未识别)")

        af = module_node.get("address_family", [])
        parts.append(f"地址族: {', '.join(af) if af else '(未识别)'}")

        layer = module_node.get("layer", "")
        parts.append(f"OSI层级: {layer if layer else '(未识别)'}")

        iface = module_node.get("interface_types", [])
        parts.append(f"管理接口: {', '.join(iface) if iface else 'CLI'}")

        related = module_node.get("related_modules", [])
        if related:
            parts.append(f"关联模块 ({len(related)}个): {', '.join(related)}")

        tags = module_node.get("feature_tags", [])
        if tags:
            parts.append(f"功能标签: {', '.join(tags)}")

        cmd_count = module_node.get("commands_count", 0)
        parts.append(f"CLI命令数: {cmd_count}")

        return "\n".join(parts)

    # ── Tool 2: Cross-Cutting Concerns ──────────────────────────────

    def discover_cross_cutting_concerns(
        self, feature_description: str
    ) -> str:
        r"""Discover cross-cutting test dimensions for a feature.

        Given a feature description, this tool traces the feature's module
        relationships in the CLI keyword graph to identify testing dimensions
        that might be overlooked (e.g., protocol version coverage, address
        family support, WebUI integration).

        Use this tool to determine what horizontal test dimensions should
        be checked beyond the primary feature functionality.

        Args:
            feature_description: A natural-language description of the feature
                being reviewed, e.g. ``"SLB cookie加密 ircookie"``.

        Returns:
            A structured analysis of cross-cutting test concerns with
            evidence from the product knowledge graph.
        """
        store = self._get_cli_graph()
        store._ensure_loaded()

        # Extract subgraph for the feature
        try:
            subgraph = store.extract_subgraph(
                [feature_description], max_nodes=40, max_depth=2
            )
        except Exception as e:
            logger.warning("extract_subgraph failed: %s", e)
            subgraph = {"nodes": [], "edges": [], "modules": []}

        # Collect tech profiles from matched modules.
        # Note: extract_subgraph returns module IDs in "modules" list,
        # but module-type nodes may not appear in "nodes" (which contains
        # command/operation_command nodes from BFS). Look up from store.
        modules_in_scope = []
        for mid in subgraph.get("modules", []):
            mnode = store._module_nodes.get(mid)
            if mnode:
                modules_in_scope.append(mnode)

        if not modules_in_scope:
            return f"(未从 '{feature_description}' 定位到模块，无法推导横切面)"

        # Aggregate cross-cutting dimensions
        all_protocols: set = set()
        all_af: set = set()
        all_interfaces: set = set()
        all_tags: set = set()
        all_related: set = set()
        module_names = []

        for mod in modules_in_scope:
            module_names.append(mod.get("label", mod.get("id", "")))
            for p in mod.get("protocol_stack", []):
                all_protocols.add(p)
            for a in mod.get("address_family", []):
                all_af.add(a)
            for i in mod.get("interface_types", []):
                all_interfaces.add(i)
            for t in mod.get("feature_tags", []):
                all_tags.add(t)
            for r in mod.get("related_modules", []):
                all_related.add(r)

        parts = [
            f"# 横切面分析: {feature_description}",
            f"涉及模块: {', '.join(module_names)}",
            "",
            "## 应检查的横切面维度:",
        ]

        if all_protocols:
            parts.append(
                f"1. **协议覆盖**: 涉及协议 {', '.join(sorted(all_protocols))}。"
                f"测试是否覆盖了各协议版本（如HTTP 1.0/1.1/2.0）？"
            )
        if all_af:
            parts.append(
                f"2. **地址族覆盖**: 涉及 {', '.join(sorted(all_af))}。"
                f"测试是否同时覆盖了IPv4和IPv6场景？"
            )
        else:
            parts.append(
                "2. **地址族覆盖**: 未检测到明确的地址族关键词。"
                "请确认该功能是否涉及网络层，若涉及则需考虑IPv4/IPv6覆盖。"
            )
        if "WebUI" in all_interfaces:
            parts.append(
                "3. **WebUI接口覆盖**: 该功能涉及WebUI管理接口。"
                "测试是否覆盖了WebUI配置场景（语言、主题、浏览器兼容性）？"
            )
        if len(all_related) > 3:
            parts.append(
                f"4. **模块交互覆盖**: 关联模块多达{len(all_related)}个"
                f" ({', '.join(sorted(all_related)[:8])}...)。"
                f"测试是否覆盖了关键模块间的交互场景？"
            )
        if "persistence" in all_tags or "session-management" in all_tags:
            parts.append(
                "5. **会话持久性**: 功能涉及会话/持久化特性。"
                "测试是否覆盖了会话恢复、配置可恢复性场景？"
            )
        if "high-availability" in all_tags:
            parts.append(
                "6. **高可用性**: 功能涉及HA特性。"
                "测试是否覆盖了主备切换、故障恢复等HA场景？"
            )

        parts.append("")
        parts.append(
            "## 推导依据: 基于CLI功能树中模块的"
            "protocol_stack/address_family/interface_types/feature_tags属性"
        )

        return "\n".join(parts)

    # ── Tool 3: Spec Constraints ────────────────────────────────────

    def check_spec_constraints(
        self, module_name: str, question: str
    ) -> str:
        r"""Check product specification constraints for a module.

        Queries the product knowledge base for specification constraints,
        limitations, or requirements related to the given module and question.

        Use this tool to verify whether a test case's expected behavior
        matches the product specification, especially for modules that
        were deprioritized or skipped during review triage.

        Args:
            module_name: The module name to query about,
                e.g. ``"SEGMENT"`` or ``"SLB"``.
            question: The specific question about expected behavior,
                e.g. ``"segment是否支持global配置"``.

        Returns:
            Relevant specification excerpts and an assessment of whether
            the expected behavior is consistent with the spec.
        """
        tk = self._get_knowledge_tk()
        query = f"{module_name} {question} 规格 约束 限制 支持"
        try:
            result = tk.search_product_knowledge(
                query=query,
                category_filter="spec/prd,spec/func_spec,spec/design",
                max_results=5,
                use_graphrag=True,
            )
            if result and not result.startswith("("):
                return (
                    f"# {module_name} 规格约束查询\n"
                    f"问题: {question}\n\n"
                    f"## 知识库检索结果:\n{result}"
                )
            return (
                f"# {module_name} 规格约束查询\n"
                f"问题: {question}\n\n"
                f"(未找到相关规格约束，建议人工确认)"
            )
        except Exception as e:
            logger.warning("check_spec_constraints failed: %s", e)
            return f"(规格约束查询失败: {e})"

    # ── Tool 4: Module Relationships ────────────────────────────────

    def query_module_relationships(self, module_name: str) -> str:
        r"""Query a module's relationships in the product knowledge graph.

        Returns the module's position in the product architecture:
        parent/child modules, keyword-linked neighbors, and the CLI
        command tree structure.

        Use this tool to understand how a module relates to other parts
        of the product, helping identify integration test scenarios.

        Args:
            module_name: The module name to query, e.g. ``"SLB"``.

        Returns:
            A formatted text block showing the module's relationships,
            command tree, and cross-module links.
        """
        store = self._get_cli_graph()
        try:
            subgraph = store.extract_subgraph(
                [module_name], max_nodes=60, max_depth=2
            )
            text = store.format_for_prompt(subgraph, max_chars=2500)
            if text:
                return f"# {module_name} 模块关系图\n\n{text}"
            return f"(模块 '{module_name}' 在CLI功能树中无数据)"
        except Exception as e:
            logger.warning("query_module_relationships failed: %s", e)
            return f"(模块关系查询失败: {e})"
