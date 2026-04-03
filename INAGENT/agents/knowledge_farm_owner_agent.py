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
农场主 Agent (Knowledge Farm-Owner Agent) — TreeInformed Decision Engine v2

职责：接收 schema_gaps.jsonl（由采购员/农民产出），
执行 GraphRAG 结构性维护 —— 新实体、属性列、冲突与溢出的保守裁决。
写入前 snapshot_backup；结束后对 GraphRAG 检索器 reload()（仅刷新当前进程内图视图）。

决策流程：每个 gap 先查 CLIGraphStore 获取 TreeContext（层级路径、父节点候选、
Skeleton artifact 状态、enrich 快照）；规则可确定时走规则，模糊时交 LLM；
禁止硬编码相似度阈值。needs_tree_session 条目放 FarmOwnerReport.deferred。

不写 knowledge_base.json。混合向量（Qdrant/BM25）默认不刷新；可传
``refresh_hybrid_vectors=True`` 先 merge_knowledge_base 再 refresh_hybrid_vector_index。
仅做图结构维护，不做 chunk 内容富化（农民的职责）。

职责变更时请同步：INAGENT/docs/agents/sessions/04-farm-owner.md、
.cursor/rules/kb-session-farm-owner.mdc 与 kb-session-farm-owner-anchor.mdc。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import BaseModelBackend

from INAGENT.rag.knowledge_schema import (
    FarmOwnerReport,
    FillRequest,
    SchemaGapEntry,
    TreeContext,
    TREE_ENRICH_KEYS,
)
from INAGENT.utils.env_utils import get_product_name, load_inagent_env

logger = logging.getLogger(__name__)

_FARM_OWNER_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_OVERFLOW_DENYLIST_PATH = _FARM_OWNER_CONFIG_DIR / "farm_owner_overflow_denylist.json"


def _load_overflow_field_denylist() -> frozenset:
    if not _OVERFLOW_DENYLIST_PATH.exists():
        return frozenset()
    try:
        raw = json.loads(_OVERFLOW_DENYLIST_PATH.read_text(encoding="utf-8"))
        names = raw.get("deny_field_names", [])
        if not isinstance(names, list):
            return frozenset()
        return frozenset(str(x).strip() for x in names if x is not None and str(x).strip())
    except Exception as exc:
        logger.warning("无法加载 overflow 字段 denylist %s: %s", _OVERFLOW_DENYLIST_PATH, exc)
        return frozenset()


