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
CLI 参考检索模块

从 cli.json / cli.pdf 数据中提供结构化 CLI 命令检索能力。
不依赖向量检索，直接通过命令前缀、关键词和元数据进行精确匹配。

用途：
- 配置生成时检索精确 CLI 语法
- 产品知识查询中检索命令帮助
- 测试用例编写时获取命令参数范围和默认值
"""
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认 CLI 参考数据路径
_DEFAULT_CLI_PATH = (
    Path(__file__).parent.parent / "knowledge_base" / "reference" / "cli.json"
)


class CLIReferenceRetriever:
    """
    CLI 参考检索器

    加载 cli.json 中的命令条目，支持：
    - 按 command_prefix 精确查找（如 "slb virtual"）
    - 按关键词模糊搜索（如 "health check"）
    - 按 product_module 过滤（如 "slb"）
    - 按 config_mode 过滤（如 "global", "interface"）
    """

    def __init__(self, cli_path: Optional[Path] = None):
        self._path = cli_path or _DEFAULT_CLI_PATH
        self._entries: List[Dict[str, Any]] = []
        self._prefix_index: Dict[str, List[int]] = {}
        self._module_index: Dict[str, List[int]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self._path.exists():
            logger.warning("CLI 参考文件不存在: %s", self._path)
            self._loaded = True
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                logger.warning("CLI 参考数据格式异常 (非 list)")
                self._loaded = True
                return
            for idx, item in enumerate(raw):
                if not isinstance(item, dict):
                    continue
                text = item.get("page_content", "")
                meta = item.get("metadata", {})
                if not text:
                    continue
                entry = {
                    "text": text,
                    "command_prefix": meta.get("command_prefix", ""),
                    "product_module": meta.get("product_module", ""),
                    "config_mode": meta.get("config_mode", ""),
                    "step_type": meta.get("step_type", ""),
                    "section_title": meta.get("section_title", ""),
                    "source_file": meta.get("source_file", ""),
                }
                self._entries.append(entry)
                # 索引: command_prefix
                prefix = (meta.get("command_prefix") or "").lower().strip()
                if prefix:
                    self._prefix_index.setdefault(prefix, []).append(len(self._entries) - 1)
                # 索引: product_module
                module = (meta.get("product_module") or "").lower().strip()
                if module:
                    self._module_index.setdefault(module, []).append(len(self._entries) - 1)
            logger.info(
                "CLI 参考加载完成: %d 条目, %d 命令前缀, %d 产品模块",
                len(self._entries),
                len(self._prefix_index),
                len(self._module_index),
            )
        except Exception as e:
            logger.error("加载 CLI 参考失败: %s", e)
        self._loaded = True

    @property
    def entry_count(self) -> int:
        self._ensure_loaded()
        return len(self._entries)

    def search_by_prefix(
        self,
        prefix: str,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """按命令前缀精确搜索。"""
        self._ensure_loaded()
        prefix_lower = prefix.lower().strip()
        results = []
        indices = self._prefix_index.get(prefix_lower, [])
        for idx in indices[:max_results]:
            results.append(self._entries[idx])
        return results

    def search_by_keyword(
        self,
        keywords: str,
        product_module: str = "",
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """按关键词模糊搜索 CLI 命令（支持产品模块过滤）。"""
        self._ensure_loaded()
        kw_lower = keywords.lower().strip()
        kw_parts = kw_lower.split()

        candidates = range(len(self._entries))
        if product_module:
            mod = product_module.lower().strip()
            candidates = self._module_index.get(mod, [])

        scored: List[tuple] = []
        for idx in candidates:
            entry = self._entries[idx]
            text_lower = entry["text"].lower()
            prefix_lower = entry["command_prefix"].lower()
            # 关键词全部匹配得分更高
            match_count = sum(1 for kw in kw_parts if kw in text_lower or kw in prefix_lower)
            if match_count == 0:
                continue
            score = match_count / max(len(kw_parts), 1)
            # 命令前缀直接匹配加分
            if kw_lower in prefix_lower or prefix_lower in kw_lower:
                score += 0.5
            scored.append((score, idx))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._entries[idx] for _, idx in scored[:max_results]]

    def search(
        self,
        query: str,
        product_module: str = "",
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        智能搜索：先尝试前缀匹配，再退回关键词搜索。

        Args:
            query: 搜索查询（命令名/关键词）
            product_module: 可选的产品模块过滤
            max_results: 最大返回数

        Returns:
            匹配的 CLI 条目列表
        """
        self._ensure_loaded()

        # 尝试从 query 中提取命令前缀
        prefix_candidates = self._extract_command_prefixes(query)
        results = []
        seen = set()
        for prefix in prefix_candidates:
            for entry in self.search_by_prefix(prefix, max_results=max_results):
                key = entry["text"][:100]
                if key not in seen:
                    seen.add(key)
                    results.append(entry)

        if len(results) >= max_results:
            return results[:max_results]

        # 补充关键词搜索
        for entry in self.search_by_keyword(query, product_module, max_results=max_results):
            key = entry["text"][:100]
            if key not in seen:
                seen.add(key)
                results.append(entry)

        return results[:max_results]

    def format_results(self, results: List[Dict[str, Any]], max_chars: int = 3000) -> str:
        """将搜索结果格式化为上下文文本。"""
        parts = []
        total = 0
        for entry in results:
            text = entry["text"]
            prefix = entry.get("command_prefix", "")
            header = f"[CLI: {prefix}]" if prefix else "[CLI]"
            chunk = f"{header}\n{text}"
            if total + len(chunk) > max_chars:
                remaining = max_chars - total
                if remaining > 100:
                    parts.append(chunk[:remaining] + "...")
                break
            parts.append(chunk)
            total += len(chunk)
        return "\n\n".join(parts)

    def _extract_command_prefixes(self, query: str) -> List[str]:
        """从查询中尝试提取可能的命令前缀。"""
        q = query.lower().strip()
        prefixes = []

        # 常见的命令前缀模式
        cli_patterns = [
            r'\b(slb\s+virtual\s+\w+)',
            r'\b(slb\s+real\s+\w+)',
            r'\b(slb\s+health\s*)',
            r'\b(slb\s+group\s+\w+)',
            r'\b(slb\s+policy\s+\w+)',
            r'\b(slb\s+\w+)',
            r'\b(llb\s+\w+)',
            r'\b(health\s+\w+)',
            r'\b(http2\s+\w+)',
            r'\b(show\s+slb\s+\w+)',
            r'\b(show\s+\w+)',
            r'\b(ip\s+\w+)',
            r'\b(no\s+\w+)',
        ]
        for pat in cli_patterns:
            match = re.search(pat, q)
            if match:
                prefixes.append(match.group(1).strip())

        # 如果没找到模式，取前两个词作为前缀尝试
        if not prefixes:
            words = q.split()[:3]
            if len(words) >= 2:
                prefixes.append(" ".join(words[:2]))
            if len(words) >= 1:
                prefixes.append(words[0])

        return prefixes


# 全局单例
_cli_retriever: Optional[CLIReferenceRetriever] = None


def get_cli_retriever(cli_path: Optional[Path] = None) -> CLIReferenceRetriever:
    """获取 CLI 检索器单例。"""
    global _cli_retriever
    if _cli_retriever is None:
        _cli_retriever = CLIReferenceRetriever(cli_path)
    return _cli_retriever
