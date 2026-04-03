"""ReviewMemory — 评审经验库 (Phase 1A + v9 向量语义检索).

持久化存储人工反馈和领域知识修正，每次评审自动注入相关经验到 Agent prompt，
避免重复犯同样的领域错误。

v9 升级: 支持向量语义检索 (VectorDBBlock)，tag-match + 语义相似度混合查询。

存储格式: JSON list，每条 entry:
  {
    "id": "rm_001",
    "pattern": "plainname 就是明文的同义词，clear 恢复默认就是恢复为 plainname",
    "source": "human_feedback_bug121100",
    "tags": ["ircookie", "plainname", "clear"],
    "created": "2026-03-29"
  }
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MEMORY_DIR = Path(__file__).resolve().parent.parent / "config" / "review_memory"

# ── 内置经验条目（来自已确认的人工反馈）──────────────────────
_BUILTIN_ENTRIES: List[Dict[str, Any]] = [
    {
        "id": "rm_builtin_001",
        "pattern": "plainname 是 ircookie_mode 的明文模式，也是默认模式。"
                   "clear slb mode ircookie 恢复默认 = 恢复为 plainname，两者语义等价。"
                   "不要认为 plainname 不是默认值。",
        "tags": ["ircookie", "plainname", "clear", "default"],
        "source": "human_feedback_bug121100",
    },
    {
        "id": "rm_builtin_002",
        "pattern": "global 和 default 不是合法的 group_name，而是保留的配置层级。"
                   "在没有预先配置 group 的情况下，对 global/default 执行 no slb mode ircookie 操作应当失败。"
                   "不要假设所有字符串都是合法的 group_name。",
        "tags": ["ircookie", "global", "default", "group_name", "no"],
        "source": "human_feedback_bug121100",
    },
    {
        "id": "rm_builtin_003",
        "pattern": "在声称'缺少某类验证用例'之前，必须遍历所有模块的全部用例（包括 WebUI 验证模块），"
                   "不能只看 CLI 模块就下结论。Expected Result 中描述 AES 加密结果验证的用例可能在 WebUI 验证模块中。",
        "tags": ["coverage", "verification", "aes", "webui"],
        "source": "human_feedback_bug121100",
    },
    {
        "id": "rm_builtin_004",
        "pattern": "全局配置 APV cookie 加密是已有功能（已支持），本次变更主要是支持基于 group 配置 + AES 加密密码。"
                   "评审重心：全局配置 → 保证基本功能正常即可；group + AES → 需深入覆盖边界/负向/并发场景。",
        "tags": ["ircookie", "global", "group", "change_scope", "priority"],
        "source": "domain_knowledge",
    },
    {
        "id": "rm_builtin_005",
        "pattern": "Segment 功能只支持基于 group 配置，不支持全局配置。"
                   "如果 segment webui 的测试用例以全局配置为前提，预期结果应为'配置失败'而非'配置成功'。",
        "tags": ["segment", "group", "global", "webui"],
        "source": "spec_constraint",
    },
    {
        "id": "rm_builtin_006",
        "pattern": "新功能（基于 group 的 ircookie）需要验证与 HTTP 1.0/1.1/2.0 各版本的兼容性，"
                   "以及 IPv4/IPv6/双栈环境下的正确性。这些是横切面测试维度，应挑选基本功能 case 验证。",
        "tags": ["http_version", "ipv6", "cross_cutting", "compatibility"],
        "source": "domain_knowledge",
    },
    {
        "id": "rm_builtin_007",
        "pattern": "配置多条基于 group 的 slb mode ircookie 以及基于全局的 slb mode ircookie 时，"
                   "需要验证相互之间功能不受影响（配置并存/隔离验证）。",
        "tags": ["group", "global", "coexistence", "isolation"],
        "source": "domain_knowledge",
    },
    # ── v9 新增通用测试惯例 ────────────────────────────────────
    {
        "id": "rm_builtin_008",
        "pattern": "WebUI 功能测试建议覆盖语言切换（中/英）和主题切换，确保不同 UI 环境下功能行为一致。"
                   "WebUI 的验证用例可能分布在专门的 WebUI 验证模块中，评审前应遍历所有模块。",
        "tags": ["webui", "language", "theme", "ui"],
        "source": "general_testing_practice",
    },
    {
        "id": "rm_builtin_009",
        "pattern": "如果 CLI 命令的某个参数允许传入保留字（如 global、default）作为合法值，"
                   "应增加 case 验证该保留字值的配置行为和预期结果。"
                   "保留字作为参数值时可能触发特殊逻辑或应当被系统拒绝。",
        "tags": ["cli", "reserved_word", "parameter", "boundary"],
        "source": "general_testing_practice",
    },
    {
        "id": "rm_builtin_010",
        "pattern": "CLI configure 类用例的预期结果应明确具体：配置是否可 save/恢复、重启后是否持久化。"
                   "模糊表述如'配置成功'不足以覆盖可恢复性验证。"
                   "建议明确预期结果包含配置后的具体效果和可验证标准。",
        "tags": ["cli", "configure", "expected_result", "save", "restore"],
        "source": "general_testing_practice",
    },
    {
        "id": "rm_builtin_011",
        "pattern": "当某个功能模块同时存在已有功能和新增功能时，已有功能用例应保持基本覆盖即可，"
                   "新增功能才需要深度覆盖边界/负向/并发场景。评审应提出用例重心调整建议。"
                   "这是结构性建议，比逐条增加用例更有价值。",
        "tags": ["scope", "priority", "structural", "new_vs_existing"],
        "source": "general_testing_practice",
    },
    {
        "id": "rm_builtin_012",
        "pattern": "不同横切测试维度（如 HTTP 版本、地址族、SSL/TLS、HA）应作为独立发现输出，"
                   "不要合并在一条发现中，以便评审人逐项跟进。"
                   "每个横切维度的测试策略可能不同，合并会导致可操作性降低。",
        "tags": ["cross_cutting", "http_version", "ipv6", "ha", "ssl"],
        "source": "general_testing_practice",
    },
    {
        "id": "rm_builtin_013",
        "pattern": "当新功能与已有功能存在集成点（如 http turbo、redirect、health check），"
                   "应单独审视集成 case 是否覆盖新功能的影响，并建议优化这些集成 case。"
                   "集成点容易成为回归的薄弱环节。",
        "tags": ["integration", "cross_feature", "regression"],
        "source": "general_testing_practice",
    },
]


class ReviewMemory:
    """评审经验库：加载内置条目 + 外部 JSON 文件 + 向量语义检索。"""

    def __init__(self, memory_dir: Optional[Path] = None):
        self._dir = Path(memory_dir) if memory_dir else _DEFAULT_MEMORY_DIR
        self._entries: List[Dict[str, Any]] = list(_BUILTIN_ENTRIES)
        self._load_external()
        self._vector_block = None
        self._init_vector_block()

    def _init_vector_block(self) -> None:
        """Initialize VectorDBBlock and index all entries for semantic retrieval."""
        from camel.memories.blocks.vectordb_block import VectorDBBlock
        from camel.memories.records import MemoryRecord
        from camel.messages import BaseMessage as _BM
        from camel.types import OpenAIBackendRole

        from INAGENT.utils.llm_config import get_gateway_config
        gw = get_gateway_config()
        base_url = (gw.get("base_url") or "").rstrip("/")
        if not base_url:
            raise RuntimeError("ReviewMemory: gateway base_url 未配置，无法初始化向量索引")
        api_key = gw.get("api_key", "local-gateway")
        embed_model = gw.get("embedding_model", "text-embedding-v4")

        from camel.embeddings import OpenAICompatibleEmbedding
        embedding = OpenAICompatibleEmbedding(
            model_type=embed_model,
            api_key=api_key,
            url=base_url,
        )

        self._vector_block = VectorDBBlock(embedding=embedding)
        for entry in self._entries:
            pattern = entry.get("pattern", "")
            if not pattern:
                continue
            record = MemoryRecord(
                message=_BM.make_user_message(
                    role_name="experience",
                    content=f"[{entry.get('id', '')}] {pattern}",
                ),
                role_at_backend=OpenAIBackendRole.USER,
            )
            self._vector_block.write_records([record])
        logger.info("ReviewMemory: vector index built with %d entries", len(self._entries))

    def _load_external(self) -> None:
        if not self._dir.is_dir():
            return
        for fp in sorted(self._dir.glob("*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._entries.extend(data)
                elif isinstance(data, dict):
                    self._entries.append(data)
            except Exception as e:
                logger.warning("ReviewMemory: 加载 %s 失败: %s", fp, e)

    @property
    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def query(
        self,
        tags: Optional[List[str]] = None,
        question: Optional[str] = None,
        max_results: int = 20,
    ) -> List[Dict[str, Any]]:
        """按 tag 交集 + 向量语义混合查询相关经验条目。

        Args:
            tags: Tag-based matching (original behavior).
            question: Natural language query for semantic matching (v9).
            max_results: Max entries to return.
        """
        # Phase 1: Tag-based matching
        tag_results: List[Dict[str, Any]] = []
        if tags:
            tag_set = {t.lower() for t in tags}
            scored: List[tuple] = []
            for entry in self._entries:
                entry_tags = {t.lower() for t in (entry.get("tags") or [])}
                overlap = len(tag_set & entry_tags)
                if overlap > 0:
                    scored.append((overlap, entry))
            scored.sort(key=lambda x: -x[0])
            tag_results = [e for _, e in scored[:max_results]]

        # Phase 2: Semantic vector retrieval (if available and question provided)
        vector_results: List[Dict[str, Any]] = []
        if self._vector_block and question:
            retrieved = self._vector_block.retrieve(
                keyword=question,
                limit=max_results,
            )
            for rec in retrieved:
                content = rec[0].message.content if hasattr(rec[0], "message") else str(rec[0])
                for entry in self._entries:
                    if entry.get("id", "") in content:
                        if entry not in vector_results:
                            vector_results.append(entry)
                        break

        # Merge: tag results first, then unique vector results
        if not tags and not question:
            return self._entries[:max_results]

        seen_ids = {e.get("id") for e in tag_results}
        merged = list(tag_results)
        for entry in vector_results:
            if entry.get("id") not in seen_ids:
                merged.append(entry)
                seen_ids.add(entry.get("id"))
        return merged[:max_results]

    def format_for_prompt(
        self,
        tags: Optional[List[str]] = None,
        max_chars: int = 3000,
    ) -> str:
        """格式化为可直接注入 prompt 的文本。"""
        entries = self.query(tags=tags) if tags else self._entries
        if not entries:
            return ""
        lines = ["<review_experience_memory>", "【评审经验库 — 来自历史评审反馈的强约束】"]
        total = 0
        for i, e in enumerate(entries, 1):
            pattern = e.get("pattern", "")
            source = e.get("source", "")
            line = f"{i}. [{source}] {pattern}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        lines.append(
            "\n请严格遵守以上经验教训，避免重复犯相同错误。\n"
            "</review_experience_memory>"
        )
        return "\n".join(lines)