class KnowledgeFarmOwnerAgent:
    def __init__(
        self,
        graphrag_retriever,
        model: Optional[BaseModelBackend] = None,
        product_name: Optional[str] = None,
        _chat_agent=None,
        cli_graph=None,
        skeleton_index=None,
    ) -> None:
        self.graphrag = graphrag_retriever
        self._chat_agent = _chat_agent
        self._overflow_field_denylist = _load_overflow_field_denylist()
        self._cli_graph_override = cli_graph
        self._skeleton_index_override = skeleton_index
        if self._chat_agent is None and model is not None:
            load_inagent_env()
            owner_product_name = product_name or get_product_name()
            self._chat_agent = ChatAgent(
                system_message=BaseMessage.make_assistant_message(
                    role_name="Knowledge Farm Owner",
                    content=(
                        f"你是 {owner_product_name} 知识库的结构维护裁决者。"
                        "你只返回 JSON，对 conflict 和 overflow 做保守、可执行的决策。"
                    ),
                ),
                model=model,
            )

    def process_gaps(self, gaps_file: Path) -> FarmOwnerReport:
        if not gaps_file.exists():
            return FarmOwnerReport(errors=[f"文件不存在: {gaps_file}"])

        entries: List[SchemaGapEntry] = []
        with open(gaps_file, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                gap_type = raw.get("gap_type", "")
                if gap_type not in (
                    "new_entity",
                    "new_entity_attribute",
                    "conflict",
                    "overflow",
                ):
                    continue
                entries.append(self._entry_from_raw(raw))

        if not entries:
            logger.info("process_gaps: 无可处理 schema gap 条目")
            return FarmOwnerReport()

        return self.process_gap_entries(entries)

    def process_gap_entries(
        self,
        entries: List[SchemaGapEntry],
        *,
        refresh_hybrid_vectors: bool = False,
        hybrid_vectors_force: bool = True,
    ) -> FarmOwnerReport:
        """处理 gap 条目。

        refresh_hybrid_vectors: GraphRAG reload 之后是否调用 ``refresh_hybrid_vector_index``，
            使 Qdrant/BM25 与 ``knowledge_base/reference`` 当前内容一致（需已配置 LLM 网关）。
        hybrid_vectors_force: 为 True 时强制清空 Qdrant 再重嵌；仅当确定 merged KB 指纹已变
            且希望省耗时时可改为 False，走指纹判定逻辑。
        """
        report = FarmOwnerReport()

        try:
            report.snapshot_dir = self.graphrag.snapshot_backup(label="farm_owner")
        except Exception as exc:
            report.errors.append(f"snapshot_backup 失败: {exc}")
            return report

        entries = self._partition_valid_gap_entries(entries, report)

        conflict_entries = [entry for entry in entries if entry.gap_type == "conflict"]
        overflow_entries = [entry for entry in entries if entry.gap_type == "overflow"]
        new_entity_entries = [entry for entry in entries if entry.gap_type == "new_entity"]
        attribute_entries = [
            entry for entry in entries if entry.gap_type == "new_entity_attribute"
        ]

        self._process_conflicts(conflict_entries, report)
        self._process_overflows(overflow_entries, report)
        self._process_new_entities(new_entity_entries, report)
        self._process_attribute_gaps(attribute_entries, report)

        try:
            self.graphrag.reload()
        except Exception as exc:
            report.errors.append(f"reload 失败: {exc}")

        if refresh_hybrid_vectors:
            try:
                from pathlib import Path
                from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base

                _ref_dir = Path(__file__).resolve().parent.parent / "knowledge_base" / "reference"
                merge_knowledge_base(_ref_dir, _ref_dir / "knowledge_base.json")
            except Exception as exc:
                report.errors.append(f"merge_knowledge_base 失败: {exc}")

            try:
                from INAGENT.workflow_config_generator import refresh_hybrid_vector_index

                refresh_hybrid_vector_index(force=hybrid_vectors_force)
            except Exception as exc:
                report.errors.append(f"混合向量索引刷新失败: {exc}")

        logger.info(
            "FarmOwner 完成: +%d entities, +%s columns, %d embedded, %d conflicts, %d overflows, %d errors",
            report.entities_added,
            report.columns_added,
            report.entities_reembedded,
            report.conflicts_resolved,
            report.overflows_handled,
            len(report.errors),
        )
        return report

    def _partition_valid_gap_entries(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> List[SchemaGapEntry]:
        out: List[SchemaGapEntry] = []
        for entry in entries:
            msg = self._gap_entry_validation_error(entry)
            if msg:
                report.errors.append(msg)
                continue
            out.append(entry)
        return out

    @staticmethod
    def _gap_entry_validation_error(entry: SchemaGapEntry) -> Optional[str]:
        gt = entry.gap_type
        if gt == "new_entity":
            if not (entry.entity_title or "").strip():
                return "new_entity 缺少 entity_title，已跳过"
        elif gt == "new_entity_attribute":
            if not (entry.entity_title or "").strip():
                return "new_entity_attribute 缺少 entity_title，已跳过"
            if not (entry.column_name or "").strip():
                return "new_entity_attribute 缺少 column_name，已跳过"
        elif gt == "conflict":
            if not (entry.entity_title or "").strip() or not (entry.field_name or "").strip():
                return "conflict 缺少 entity_title 或 field_name，已跳过"
        return None

    def _entry_from_raw(self, raw: Dict[str, Any]) -> SchemaGapEntry:
        nearest_matches = raw.get("nearest_matches", [])
        if not isinstance(nearest_matches, list):
            nearest_matches = []
        return SchemaGapEntry(
            gap_type=raw.get("gap_type", "new_entity_attribute"),
            entity_title=raw.get("entity_title", ""),
            entity_description=raw.get("entity_description", ""),
            entity_type=raw.get("entity_type", ""),
            field_name=raw.get("field_name", ""),
            skeleton_value=raw.get("skeleton_value"),
            new_value=raw.get("new_value"),
            column_name=raw.get("column_name", ""),
            column_dtype=raw.get("column_dtype", "str"),
            default_value=raw.get("default_value"),
            evidence=raw.get("evidence", ""),
            source_file=raw.get("source_file", ""),
            timestamp=raw.get("timestamp", ""),
            nearest_matches=nearest_matches,
            chunk_content=raw.get("chunk_content", ""),
        )

    def _process_new_entities(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> None:
        if not entries:
            return

        for entry in entries:
            ctx = self._query_tree_context(entry.entity_title)
            action, enrich_fields, reason = self._decide_new_entity_action(entry, ctx, report)

            if action == "discard":
                logger.debug("new_entity discard: %s (%s)", entry.entity_title, reason)
                continue

            if action == "needs_tree_session":
                report.deferred.append(FillRequest(
                    entity_title=entry.entity_title,
                    source_evidence=entry.evidence,
                    action="needs_tree_session",
                    target_node_id=entry.entity_title,
                    tree_level=ctx.tree_level,
                    enrich_fields=enrich_fields,
                ))
                continue

            # merge_into_existing / tree_create_leaf / tree_create_branch
            try:
                added = self.graphrag.upsert_entities([{
                    "title": entry.entity_title,
                    "description": entry.entity_description,
                    "entity_type": entry.entity_type or "CONFIGURATION",
                    "source_id": entry.source_file,
                }])
                report.entities_added += added
            except Exception as exc:
                report.errors.append(f"upsert_entities 失败: {exc}")
                continue

            if report.entities_added > 0 or action == "merge_into_existing":
                try:
                    report.entities_reembedded += self.graphrag.reembed_entities(
                        [entry.entity_title]
                    )
                except Exception as exc:
                    report.errors.append(f"reembed_entities 失败: {exc}")

            # add_relationships if merging into existing parent
            if action in ("merge_into_existing",) and ctx.parent_candidate_id:
                try:
                    self.graphrag.add_relationships([{
                        "source": entry.entity_title,
                        "target": ctx.parent_candidate_id,
                        "type": "BELONGS_TO",
                        "description": entry.evidence,
                        "source_id": entry.source_file,
                    }])
                except Exception as exc:
                    logger.debug("add_relationships 失败(非致命): %s", exc)

            # skeleton_register
            if action in ("merge_into_existing", "tree_create_leaf", "tree_create_branch"):
                si = self._get_skeleton_index()
                if si is not None and ctx.skeleton_module_id:
                    try:
                        si.register_artifact(
                            artifact_id=entry.entity_title,
                            artifact_type="graphrag_entity",
                            module_id=ctx.skeleton_module_id,
                            document_category=entry.entity_type or "CONFIGURATION",
                            source_file=entry.source_file,
                            graphrag_entity_id=entry.entity_title,
                            title=entry.entity_title,
                        )
                    except Exception as exc:
                        logger.debug("skeleton register 失败(非致命): %s", exc)

            report.fill_requests.append(FillRequest(
                entity_title=entry.entity_title,
                fill_fields=enrich_fields,
                source_evidence=entry.evidence,
                action=action,
                resolved_value=enrich_fields,
                target_node_id=ctx.matched_node_id or entry.entity_title,
                tree_level=ctx.tree_level,
                enrich_fields=enrich_fields,
                artifact_id=entry.entity_title,
            ))

    def _decide_new_entity_action(
        self,
        entry: SchemaGapEntry,
        ctx: TreeContext,
        report: FarmOwnerReport,
    ):
        """规则先行，规则无法判断交 LLM。返回 (action, enrich_fields, reason)。"""
        # 规则 1: exists_in_tree + artifact 已有 → merge_into_existing
        if ctx.exists_in_tree and ctx.skeleton_artifact_exists:
            return "merge_into_existing", {}, "树上已有且 artifact 已注册"

        # 规则 2: exists_in_tree + 无 artifact → merge + skeleton_register
        if ctx.exists_in_tree and not ctx.skeleton_artifact_exists:
            return "merge_into_existing", {}, "树上已有但缺 artifact 注册"

        # 规则 3: 不在树上 + 有父节点候选 → 按类型决定 leaf/branch
        if not ctx.exists_in_tree and ctx.parent_candidate_id:
            entity_type = (entry.entity_type or "").upper()
            action = "tree_create_leaf" if "COMMAND" in entity_type else "tree_create_branch"
            return action, {}, f"父节点候选 {ctx.parent_candidate_id}"

        # 规则无法判断 → LLM
        return self._llm_decide_new_entity(entry, ctx, report)

    def _llm_decide_new_entity(
        self,
        entry: SchemaGapEntry,
        ctx: TreeContext,
        report: FarmOwnerReport,
    ):
        has_info = bool(entry.entity_title or entry.entity_description)
        default_action = "tree_create_branch" if has_info else "discard"
        prompt = (
            "你是知识图谱维护裁决者，只返回 JSON。\n"
            "输出格式: {\"action\": \"tree_create_leaf|tree_create_branch|merge_into_existing|needs_tree_session|discard\", "
            "\"enrich_fields\": {}, \"reason\": \"...\"}\n\n"
            f"实体标题: {entry.entity_title}\n"
            f"实体描述: {entry.entity_description}\n"
            f"实体类型: {entry.entity_type}\n"
            f"证据: {entry.evidence}\n"
            f"树上是否存在: {ctx.exists_in_tree}\n"
            f"相似命令: {json.dumps(ctx.similar_commands, ensure_ascii=False)}\n"
            f"层级路径: {ctx.hierarchy_prefix}\n"
            f"父节点候选: {ctx.parent_candidate_id}\n"
            f"Skeleton 模块: {ctx.skeleton_module_id}\n"
            "请裁决此实体应当：新建叶(tree_create_leaf)、新建枝(tree_create_branch)、"
            "挂入已有节点(merge_into_existing)、等待树会话(needs_tree_session)、还是丢弃(discard)。"
        )
        result = self._run_decision(
            prompt, {"action": default_action, "enrich_fields": {}, "reason": "fallback"}
        )
        action = result.get("action", default_action)
        valid_actions = {
            "tree_create_leaf", "tree_create_branch",
            "merge_into_existing", "needs_tree_session", "discard",
        }
        if action not in valid_actions:
            report.errors.append(f"LLM 返回无效 action '{action}'，改为 {default_action}")
            action = default_action
        enrich_fields = result.get("enrich_fields") or {}
        if not isinstance(enrich_fields, dict):
            enrich_fields = {}
        return action, enrich_fields, result.get("reason", "")

    def _process_attribute_gaps(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> None:
        if not entries:
            return

        column_specs = [
            {
                "name": entry.column_name,
                "dtype": entry.column_dtype,
                "default": entry.default_value,
            }
            for entry in entries
            if entry.column_name
        ]

        if column_specs:
            try:
                cols = self.graphrag.add_entity_columns(column_specs)
                for col in cols:
                    if col not in report.columns_added:
                        report.columns_added.append(col)
            except Exception as exc:
                report.errors.append(f"add_entity_columns 失败: {exc}")

        for entry in entries:
            if not entry.column_name:
                continue
            if entry.default_value is None:
                continue
            report.fill_requests.append(FillRequest(
                entity_title=entry.entity_title,
                fill_fields={entry.column_name: entry.default_value},
                source_evidence=entry.evidence,
                action="update",
                resolved_value={entry.column_name: entry.default_value},
                target_node_id=entry.entity_title,
            ))

        # skeleton_register: 为已添加列的实体注册 artifact
        si = self._get_skeleton_index()
        if si is not None:
            for entry in entries:
                if not entry.entity_title:
                    continue
                ctx = self._query_tree_context(entry.entity_title)
                if not ctx.skeleton_module_id:
                    continue
                try:
                    si.register_artifact(
                        artifact_id=entry.entity_title,
                        artifact_type="graphrag_entity",
                        module_id=ctx.skeleton_module_id,
                        document_category=entry.entity_type or "CONFIGURATION",
                        source_file=entry.source_file,
                        graphrag_entity_id=entry.entity_title,
                        title=entry.entity_title,
                    )
                except Exception as exc:
                    logger.debug("attribute_gaps skeleton register 失败(非致命): %s", exc)

    def _process_conflicts(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> None:
        for entry in entries:
            fill_request = self._resolve_conflict(entry, report)
            if fill_request is None:
                continue
            report.fill_requests.append(fill_request)
            report.conflicts_resolved += 1

    def _process_overflows(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> None:
        for entry in entries:
            if entry.field_name in self._overflow_field_denylist:
                report.fill_requests.append(FillRequest(
                    entity_title=entry.entity_title or "",
                    source_evidence=entry.evidence,
                    action="discard",
                    target_node_id=entry.entity_title or "",
                ))
                report.overflows_handled += 1
                continue
            fill_request = self._resolve_overflow(entry, report)
            if fill_request is None:
                continue
            report.fill_requests.append(fill_request)
            report.overflows_handled += 1

    def _resolve_conflict(
        self,
        entry: SchemaGapEntry,
        report: FarmOwnerReport,
    ) -> Optional[FillRequest]:
        if not entry.entity_title or not entry.field_name:
            report.errors.append("conflict 条目缺少 entity_title 或 field_name")
            return None

        decision = self._decide_conflict(entry)
        decision_name = decision.get("decision", "keep_skeleton")
        final_value = self._resolve_conflict_value(
            entry,
            decision_name,
            decision.get("resolved_value"),
        )

        if decision_name in ("accept_new", "merge"):
            try:
                updated = self.graphrag.update_entity_fields(
                    entry.entity_title,
                    {entry.field_name: final_value},
                )
                if not updated:
                    report.errors.append(
                        f"update_entity_fields 未命中实体: {entry.entity_title}"
                    )
                elif hasattr(self.graphrag, "replace_entity_embeddings"):
                    report.entities_reembedded += self.graphrag.replace_entity_embeddings([
                        entry.entity_title,
                    ])
            except Exception as exc:
                report.errors.append(f"conflict 更新失败: {exc}")

        fill_fields = {entry.field_name: final_value}
        return FillRequest(
            entity_title=entry.entity_title,
            fill_fields=fill_fields,
            source_evidence=entry.evidence,
            action="update",
            resolved_value=fill_fields,
            target_node_id=entry.entity_title,
        )

    def _resolve_overflow(
        self,
        entry: SchemaGapEntry,
        report: FarmOwnerReport,
    ) -> Optional[FillRequest]:
        decision = self._decide_overflow(entry)
        decision_name = decision.get("decision", "discard")

        if decision_name == "merge_into":
            target_node_id = decision.get("target_node_id") or self._default_overflow_target(entry)
            resolved_value = {
                "overflow_content": entry.chunk_content or entry.evidence,
            }
            return FillRequest(
                entity_title=target_node_id,
                fill_fields=resolved_value,
                source_evidence=entry.evidence,
                action="update",
                resolved_value=resolved_value,
                target_node_id=target_node_id,
            )

        if decision_name == "create_new":
            new_entity = decision.get("new_entity", {})
            if not isinstance(new_entity, dict):
                new_entity = {}
            title = new_entity.get("title") or entry.entity_title or "overflow_entity"
            description = (
                new_entity.get("description")
                or entry.entity_description
                or entry.evidence
            )
            entity_type = (
                new_entity.get("entity_type")
                or entry.entity_type
                or "CONFIGURATION"
            )

            try:
                added = self.graphrag.upsert_entities([{
                    "title": title,
                    "description": description,
                    "entity_type": entity_type,
                    "source_id": entry.source_file,
                }])
                report.entities_added += added
                parent_node_id = self._default_overflow_target(entry)
                if added > 0 and parent_node_id:
                    self.graphrag.add_relationships([{
                        "source": title,
                        "target": parent_node_id,
                        "type": "BELONGS_TO",
                        "description": entry.evidence,
                        "source_id": entry.source_file,
                    }])
                if added > 0:
                    report.entities_reembedded += self.graphrag.reembed_entities([title])
            except Exception as exc:
                report.errors.append(f"overflow create_new 失败: {exc}")

            return FillRequest(
                entity_title=title,
                source_evidence=entry.evidence,
                action="create_slot",
                target_node_id=title,
                new_node_template=self._build_new_node_template(
                    entry=entry,
                    node_id=title,
                    entity_type=entity_type,
                ),
            )

        return FillRequest(
            entity_title=entry.entity_title or "",
            source_evidence=entry.evidence,
            action="discard",
            target_node_id=entry.entity_title or "",
        )

    def _decide_conflict(self, entry: SchemaGapEntry) -> Dict[str, Any]:
        fallback = self._fallback_conflict_decision(entry)
        neighbors = self._get_neighbors(entry.entity_title)
        ctx = self._query_tree_context(entry.entity_title or "")
        prompt = (
            "你是知识图谱维护裁决者，只返回 JSON。\n"
            "输出格式: {\"decision\": \"accept_new|keep_skeleton|merge\", \"resolved_value\": ..., "
            "\"tree_level\": \"...\", \"enrich_fields\": {}, \"rationale\": \"...\"}\n"
            f"实体: {entry.entity_title}\n"
            f"冲突字段: {entry.field_name}\n"
            f"骨架值: {json.dumps(entry.skeleton_value, ensure_ascii=False)}\n"
            f"新值: {json.dumps(entry.new_value, ensure_ascii=False)}\n"
            f"证据: {entry.evidence}\n"
            f"树层级: {ctx.tree_level}\n"
            f"层级路径: {ctx.hierarchy_prefix}\n"
            f"已有 enrich 字段快照: {json.dumps(ctx.enrich_snapshot, ensure_ascii=False)}\n"
            f"父节点候选: {ctx.parent_candidate_id}\n"
            f"图上下文: {json.dumps(neighbors, ensure_ascii=False)}"
        )
        return self._run_decision(prompt, fallback)

    def _decide_overflow(self, entry: SchemaGapEntry) -> Dict[str, Any]:
        fallback = self._fallback_overflow_decision(entry)
        ctx = self._query_tree_context(entry.entity_title or "")
        valid_enrich = TREE_ENRICH_KEYS.get(ctx.tree_level, [])
        # 规则: 若字段名在该层级合法 enrich 键内，直接归为 attr_tag_update
        if entry.field_name and entry.field_name in valid_enrich:
            return {
                "decision": "merge_into",
                "target_node_id": ctx.matched_node_id or self._default_overflow_target(entry),
            }
        prompt = (
            "你是知识图谱维护裁决者，只返回 JSON。\n"
            "输出格式: {\"decision\": \"create_new|merge_into|discard\", \"target_node_id\": \"...\", \"new_entity\": {\"title\": \"...\", \"description\": \"...\", \"entity_type\": \"...\"}, \"rationale\": \"...\"}\n"
            f"候选标题: {entry.entity_title}\n"
            f"候选描述: {entry.entity_description}\n"
            f"最近节点: {json.dumps(entry.nearest_matches, ensure_ascii=False)}\n"
            f"树层级: {ctx.tree_level}\n"
            f"层级路径: {ctx.hierarchy_prefix}\n"
            f"树上已有: {ctx.exists_in_tree}\n"
            f"相似命令: {json.dumps(ctx.similar_commands, ensure_ascii=False)}\n"
            f"原始内容: {entry.chunk_content or entry.evidence}"
        )
        return self._run_decision(prompt, fallback)

    def _run_decision(self, prompt: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
        if self._chat_agent is None:
            return fallback

        try:
            msg = BaseMessage.make_user_message(role_name="Operator", content=prompt)
            response = self._chat_agent.step(msg)
            raw = response.msgs[0].content if response.msgs else ""
            parsed = self._parse_json_object(raw)
            if isinstance(parsed, dict) and "decision" in parsed:
                return parsed
        except Exception as exc:
            logger.warning("FarmOwner 裁决回退到规则模式: %s", exc)

        return fallback

    def _parse_json_object(self, raw: str) -> Dict[str, Any]:
        if not raw:
            return {}
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
        if fence:
            raw = fence.group(1).strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return {}
        try:
            parsed = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _fallback_conflict_decision(self, entry: SchemaGapEntry) -> Dict[str, Any]:
        if entry.new_value in (None, "", [], {}):
            return {"decision": "keep_skeleton", "resolved_value": entry.skeleton_value}
        if entry.skeleton_value in (None, "", [], {}):
            return {"decision": "accept_new", "resolved_value": entry.new_value}
        if entry.new_value == entry.skeleton_value:
            return {"decision": "keep_skeleton", "resolved_value": entry.skeleton_value}
        return {"decision": "keep_skeleton", "resolved_value": entry.skeleton_value}

    def _fallback_overflow_decision(self, entry: SchemaGapEntry) -> Dict[str, Any]:
        target_node_id = self._default_overflow_target(entry)
        if target_node_id:
            return {"decision": "merge_into", "target_node_id": target_node_id}
        if entry.entity_title or entry.entity_description or entry.chunk_content:
            return {
                "decision": "create_new",
                "new_entity": {
                    "title": entry.entity_title or "overflow_entity",
                    "description": entry.entity_description or entry.evidence,
                    "entity_type": entry.entity_type or "CONFIGURATION",
                },
            }
        return {"decision": "discard"}

    def _resolve_conflict_value(
        self,
        entry: SchemaGapEntry,
        decision_name: str,
        resolved_value: Any,
    ) -> Any:
        if isinstance(resolved_value, dict):
            if entry.field_name in resolved_value:
                return resolved_value[entry.field_name]
            if "value" in resolved_value:
                return resolved_value["value"]
        if resolved_value not in (None, "", [], {}):
            return resolved_value
        if decision_name == "accept_new":
            return entry.new_value
        if decision_name == "merge" and entry.new_value not in (None, "", [], {}):
            return entry.new_value
        return entry.skeleton_value

    def _default_overflow_target(self, entry: SchemaGapEntry) -> str:
        if not entry.nearest_matches:
            return ""
        match = entry.nearest_matches[0]
        return str(match.get("node_id", "") or match.get("entity_title", ""))

    def _build_new_node_template(
        self,
        entry: SchemaGapEntry,
        node_id: str,
        entity_type: str,
    ) -> Dict[str, Any]:
        metadata = {
            "node_id": node_id,
            "entity_type": entity_type,
            "source_file": entry.source_file,
        }
        parent_node_id = self._default_overflow_target(entry)
        if parent_node_id:
            metadata["parent_node_id"] = parent_node_id
        return {
            "page_content": entry.chunk_content or entry.evidence,
            "metadata": metadata,
        }

    def _get_neighbors(self, entity_title: str) -> Dict[str, Any]:
        if not entity_title or not hasattr(self.graphrag, "get_entity_neighbors"):
            return {"entities": [], "relationships": []}
        try:
            neighbors = self.graphrag.get_entity_neighbors(entity_title)
            if isinstance(neighbors, dict):
                return neighbors
            return {"entities": [], "relationships": []}
        except Exception:
            return {"entities": [], "relationships": []}

    # ── 懒加载树查询依赖 ───────────────────────────────────────────

    def _get_cli_graph(self):
        if self._cli_graph_override is not None:
            return self._cli_graph_override
        try:
            from INAGENT.rag.cli_graph_store import get_cli_graph_store
            return get_cli_graph_store()
        except Exception as exc:
            logger.warning("CLIGraphStore 不可用: %s", exc)
            return None

    def _get_skeleton_index(self):
        if self._skeleton_index_override is not None:
            return self._skeleton_index_override
        try:
            from INAGENT.rag.skeleton_index import get_skeleton_index
            return get_skeleton_index()
        except Exception as exc:
            logger.warning("SkeletonIndex 不可用: %s", exc)
            return None

    def _query_tree_context(self, entity_title: str) -> TreeContext:
        """纯读树查询，无副作用。失败时返回 exists_in_tree=False 的空上下文。"""
        ctx = TreeContext(entity_title=entity_title)
        if not entity_title:
            return ctx

        cli = self._get_cli_graph()
        si = self._get_skeleton_index()

        # Step 1: command_exists
        if cli is not None:
            try:
                exists, similar = cli.command_exists(entity_title)
                ctx.exists_in_tree = exists
                ctx.similar_commands = similar or []
            except Exception as exc:
                logger.debug("command_exists 异常: %s", exc)

        # Step 2: 若命中，查层级前缀推断 tree_level
        if ctx.exists_in_tree and cli is not None:
            try:
                prefix = cli.get_hierarchy_prefix(entity_title)
                ctx.hierarchy_prefix = prefix or ""
                # 推断层级：有前缀说明至少是 branch/leaf
                ctx.tree_level = self._infer_tree_level_from_cli(cli, entity_title)
            except Exception as exc:
                logger.debug("get_hierarchy_prefix 异常: %s", exc)

        # Step 3: derive_trunk（有 module_id 时）
        if cli is not None and ctx.tree_level in ("root", "trunk"):
            try:
                trunk = cli.derive_trunk(entity_title)
                ctx.trunk_info = trunk or {}
            except Exception as exc:
                logger.debug("derive_trunk 异常: %s", exc)

        # Step 4: branch_ids → parent_candidate
        if cli is not None and ctx.exists_in_tree:
            try:
                branches = cli.get_branch_ids(entity_title)
                ctx.branch_ids = branches or []
                if not ctx.parent_candidate_id and ctx.branch_ids:
                    ctx.parent_candidate_id = ctx.branch_ids[0]
            except Exception as exc:
                logger.debug("get_branch_ids 异常: %s", exc)

        # Step 5: resolve_module via SkeletonIndex
        if si is not None:
            try:
                module_id = si.resolve_module(entity_title)
                ctx.skeleton_module_id = module_id or ""
            except Exception as exc:
                logger.debug("resolve_module 异常: %s", exc)

        # Step 6: artifact_count
        if si is not None and ctx.skeleton_module_id:
            try:
                cnt = si.artifact_count(ctx.skeleton_module_id)
                ctx.skeleton_artifact_exists = cnt > 0
            except Exception as exc:
                logger.debug("artifact_count 异常: %s", exc)

        # Step 7: enrich_snapshot（从 CLI graph 节点直接读 enrich 字段）
        if cli is not None and ctx.exists_in_tree:
            try:
                ctx.enrich_snapshot = self._read_enrich_snapshot(cli, entity_title)
            except Exception as exc:
                logger.debug("enrich_snapshot 异常: %s", exc)

        return ctx

    def _infer_tree_level_from_cli(self, cli, entity_title: str) -> str:
        """根据 CLI graph 节点 type 推断树层级。"""
        try:
            cli._ensure_loaded()
            nid = entity_title.lower().replace(" ", "_")
            node = cli._nodes_by_id.get(nid) or cli._nodes_by_id.get(entity_title)
            if node is None:
                # 尝试通过 command_exists 中的相似 ID 找节点
                for nid_k, n in cli._nodes_by_id.items():
                    label = n.get("label", "").lower()
                    if label == entity_title.lower():
                        node = n
                        break
            if node is None:
                return "unknown"
            ntype = node.get("type", "")
            if ntype == "module":
                branches = cli.get_branch_ids(node.get("id", entity_title))
                return "trunk" if branches else "root"
            if ntype in ("command", "operation_command"):
                return "leaf"
            return "branch"
        except Exception:
            return "unknown"

    def _read_enrich_snapshot(self, cli, entity_title: str) -> Dict[str, Any]:
        """从 CLI graph 节点读取所有 enrich 字段的当前值。"""
        all_enrich_keys = set()
        for keys in TREE_ENRICH_KEYS.values():
            all_enrich_keys.update(keys)
        snapshot: Dict[str, Any] = {}
        try:
            cli._ensure_loaded()
            nid = entity_title.lower().replace(" ", "_")
            node = cli._nodes_by_id.get(nid) or cli._nodes_by_id.get(entity_title)
            if node:
                for k in all_enrich_keys:
                    if k in node:
                        snapshot[k] = node[k]
        except Exception:
            pass
        return snapshot
