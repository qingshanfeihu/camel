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
农场主 Agent (Knowledge Farm-Owner Agent) — TreeInformed 决策（与宪章 04 对齐）

职责：接收 schema_gaps.jsonl（由采购员/农民产出），对 GraphRAG 做结构性维护 ——
新实体、新列、conflict / overflow / ambiguous_match 的保守裁决；可选 classify_uncovered_chunks
（chunk 元数据补丁 FillRequest）。写入前 snapshot_backup；结束后 ``GraphRAGRetriever.reload()``。

**枝干场景培育**：cultivate_scenarios() 从 reference/knowledge_base.json 按 _tree_feature_id /
function_hierarchy 识别 branch/trunk/root 缺口，写 **reference/scenarios_scaffold.json**（骨架）；
HyDE 字段可选由 ``_model`` 生成。正文合成进 **scenarios_synthesized.json** 由农民完成（进向量索引）。
入口：INAGENT/scripts/run_scenario_cultivation.py

**决策**：_query_tree_context 懒加载 CLIGraphStore + SkeletonIndex（command_exists 签名为 Tuple[bool, List[str]]）；new_entity 规则优先，
未定条目批量 LLM（BATCH_SIZE=20）；overflow 在 TREE_ENRICH_KEYS 命中时可规则直连 merge_into；
overflow 无树上下文时 _fallback_overflow_decision 仍可用 nearest_matches 的 similarity>=0.9 作 merge 提示。
needs_tree_session → FillRequest + FarmOwnerReport.deferred。单测可注入 cli_graph、skeleton_index。

不写 knowledge_base.json。混合向量默认不刷新；``refresh_hybrid_vectors=True`` 时顺序为
reload → merge_knowledge_base → refresh_hybrid_vector_index（默认 hybrid_vectors_force=True 全量 Qdrant）。

**编排顺序（E2E）**：农民 ``write_to_reference`` 只写入 ``reference/{stem}.json``，
**不会**合并 ``knowledge_base.json``、也不会触发向量刷新。若管线在农场主
``process_gap_entries(..., refresh_hybrid_vectors=True)`` **之后**仍有
``write_to_reference``，则必须再由上层显式 ``merge_knowledge_base`` +
``refresh_hybrid_vector_index``（或等价的 RAG 重建）；否则混合检索仍读旧合并结果。
推荐顺序：所有目标 ``write_to_reference`` 完成 **早于** 带 ``refresh_hybrid_vectors=True``
的农场主步骤（见 ``INAGENT/docs/agents/sessions/04-farm-owner.md``、``DATA_FLOW.md`` §3.7）。

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
    TreeMutation,
    TREE_ENRICH_KEYS,
    build_tree_position,
    infer_document_category,
    infer_knowledge_role,
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
        self._model = model            # 用于 HyDE 规格生成（不做内容生成）
        self._chat_agent = _chat_agent
        self._overflow_field_denylist = _load_overflow_field_denylist()
        self._cli_graph_override = cli_graph
        self._skeleton_index_override = skeleton_index
        self._tree_context_cache: Dict[str, TreeContext] = {}
        self._decision_cache: Dict[str, Dict[str, Any]] = {}
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
                    "ambiguous_match",
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

        refresh_hybrid_vectors: GraphRAG reload 之后是否 ``merge_knowledge_base`` 再调用
            ``refresh_hybrid_vector_index``，使 Qdrant/BM25 与**本调用执行时** ``reference/*.json``
            的合并结果一致（需已配置 LLM 网关）。此前或此后农民的 ``write_to_reference`` 不会自动纳入，
            除非编排再次触发合并与刷新。
        hybrid_vectors_force: 为 True 时强制清空 Qdrant 再重嵌；仅当确定 merged KB 指纹已变
            且希望省耗时时可改为 False，走指纹判定逻辑。
        """
        report = FarmOwnerReport()
        self._tree_context_cache.clear()
        self._decision_cache.clear()

        try:
            report.snapshot_dir = self.graphrag.snapshot_backup(label="farm_owner")
        except Exception as exc:
            report.errors.append(f"snapshot_backup 失败: {exc}")
            return report

        entries = self._partition_valid_gap_entries(entries, report)

        conflict_entries = [entry for entry in entries if entry.gap_type == "conflict"]
        overflow_entries = [entry for entry in entries if entry.gap_type == "overflow"]
        non_knowledge_entries = [entry for entry in entries if entry.gap_type == "non_knowledge"]
        new_entity_entries = [entry for entry in entries if entry.gap_type == "new_entity"]
        attribute_entries = [
            entry for entry in entries if entry.gap_type == "new_entity_attribute"
        ]
        ambiguous_entries = [
            entry for entry in entries if entry.gap_type == "ambiguous_match"
        ]

        self._process_conflicts(conflict_entries, report)
        self._process_overflows(overflow_entries, report)
        self._process_non_knowledge(non_knowledge_entries, report)
        self._process_new_entities(new_entity_entries, report)
        self._process_attribute_gaps(attribute_entries, report)
        self._process_ambiguous_matches(ambiguous_entries, report)

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

        op_summary = report.operation_summary()
        logger.info(
            "FarmOwner 完成: +%d entities, +%s columns, %d embedded, "
            "%d conflicts, %d overflows, %d errors | ops=%d (errors=%d, incomplete=%d)",
            report.entities_added,
            report.columns_added,
            report.entities_reembedded,
            report.conflicts_resolved,
            report.overflows_handled,
            len(report.errors),
            op_summary["total_ops"],
            op_summary["errors"],
            op_summary["incomplete"],
        )
        if op_summary["by_phase"]:
            for phase, actions in op_summary["by_phase"].items():
                logger.info("  [农场主] %s: %s", phase, actions)
        return report

    # ── 枝干场景骨架培育（农场主职责：识别树型缺口 + 写规格，不加肉） ───

    def cultivate_scenarios(
        self,
        *,
        min_branch_cmds: int = 4,
        max_branch_cmds: int = 15,
        include_trunk: bool = True,
        include_root: bool = True,
        force_regenerate: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """全树层级缺口分析 + 骨架培育（农场主核心职责）。

        农场主是唯一懂整棵树结构的角色：
          root（根）→ trunk（躯干/模块）→ branch（枝干/功能组）→ leaf（叶/命令）
        当前 KB 只有 leaf（cli/reference），其他三层全部缺失。

        职责划分：
          - 农场主：遍历树、识别各层缺口、写骨架（metadata 规格 + HyDE 查询规格）
          - 农民：读骨架、LLM 填充内容（enrich_scenario_nodes）

        HyDE（Hypothetical Document Embeddings）役割：
          农场主不写内容，但用 LLM 生成「假设查询」（_hyde_query）和「内容规格」
          （_hyde_coverage_hints）——即"如果这个节点的文档写好了，什么查询应该能命中它"。
          这份规格是给农民的作业说明，也是 Phase 2 测试的评判依据。
          若 model=None，则只写结构规格（_hyde_query 留空），功能退化但不中断。

        骨架文件：scenarios_scaffold.json（不进入向量索引，仅供农民读取）
        场景文档：scenarios_synthesized.json（农民写入，进向量索引）

        Args:
            min_branch_cmds: branch 层最少命令数（默认 4）。
            max_branch_cmds: branch 层最多命令数（默认 15）。
            include_trunk: 是否扫描 trunk（模块）层缺口（默认 True）。
            include_root: 是否扫描 root（产品）层缺口（默认 True）。
            force_regenerate: 覆盖已有骨架（默认跳过）。
            dry_run: 只统计，不写文件。

        Returns:
            {"gaps_by_level", "scaffolded", "skipped", "errors", "dry_run", "scaffold_path"}
        """
        _ref_dir = Path(__file__).resolve().parent.parent / "knowledge_base" / "reference"
        _kb_path = _ref_dir / "knowledge_base.json"
        _out_path = _ref_dir / "scenarios_scaffold.json"

        summary: Dict[str, Any] = {
            "gaps_by_level": {"root": 0, "trunk": 0, "branch": 0},
            "scaffolded": 0,
            "skipped": 0,
            "errors": [],
            "dry_run": dry_run,
            "scaffold_path": str(_out_path),
        }

        if not _kb_path.exists():
            summary["errors"].append(f"knowledge_base.json 不存在: {_kb_path}")
            return summary

        kb_data = json.loads(_kb_path.read_text(encoding="utf-8"))
        existing = self._load_existing_scenarios(_out_path)
        existing_fids: set = {
            e.get("metadata", {}).get("tree_node_id", "") for e in existing
        }
        existing_fids.discard("")

        gap_plan = self._build_tree_gap_plan(
            kb_data,
            min_branch_cmds=min_branch_cmds,
            max_branch_cmds=max_branch_cmds,
            include_trunk=include_trunk,
            include_root=include_root,
        )

        for level, count in gap_plan["level_counts"].items():
            summary["gaps_by_level"][level] = count

        new_scaffolds: List[Dict[str, Any]] = []
        product = get_product_name()

        for gap in gap_plan["gaps"]:
            fid = gap["tree_node_id"]
            level = gap["_node_level"]

            if not force_regenerate and fid in existing_fids:
                logger.debug("cultivate_scenarios: 跳过已有骨架 %s [%s]", fid, level)
                summary["skipped"] += 1
                continue

            scaffold = self._build_scaffold_doc_typed(gap)

            if self._model is not None:
                try:
                    hyde = self._generate_hyde_spec(gap, product)
                    scaffold["metadata"]["_hyde_query"] = hyde.get("query", "")
                    scaffold["metadata"]["_hyde_coverage_hints"] = hyde.get("coverage_hints", "")
                    scaffold["metadata"]["_hyde_required_fields"] = hyde.get("required_fields", "")
                except Exception as exc:
                    logger.warning("HyDE 规格生成失败 (%s): %s", fid, exc)

            new_scaffolds.append(scaffold)
            summary["scaffolded"] += 1
            logger.info(
                "  骨架 [%s] %s (%s)",
                level,
                fid,
                "有HyDE规格" if scaffold["metadata"].get("_hyde_query") else "无HyDE",
            )

        if dry_run:
            logger.info(
                "cultivate_scenarios [dry-run]: gaps=%s 将新增 %d 骨架",
                summary["gaps_by_level"], len(new_scaffolds),
            )
            return summary

        if new_scaffolds:
            if force_regenerate:
                new_fids = {d["metadata"]["tree_node_id"] for d in new_scaffolds}
                merged = [
                    e for e in existing
                    if e.get("metadata", {}).get("tree_node_id", "") not in new_fids
                ]
            else:
                merged = list(existing)
            merged.extend(new_scaffolds)
            _out_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                "cultivate_scenarios: 骨架写入 %s (%d 条，新增 %d)",
                _out_path.name, len(merged), len(new_scaffolds),
            )

        logger.info(
            "cultivate_scenarios 完成: gaps=%s scaffolded=%d skipped=%d errors=%d",
            summary["gaps_by_level"], summary["scaffolded"],
            summary["skipped"], len(summary["errors"]),
        )
        return summary

    def _build_tree_gap_plan(
        self,
        kb_data: List[Dict[str, Any]],
        *,
        min_branch_cmds: int,
        max_branch_cmds: int,
        include_trunk: bool,
        include_root: bool,
    ) -> Dict[str, Any]:
        """遍历整棵树，识别各层缺口。

        Returns:
            {
                "gaps": [{"_node_level", "tree_node_id", ...gap-specific fields}],
                "level_counts": {"root": N, "trunk": N, "branch": N},
            }
        """
        from collections import defaultdict

        # ── 已有文档 map ──────────────────────────────────────────
        existing_tids: set = set()
        for item in kb_data:
            m = item.get("metadata", {})
            dc = m.get("document_category", "")
            tid = m.get("tree_node_id", "")
            if tid and dc not in ("cli/reference",):
                existing_tids.add(tid)

        # ── 叶节点聚合 (branch 分析基础) ──────────────────────────
        branch_raw: Dict[str, Dict] = defaultdict(lambda: {"leaves": [], "fh": "", "module": ""})
        for item in kb_data:
            m = item.get("metadata", {})
            fid = m.get("_tree_feature_id", "")
            tid = m.get("tree_node_id", "") or m.get("node_id", "")
            cp = m.get("command_prefix", "")
            if not fid or not tid or not cp:
                continue
            g = branch_raw[fid]
            seen = {l["tree_node_id"] for l in g["leaves"]}
            if tid not in seen:
                g["leaves"].append({
                    "tree_node_id": tid,
                    "command_prefix": cp,
                    "function_hierarchy": m.get("function_hierarchy", ""),
                    "product_module": m.get("product_module", ""),
                    "enriched_description": m.get("_enriched_description", ""),
                })
            if not g["fh"] and m.get("function_hierarchy"):
                g["fh"] = m["function_hierarchy"]
            if not g["module"] and m.get("product_module"):
                g["module"] = m["product_module"]

        gaps: List[Dict[str, Any]] = []
        level_counts: Dict[str, int] = {"root": 0, "trunk": 0, "branch": 0}

        # ── branch 层缺口 ─────────────────────────────────────────
        for fid, grp in branch_raw.items():
            n = len(grp["leaves"])
            if not (min_branch_cmds <= n <= max_branch_cmds):
                continue
            if fid in existing_tids:
                continue
            gaps.append({
                "_node_level": "branch",
                "tree_node_id": fid,
                "function_hierarchy": grp["fh"],
                "product_module": grp["module"],
                "leaves": grp["leaves"],
                "document_category": "scenario/scaffold",
            })
            level_counts["branch"] += 1

        # ── trunk 层缺口 ──────────────────────────────────────────
        if include_trunk:
            # function_hierarchy 首段 = trunk label
            trunk_to_branches: Dict[str, List[str]] = defaultdict(list)
            trunk_fh_map: Dict[str, str] = {}
            for fid, grp in branch_raw.items():
                fh = grp["fh"]
                trunk_label = fh.split(">")[0].strip() if fh else ""
                if trunk_label:
                    trunk_to_branches[trunk_label].append(fid)
                    trunk_fh_map[trunk_label] = fh

            for trunk_label, branch_fids in trunk_to_branches.items():
                trunk_tid = f"trunk_{trunk_label.lower().replace(' ', '_')}"
                if trunk_tid in existing_tids:
                    continue
                gaps.append({
                    "_node_level": "trunk",
                    "tree_node_id": trunk_tid,
                    "trunk_label": trunk_label,
                    "function_hierarchy": trunk_fh_map.get(trunk_label, trunk_label),
                    "child_feature_ids": branch_fids,
                    "document_category": "module/overview",
                })
                level_counts["trunk"] += 1

        # ── root 层缺口 ───────────────────────────────────────────
        if include_root and "product_root" not in existing_tids:
            all_trunks = [
                f"{g['tree_node_id']}({g['trunk_label']})"
                if g.get("trunk_label") else g["tree_node_id"]
                for g in gaps if g["_node_level"] == "trunk"
            ]
            gaps.append({
                "_node_level": "root",
                "tree_node_id": "product_root",
                "function_hierarchy": "",
                "child_trunk_ids": all_trunks,
                "document_category": "product/overview",
            })
            level_counts["root"] = 1

        return {"gaps": gaps, "level_counts": level_counts}

    def _build_scaffold_doc_typed(self, gap: Dict[str, Any]) -> Dict[str, Any]:
        """根据 gap 层级构造骨架条目（纯结构，无内容，无 LLM）。"""
        level = gap["_node_level"]
        fid = gap["tree_node_id"]
        fh = gap.get("function_hierarchy", "")
        dc = gap.get("document_category", "scenario/scaffold")

        meta: Dict[str, Any] = {
            "document_category": dc,
            "tree_node_id": fid,
            "_tree_feature_id": fid,
            "function_hierarchy": fh,
            "source_file": "scaffold",
            "_scaffold": "True",
            "_node_level": level,
        }

        if level == "branch":
            leaves = gap.get("leaves", [])
            cmd_prefixes = [l["command_prefix"] for l in leaves]
            node_ids = [l["tree_node_id"] for l in leaves]
            meta["section_title"] = fid.replace("_", " ") + " 功能配置"
            meta["product_module"] = gap.get("product_module", "")
            meta["scenario_commands"] = ",".join(node_ids)
            meta["scenario_command_prefixes"] = "|".join(cmd_prefixes)
            meta["_required_fields"] = "功能简介,配置步骤,每条命令说明,注意事项"
            placeholder = "[SCAFFOLD/branch] " + "、".join(cmd_prefixes)

        elif level == "trunk":
            trunk_label = gap.get("trunk_label", fid)
            child_fids = gap.get("child_feature_ids", [])
            meta["section_title"] = trunk_label + " 模块概述"
            meta["child_feature_ids"] = ",".join(child_fids)
            meta["_required_fields"] = "模块简介,子功能目录,各功能适用场景,CLI命令导航"
            placeholder = f"[SCAFFOLD/trunk] {trunk_label}: " + ", ".join(child_fids[:5])

        elif level == "root":
            child_trunks = gap.get("child_trunk_ids", [])
            meta["section_title"] = "产品功能全览"
            meta["child_trunk_ids"] = ",".join(child_trunks[:20])
            meta["_required_fields"] = "产品简介,功能模块目录,典型部署场景,快速导航"
            placeholder = "[SCAFFOLD/root] " + ", ".join(child_trunks[:8])

        else:
            placeholder = f"[SCAFFOLD/{level}] {fid}"

        return {"page_content": placeholder, "metadata": meta}

    def _generate_hyde_spec(self, gap: Dict[str, Any], product: str) -> Dict[str, str]:
        """用 LLM 生成 HyDE 查询规格（不写内容，只写「这个节点应该回答什么查询」）。

        HyDE 在此处的作用：
          农场主生成「假设文档存在时，用户会用什么查询命中它」——这就是 _hyde_query。
          同时生成「这份文档必须覆盖的内容要点」——这是给农民的作业说明。
          二者共同构成骨架的「规格」，不是内容本身。

        Returns:
            {"query": str, "coverage_hints": str, "required_fields": str}
        """
        level = gap["_node_level"]
        fid = gap["tree_node_id"]
        fh = gap.get("function_hierarchy", "")

        if level == "branch":
            leaves = gap.get("leaves", [])
            cmd_list = "、".join(l["command_prefix"] for l in leaves[:8])
            context = (
                f"功能组: {fid}  层级: {fh}\n"
                f"包含命令: {cmd_list}"
            )
            task = (
                "请给出：\n"
                "1. 用户最可能搜索此功能的查询句（_hyde_query，1句）\n"
                "2. 此文档必须覆盖的内容要点（_hyde_coverage_hints，逗号分隔，5-8项）\n"
                "3. 文档必须包含的字段（_required_fields，逗号分隔）\n"
                "只返回JSON：{\"query\":\"...\",\"coverage_hints\":\"...\",\"required_fields\":\"...\"}"
            )

        elif level == "trunk":
            trunk_label = gap.get("trunk_label", fid)
            child_fids = gap.get("child_feature_ids", [])[:6]
            context = (
                f"模块: {trunk_label}\n"
                f"子功能: {', '.join(child_fids)}"
            )
            task = (
                "请给出：\n"
                "1. 用户搜索此模块概览的典型查询句（_hyde_query，1句）\n"
                "2. 模块概述必须覆盖的内容要点（_hyde_coverage_hints，逗号分隔）\n"
                "3. 文档必须包含的字段（_required_fields，逗号分隔）\n"
                "只返回JSON：{\"query\":\"...\",\"coverage_hints\":\"...\",\"required_fields\":\"...\"}"
            )

        else:  # root
            child_trunks = gap.get("child_trunk_ids", [])[:8]
            context = f"产品: {product}\n功能模块: {', '.join(child_trunks)}"
            task = (
                "请给出：\n"
                "1. 用户搜索此产品全览的典型查询句（_hyde_query，1句）\n"
                "2. 产品概览必须覆盖的内容要点（_hyde_coverage_hints，逗号分隔）\n"
                "3. 文档必须包含的字段（_required_fields，逗号分隔）\n"
                "只返回JSON：{\"query\":\"...\",\"coverage_hints\":\"...\",\"required_fields\":\"...\"}"
            )

        prompt = (
            f"你是 {product} 知识库的文档架构师，负责定义文档规格（不写内容）。\n\n"
            f"{context}\n\n{task}"
        )

        try:
            user_msg = BaseMessage.make_user_message(role_name="user", content=prompt)
            # 使用独立 ChatAgent 避免污染裁决上下文
            from camel.agents import ChatAgent as _CA
            spec_agent = _CA(
                system_message=BaseMessage.make_assistant_message(
                    role_name="Doc Spec Architect",
                    content=f"你是 {product} 文档规格设计专家，只返回精简 JSON，不写内容。",
                ),
                model=self._model,
            )
            resp = spec_agent.step(user_msg)
            raw = resp.msgs[0].content if resp.msgs else "{}"
            return self._parse_hyde_json(raw)
        except Exception as exc:
            logger.debug("_generate_hyde_spec 异常 (%s): %s", fid, exc)
            return {}

    @staticmethod
    def _parse_hyde_json(raw: str) -> Dict[str, str]:
        """从 LLM 输出中提取 HyDE 规格 JSON。"""
        import re
        m = re.search(r"\{[^{}]+\}", raw, re.DOTALL)
        if not m:
            return {}
        try:
            d = json.loads(m.group())
            return {
                "query": str(d.get("query", "")),
                "coverage_hints": str(d.get("coverage_hints", "")),
                "required_fields": str(d.get("required_fields", "")),
            }
        except Exception:
            return {}

    @staticmethod
    def _collect_feature_groups(
        kb_data: List[Dict[str, Any]],
        min_cmds: int,
        max_cmds: int,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """按 _tree_feature_id 聚合叶节点（向后兼容用）。"""
        from collections import defaultdict
        raw: Dict[str, List] = defaultdict(list)
        for item in kb_data:
            m = item.get("metadata", {})
            fid = m.get("_tree_feature_id", "")
            tid = m.get("tree_node_id", "") or m.get("node_id", "")
            cp = m.get("command_prefix", "")
            if not fid or not tid or not cp:
                continue
            seen_tids = {leaf["tree_node_id"] for leaf in raw[fid]}
            if tid not in seen_tids:
                raw[fid].append({
                    "tree_node_id": tid,
                    "command_prefix": cp,
                    "function_hierarchy": m.get("function_hierarchy", ""),
                    "product_module": m.get("product_module", ""),
                    "enriched_description": m.get("_enriched_description", ""),
                    "page_content": item.get("page_content", ""),
                })
        return {
            fid: leaves
            for fid, leaves in raw.items()
            if min_cmds <= len(leaves) <= max_cmds
        }

    @staticmethod
    def _load_existing_scenarios(path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

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

    def _process_non_knowledge(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> None:
        if not entries:
            return

        product_name = (self._product_name or "").lower()
        _PRODUCT_KEYWORDS = {"slb", "llb", "gslb", "health check", "virtual", "real server",
                             "负载均衡", "健康检查", "会话保持", "持久化", "集群"}
        if product_name:
            _PRODUCT_KEYWORDS.add(product_name)

        discarded = 0
        rescued = 0
        for entry in entries:
            desc_lower = (entry.entity_description or "").lower()
            has_product_signal = any(kw in desc_lower for kw in _PRODUCT_KEYWORDS)

            if has_product_signal:
                entry.gap_type = "new_entity"
                entry.evidence = f"[rescued from non_knowledge] {entry.evidence}"
                rescued += 1
                continue

            ctx = TreeContext(entity_title=entry.entity_title or "")
            patch = self._build_chunk_meta_patch(ctx, gap_type="non_knowledge", action="discard")
            report.fill_requests.append(FillRequest(
                entity_title=entry.entity_title,
                source_evidence=entry.evidence,
                action="discard",
                target_block_id=getattr(entry, "chunk_block_id", "") or "",
                chunk_meta_patch=patch,
            ))
            report.log_op("non_knowledge", entry.entity_title, "discard",
                          entry.evidence, source="rule")
            discarded += 1

        if rescued:
            rescued_entries = [e for e in entries if e.gap_type == "new_entity"]
            self._process_new_entities(rescued_entries, report)

        logger.info("[农场主] non_knowledge: %d 条丢弃, %d 条降级为 new_entity", discarded, rescued)

    def _process_new_entities(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> None:
        if not entries:
            return

        entries = self._sanitize_abbreviation_titles(entries)

        # Pass 1: 规则决策 + 收集需要 LLM 的条目
        decided: List[tuple] = []  # (entry, ctx, action, enrich_fields, reason, source)
        pending_llm: List[tuple] = []  # (idx_in_decided, entry, ctx)

        for entry in entries:
            ctx = self._query_tree_context(entry.entity_title)
            action, enrich_fields, reason = self._decide_new_entity_action_rules_only(entry, ctx)
            if action is not None:
                decided.append((entry, ctx, action, enrich_fields, reason, "rule"))
            else:
                placeholder_idx = len(decided)
                decided.append((entry, ctx, None, {}, "", "rule"))
                pending_llm.append((placeholder_idx, entry, ctx))

        # Pass 2: batch LLM 裁决
        if pending_llm:
            items = []
            fallbacks = []
            for _, entry, ctx in pending_llm:
                has_info = bool(entry.entity_title or entry.entity_description)
                default_action = "tree_create_branch" if has_info else "discard"
                items.append({
                    "entity_title": entry.entity_title,
                    "entity_description": entry.entity_description[:300] if entry.entity_description else "",
                    "entity_type": entry.entity_type,
                    "evidence": entry.evidence,
                    "exists_in_tree": ctx.exists_in_tree,
                    "similar_commands": ctx.similar_commands,
                    "hierarchy_prefix": ctx.hierarchy_prefix,
                    "parent_candidate": ctx.parent_candidate_id,
                    "skeleton_module": ctx.skeleton_module_id,
                })
                fallbacks.append({"action": default_action, "enrich_fields": {}, "reason": "fallback"})

            batch_results = self._batch_run_decisions(items, fallbacks, "new_entity")
            for (placeholder_idx, entry, ctx), result in zip(pending_llm, batch_results):
                action = result.get("action", "discard")
                valid_actions = {
                    "tree_create_leaf", "tree_create_branch",
                    "tree_create_trunk", "tree_create_root",
                    "merge_into_existing", "needs_tree_session", "discard",
                }
                if action not in valid_actions:
                    action = "discard"
                enrich_fields = result.get("enrich_fields") or {}
                if not isinstance(enrich_fields, dict):
                    enrich_fields = {}
                reason = result.get("reason", "")
                decided[placeholder_idx] = (entry, ctx, action, enrich_fields, reason, "llm")

        # Pass 3: 执行副作用
        for entry, ctx, action, enrich_fields, reason, *rest in decided:
            src = rest[0] if rest else "rule"
            self._apply_new_entity_action(entry, ctx, action, enrich_fields, reason, report, source=src)

        logger.info(
            "[农场主] new_entity: %d 条处理 (rule=%d, llm=%d)",
            len(decided), len(decided) - len(pending_llm), len(pending_llm),
        )

    def _decide_new_entity_action(
        self,
        entry: SchemaGapEntry,
        ctx: TreeContext,
        report: FarmOwnerReport,
    ):
        """规则先行，规则无法判断交 LLM。返回 (action, enrich_fields, reason)。"""
        result = self._decide_new_entity_action_rules_only(entry, ctx)
        if result[0] is not None:
            return result
        return self._llm_decide_new_entity(entry, ctx, report)

    def _decide_new_entity_action_rules_only(
        self,
        entry: SchemaGapEntry,
        ctx: TreeContext,
    ):
        """纯规则决策。返回 (action, enrich_fields, reason)；action=None 表示需要 LLM。"""
        if ctx.exists_in_tree and ctx.skeleton_artifact_exists:
            return "merge_into_existing", {}, "树上已有且 artifact 已注册"

        if ctx.exists_in_tree and not ctx.skeleton_artifact_exists:
            return "merge_into_existing", {}, "树上已有但缺 artifact 注册"

        if not ctx.exists_in_tree and ctx.parent_candidate_id:
            entity_type = (entry.entity_type or "").upper()
            action = "tree_create_leaf" if "COMMAND" in entity_type else "tree_create_branch"
            return action, {}, f"父节点候选 {ctx.parent_candidate_id}"

        return None, {}, ""

    _ABBREV_RE = re.compile(r'\b([A-Z][A-Z0-9]{1,10})\b')

    def _sanitize_abbreviation_titles(
        self, entries: List[SchemaGapEntry]
    ) -> List[SchemaGapEntry]:
        """M6: 检测误翻的缩写标题。

        如果 entity_title 全中文，但 evidence/description 含英文缩写，
        且中文标题与网络/负载均衡无关，用原始缩写替换标题，
        中文保留到 entity_description 的 codename 段。
        """
        _CJK_ONLY = re.compile(r'^[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\s]+$')
        sanitized = []
        for entry in entries:
            title = (entry.entity_title or "").strip()
            if not title or not _CJK_ONLY.match(title):
                sanitized.append(entry)
                continue
            evidence = entry.evidence or ""
            desc = entry.entity_description or ""
            combined = evidence + " " + desc
            abbrevs = self._ABBREV_RE.findall(combined)
            abbrevs = [a for a in abbrevs if len(a) >= 2 and a not in ("CLI", "LLM", "JSON", "PDF", "API")]
            if not abbrevs:
                sanitized.append(entry)
                continue
            best_abbrev = abbrevs[0]
            logger.info(
                "[sanitize-abbrev] 替换误翻标题: '%s' → '%s' (原中文保留为 codename)",
                title, best_abbrev,
            )
            entry = SchemaGapEntry(
                gap_type=entry.gap_type,
                entity_title=best_abbrev,
                entity_description=f"{desc} [codename: {title}]" if desc else f"[codename: {title}]",
                entity_type=entry.entity_type,
                evidence=entry.evidence,
                source_file=entry.source_file,
            )
            sanitized.append(entry)
        return sanitized

    def _apply_new_entity_action(
        self,
        entry: SchemaGapEntry,
        ctx: TreeContext,
        action: str,
        enrich_fields: dict,
        reason: str,
        report: FarmOwnerReport,
        *,
        source: str = "rule",
    ) -> None:
        if action == "discard":
            report.log_op("new_entity", entry.entity_title, "discard", reason, source=source)
            report.discarded_count += 1
            report.discarded_entities.append(entry.entity_title)
            report.fill_requests.append(FillRequest(
                entity_title=entry.entity_title,
                source_evidence=entry.evidence,
                action="discard",
                target_node_id=entry.entity_title,
                chunk_meta_patch=self._build_chunk_meta_patch(ctx, gap_type="new_entity", action="discard"),
            ))
            return

        if action == "needs_tree_session":
            report.deferred.append(FillRequest(
                entity_title=entry.entity_title,
                source_evidence=entry.evidence,
                action="needs_tree_session",
                target_node_id=entry.entity_title,
                tree_level=ctx.tree_level,
                enrich_fields=enrich_fields,
            ))
            report.log_op("new_entity", entry.entity_title, "needs_tree_session", reason,
                          status="INCOMPLETE", source=source)
            return

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
            report.log_op("new_entity", entry.entity_title, action, f"upsert失败: {exc}",
                          status="ERROR", source=source)
            return

        report.log_op("new_entity", entry.entity_title, action, reason, source=source,
                      partial_data={"added": added, "enrich_fields": list(enrich_fields.keys())} if enrich_fields else None)

        if report.entities_added > 0 or action == "merge_into_existing":
            try:
                report.entities_reembedded += self.graphrag.reembed_entities(
                    [entry.entity_title]
                )
            except Exception as exc:
                report.errors.append(f"reembed_entities 失败: {exc}")

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

        # 兜底初始化，确保后续代码无论 action 是否进入 if 块都不会 NameError
        _effective_tree_level = ctx.tree_level
        if action in ("merge_into_existing", "tree_create_leaf", "tree_create_branch",
                      "tree_create_trunk", "tree_create_root"):
            _ACTION_TO_MUTATION = {
                "tree_create_leaf": "create_leaf",
                "tree_create_branch": "create_branch",
                "tree_create_trunk": "create_trunk",
                "tree_create_root": "create_root",
                "merge_into_existing": "update_fields",
            }
            mutation_type = _ACTION_TO_MUTATION.get(action, "update_fields")
            # new_leaf: CLI 文档中有语法但 CT 中不存在的命令节点
            _effective_tree_level = ctx.tree_level
            if action == "tree_create_leaf" and (entry.entity_type or "").upper() == "COMMAND":
                _effective_tree_level = "new_leaf"
            report.tree_mutations.append(TreeMutation(
                mutation_type=mutation_type,
                node_id=entry.entity_title,
                parent_node_id=ctx.parent_candidate_id,
                tree_level=_effective_tree_level,
                fields=enrich_fields,
                source_gap_type="new_entity",
            ))
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

        # 为 chunk_meta_patch 构建使用 effective tree_level 的上下文副本
        _patch_ctx = ctx
        if _effective_tree_level != ctx.tree_level:
            from dataclasses import replace as _dc_replace
            _patch_ctx = _dc_replace(ctx, tree_level=_effective_tree_level)
        _fill_tree_level = _effective_tree_level if action in (
            "tree_create_leaf", "tree_create_branch",
            "tree_create_trunk", "tree_create_root", "merge_into_existing",
        ) else ctx.tree_level
        report.fill_requests.append(FillRequest(
            entity_title=entry.entity_title,
            fill_fields=enrich_fields,
            source_evidence=entry.evidence,
            action=action,
            target_node_id=ctx.matched_node_id or entry.entity_title,
            tree_level=_fill_tree_level,
            enrich_fields=enrich_fields,
            target_block_id=getattr(entry, "chunk_block_id", "") or "",
            chunk_meta_patch=self._build_chunk_meta_patch(
                _patch_ctx, gap_type="new_entity", action=action,
            ),
        ))

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
            "输出格式: {\"action\": \"tree_create_leaf|tree_create_branch|tree_create_trunk|tree_create_root|merge_into_existing|needs_tree_session|discard\", "
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
            "新建躯干(tree_create_trunk)、新建根(tree_create_root)、"
            "挂入已有节点(merge_into_existing)、等待树会话(needs_tree_session)、还是丢弃(discard)。"
        )
        result = self._run_decision(
            prompt, {"action": default_action, "enrich_fields": {}, "reason": "fallback"}
        )
        action = result.get("action", default_action)
        valid_actions = {
            "tree_create_leaf", "tree_create_branch",
            "tree_create_trunk", "tree_create_root",
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
            attr_ctx = self._query_tree_context(entry.entity_title)
            report.fill_requests.append(FillRequest(
                entity_title=entry.entity_title,
                fill_fields={entry.column_name: entry.default_value},
                source_evidence=entry.evidence,
                action="update",
                resolved_value={entry.column_name: entry.default_value},
                target_node_id=entry.entity_title,
                chunk_meta_patch=self._build_chunk_meta_patch(attr_ctx, gap_type="new_entity_attribute", action="update"),
            ))
            report.log_op("attribute", entry.entity_title, "add_column",
                          f"column={entry.column_name}", source="rule")

        logger.info("[农场主] attribute: %d 条处理, columns=%s", len(entries), report.columns_added)

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

    def _process_ambiguous_matches(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> None:
        if not entries:
            return

        items = []
        fallbacks = []
        for entry in entries:
            candidates = entry.ambiguous_candidates or []
            ctx = self._query_tree_context(entry.entity_title or "")

            if len(candidates) == 1:
                report.fill_requests.append(FillRequest(
                    entity_title=entry.entity_title,
                    fill_fields={"tree_node_id": candidates[0].get("node_id", "")},
                    source_evidence=entry.evidence,
                    action="update",
                    target_node_id=candidates[0].get("node_id", ""),
                    chunk_meta_patch=self._build_chunk_meta_patch(ctx, gap_type="ambiguous_match", action="update"),
                ))
                report.log_op("ambiguous", entry.entity_title, "auto_resolve",
                              "single candidate", source="rule")
                continue

            if not candidates:
                report.log_op("ambiguous", entry.entity_title, "discard",
                              "no candidates", source="rule")
                continue

            items.append({
                "entity_title": entry.entity_title,
                "entity_description": entry.entity_description[:300] if entry.entity_description else "",
                "candidates": [
                    {"node_id": c.get("node_id", ""), "score": c.get("score", 0),
                     "command_prefix": c.get("command_prefix", "")}
                    for c in candidates[:5]
                ],
                "tree_level": ctx.tree_level,
                "content": (entry.chunk_content or entry.evidence)[:300],
            })
            fallbacks.append({
                "decision": "pick",
                "target_node_id": candidates[0].get("node_id", ""),
                "rationale": "fallback: pick highest score",
            })

        if items:
            decisions = self._batch_run_decisions(items, fallbacks, "ambiguous")
            item_idx = 0
            for entry in entries:
                if not entry.ambiguous_candidates or len(entry.ambiguous_candidates) <= 1:
                    continue
                decision = decisions[item_idx]
                item_idx += 1
                target = decision.get("target_node_id", "")
                if not target:
                    report.log_op("ambiguous", entry.entity_title, "discard",
                                  "LLM returned no target", source="llm")
                    continue
                amb_ctx = self._query_tree_context(entry.entity_title or "")
                report.fill_requests.append(FillRequest(
                    entity_title=entry.entity_title,
                    fill_fields={"tree_node_id": target},
                    source_evidence=entry.evidence,
                    action="update",
                    target_node_id=target,
                    chunk_meta_patch=self._build_chunk_meta_patch(amb_ctx, gap_type="ambiguous_match", action="update"),
                ))
                report.log_op("ambiguous", entry.entity_title, "pick",
                              decision.get("rationale", ""), source="llm",
                              partial_data={"target": target})

        logger.info("[农场主] ambiguous: %d 条处理", len(entries))

    def _process_conflicts(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> None:
        if not entries:
            return

        items = []
        fallbacks = []
        for entry in entries:
            ctx = self._query_tree_context(entry.entity_title or "")
            neighbors = self._get_neighbors(entry.entity_title)
            items.append({
                "entity": entry.entity_title,
                "conflict_field": entry.field_name,
                "skeleton_value": entry.skeleton_value,
                "new_value": entry.new_value,
                "evidence": entry.evidence,
                "tree_level": ctx.tree_level,
                "hierarchy_prefix": ctx.hierarchy_prefix,
                "enrich_snapshot": ctx.enrich_snapshot,
                "parent_candidate": ctx.parent_candidate_id,
                "graph_context": neighbors,
            })
            fallbacks.append(self._fallback_conflict_decision(entry))

        decisions = self._batch_run_decisions(items, fallbacks, "conflict")

        for entry, decision in zip(entries, decisions):
            fill_request = self._apply_conflict_decision(entry, decision, report)
            if fill_request is None:
                continue
            report.fill_requests.append(fill_request)
            report.conflicts_resolved += 1

        logger.info("[农场主] conflict: %d 条处理, %d 已解决", len(entries), report.conflicts_resolved)

    def _apply_conflict_decision(
        self,
        entry: SchemaGapEntry,
        decision: Dict[str, Any],
        report: FarmOwnerReport,
    ) -> Optional[FillRequest]:
        if not entry.entity_title or not entry.field_name:
            report.errors.append("conflict 条目缺少 entity_title 或 field_name")
            report.log_op("conflict", entry.entity_title or "?", "skip",
                          "missing entity_title or field_name", status="ERROR")
            return None

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
                report.log_op("conflict", entry.entity_title, decision_name,
                              f"graphrag update failed: {exc}", status="ERROR", source="llm")
            else:
                report.log_op("conflict", entry.entity_title, decision_name,
                              decision.get("rationale", ""), source="llm",
                              partial_data={"field": entry.field_name})
        else:
            report.log_op("conflict", entry.entity_title, decision_name,
                          decision.get("rationale", ""), source="llm",
                          partial_data={"field": entry.field_name})

        fill_fields = {entry.field_name: final_value}
        ctx = self._query_tree_context(entry.entity_title)
        return FillRequest(
            entity_title=entry.entity_title,
            fill_fields=fill_fields,
            source_evidence=entry.evidence,
            action="update",
            resolved_value=fill_fields,
            target_node_id=entry.entity_title,
            chunk_meta_patch=self._build_chunk_meta_patch(ctx, gap_type="conflict", action="update"),
        )

    def _process_overflows(
        self,
        entries: List[SchemaGapEntry],
        report: FarmOwnerReport,
    ) -> None:
        if not entries:
            return

        # Pass 1: 规则筛选 + 收集需要 LLM 的条目
        rule_decided: List[tuple] = []  # (entry, decision_or_None)
        pending_llm: List[tuple] = []   # (idx, entry, ctx)

        for entry in entries:
            if entry.field_name in self._overflow_field_denylist:
                rule_decided.append((entry, {"decision": "discard_denylist"}))
                continue

            ctx = self._query_tree_context(entry.entity_title or "")
            valid_enrich = TREE_ENRICH_KEYS.get(ctx.tree_level, [])
            if entry.field_name and entry.field_name in valid_enrich:
                rule_decided.append((entry, {
                    "decision": "merge_into",
                    "target_node_id": ctx.matched_node_id or self._default_overflow_target(entry),
                }))
                continue

            placeholder_idx = len(rule_decided)
            rule_decided.append((entry, None))
            pending_llm.append((placeholder_idx, entry, ctx))

        # Pass 2: batch LLM
        if pending_llm:
            items = []
            fallbacks = []
            for _, entry, ctx in pending_llm:
                items.append({
                    "entity_title": entry.entity_title,
                    "entity_description": entry.entity_description[:300] if entry.entity_description else "",
                    "nearest_matches": entry.nearest_matches,
                    "tree_level": ctx.tree_level,
                    "hierarchy_prefix": ctx.hierarchy_prefix,
                    "exists_in_tree": ctx.exists_in_tree,
                    "similar_commands": ctx.similar_commands,
                    "content": (entry.chunk_content or entry.evidence)[:300],
                })
                fallbacks.append(self._fallback_overflow_decision(entry))

            batch_results = self._batch_run_decisions(items, fallbacks, "overflow")
            for (placeholder_idx, entry, ctx), decision in zip(pending_llm, batch_results):
                rule_decided[placeholder_idx] = (entry, decision)

        # Pass 3: 执行副作用
        for entry, decision in rule_decided:
            if decision is None:
                continue
            if decision.get("decision") == "discard_denylist":
                denylist_ctx = self._query_tree_context(entry.entity_title or "")
                report.fill_requests.append(FillRequest(
                    entity_title=entry.entity_title or "",
                    source_evidence=entry.evidence,
                    action="discard",
                    target_node_id=entry.entity_title or "",
                    chunk_meta_patch=self._build_chunk_meta_patch(denylist_ctx, gap_type="overflow", action="discard"),
                ))
                report.discarded_count += 1
                report.discarded_entities.append(entry.entity_title or "")
                report.log_op("overflow", entry.entity_title or "", "discard_denylist",
                              f"field={entry.field_name}", source="rule")
                report.overflows_handled += 1
                continue
            fill_request = self._apply_overflow_decision(entry, decision, report)
            if fill_request is not None:
                report.fill_requests.append(fill_request)
            report.overflows_handled += 1

        logger.info(
            "[农场主] overflow: %d 条处理 (rule=%d, llm=%d)",
            len(rule_decided), len(rule_decided) - len(pending_llm), len(pending_llm),
        )

    def _apply_overflow_decision(
        self,
        entry: SchemaGapEntry,
        decision: Dict[str, Any],
        report: FarmOwnerReport,
    ) -> Optional[FillRequest]:
        decision_name = decision.get("decision", "discard")

        if decision_name == "merge_into":
            target_node_id = decision.get("target_node_id") or self._default_overflow_target(entry)
            merge_ctx = self._query_tree_context(target_node_id or entry.entity_title or "")
            resolved_value = {
                "overflow_content": entry.chunk_content or entry.evidence,
            }
            report.log_op("overflow", entry.entity_title or "", "merge_into",
                          decision.get("rationale", ""), source="llm",
                          partial_data={"target": target_node_id})
            return FillRequest(
                entity_title=target_node_id,
                fill_fields=resolved_value,
                source_evidence=entry.evidence,
                action="update",
                resolved_value=resolved_value,
                target_node_id=target_node_id,
                chunk_meta_patch=self._build_chunk_meta_patch(merge_ctx, gap_type="overflow", action="merge_into"),
            )

        if decision_name == "create_new":
            entry_ctx = self._query_tree_context(entry.entity_title or "")
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
                report.log_op("overflow", title, "create_new",
                              f"upsert failed: {exc}", status="ERROR", source="llm")
            else:
                report.log_op("overflow", title, "create_new",
                              decision.get("rationale", ""), source="llm")

            report.tree_mutations.append(TreeMutation(
                mutation_type="create_leaf",
                node_id=title,
                parent_node_id=self._default_overflow_target(entry) or "",
                fields={"entity_type": entity_type},
                source_gap_type="overflow",
            ))

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
                chunk_meta_patch=self._build_chunk_meta_patch(entry_ctx, gap_type="overflow", action="create_slot"),
            )

        discard_ctx = self._query_tree_context(entry.entity_title or "")
        report.log_op("overflow", entry.entity_title or "", "discard",
                      decision.get("rationale", ""), source="llm")
        report.discarded_count += 1
        report.discarded_entities.append(entry.entity_title or "")
        return FillRequest(
            entity_title=entry.entity_title or "",
            source_evidence=entry.evidence,
            action="discard",
            target_node_id=entry.entity_title or "",
            chunk_meta_patch=self._build_chunk_meta_patch(discard_ctx, gap_type="overflow", action="discard"),
        )

    def _run_decision(self, prompt: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
        if self._chat_agent is None:
            return fallback

        try:
            msg = BaseMessage.make_user_message(role_name="Operator", content=prompt)
            response = self._chat_agent.step(msg)
            raw = response.msgs[0].content if response.msgs else ""
            parsed = self._parse_json_object(raw)
            if isinstance(parsed, dict) and ("decision" in parsed or "action" in parsed):
                return parsed
        except Exception as exc:
            logger.warning("FarmOwner 裁决回退到规则模式: %s", exc)

        return fallback

    BATCH_SIZE = 20

    def _batch_run_decisions(
        self,
        items: List[Dict[str, Any]],
        fallbacks: List[Dict[str, Any]],
        decision_type: str,
    ) -> List[Dict[str, Any]]:
        """批量 LLM 裁决：将多条 gap 打包成一次 LLM 调用。
        同一 entity_title + decision_type 命中缓存时跳过 LLM。

        Args:
            items: 每条的上下文信息（会序列化进 prompt）
            fallbacks: 与 items 等长的 fallback 列表
            decision_type: "new_entity" | "conflict" | "overflow"

        Returns:
            与 items 等长的决策列表
        """
        if not items:
            return []
        if self._chat_agent is None:
            return list(fallbacks)

        results: List[Dict[str, Any]] = [None] * len(items)  # type: ignore[list-item]
        uncached_indices: List[int] = []
        uncached_items: List[Dict[str, Any]] = []
        uncached_fallbacks: List[Dict[str, Any]] = []
        cache_hits = 0

        for i, (item, fb) in enumerate(zip(items, fallbacks)):
            entity_key = item.get("entity_title") or item.get("entity", "") or ""
            field_key = item.get("conflict_field") or item.get("field_name") or ""
            cache_key = f"{decision_type}:{entity_key}:{field_key}"
            cached = self._decision_cache.get(cache_key)
            if cached is not None:
                results[i] = cached
                cache_hits += 1
            else:
                uncached_indices.append(i)
                uncached_items.append(item)
                uncached_fallbacks.append(fb)

        if cache_hits:
            logger.info("[农场主] decision_cache hit: %d/%d (%s)", cache_hits, len(items), decision_type)

        if uncached_items:
            llm_results: List[Dict[str, Any]] = []
            for batch_start in range(0, len(uncached_items), self.BATCH_SIZE):
                batch_items = uncached_items[batch_start:batch_start + self.BATCH_SIZE]
                batch_fallbacks = uncached_fallbacks[batch_start:batch_start + self.BATCH_SIZE]
                batch_results = self._batch_run_decisions_single(
                    batch_items, batch_fallbacks, decision_type
                )
                llm_results.extend(batch_results)
            for idx, llm_result in zip(uncached_indices, llm_results):
                results[idx] = llm_result
                entity_key = items[idx].get("entity_title") or items[idx].get("entity", "") or ""
                field_key = items[idx].get("conflict_field") or items[idx].get("field_name") or ""
                cache_key = f"{decision_type}:{entity_key}:{field_key}"
                if entity_key:
                    self._decision_cache[cache_key] = llm_result

        return results

    def _batch_run_decisions_single(
        self,
        items: List[Dict[str, Any]],
        fallbacks: List[Dict[str, Any]],
        decision_type: str,
    ) -> List[Dict[str, Any]]:
        if len(items) == 1:
            return [self._run_decision(
                self._format_single_decision_prompt(items[0], decision_type),
                fallbacks[0],
            )]

        output_schemas = {
            "new_entity": (
                '{\"action\": \"tree_create_leaf|tree_create_branch|tree_create_trunk|tree_create_root|'
                'merge_into_existing|needs_tree_session|discard\", '
                '\"enrich_fields\": {}, \"reason\": \"...\"}'
            ),
            "conflict": (
                '{\"decision\": \"accept_new|keep_skeleton|merge\", '
                '\"resolved_value\": ..., \"rationale\": \"...\"}'
            ),
            "overflow": (
                '{\"decision\": \"create_new|merge_into|discard\", '
                '\"target_node_id\": \"...\", \"new_entity\": '
                '{\"title\": \"...\", \"description\": \"...\", '
                '\"entity_type\": \"...\"}, \"rationale\": \"...\"}'
            ),
            "ambiguous": (
                '{\"decision\": \"pick|discard\", '
                '\"target_node_id\": \"...\", \"rationale\": \"...\"}'
            ),
        }
        schema = output_schemas.get(decision_type, output_schemas["new_entity"])

        entries_text = []
        for i, item in enumerate(items):
            fields = "\n".join(f"  {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in item.items())
            entries_text.append(f"[{i}]\n{fields}")

        prompt = (
            "你是知识图谱维护裁决者。下面有多条待裁决条目，请对每条返回决策。\n"
            f"每条输出格式: {schema}\n"
            "请返回一个 JSON 数组，数组长度必须等于条目数，按序号对应。\n"
            "只返回 JSON 数组，不要返回其他内容。\n\n"
            + "\n\n".join(entries_text)
        )

        try:
            msg = BaseMessage.make_user_message(role_name="Operator", content=prompt)
            response = self._chat_agent.step(msg)
            raw = response.msgs[0].content if response.msgs else ""
            parsed = self._parse_json_array(raw)
            if isinstance(parsed, list) and len(parsed) == len(items):
                result = []
                decision_key = "action" if decision_type == "new_entity" else "decision"
                for i, (p, fb) in enumerate(zip(parsed, fallbacks)):
                    if isinstance(p, dict) and decision_key in p:
                        result.append(p)
                    else:
                        result.append(fb)
                return result
            logger.warning(
                "batch LLM 返回长度不匹配 (got %d, want %d)，逐条 fallback",
                len(parsed) if isinstance(parsed, list) else -1,
                len(items),
            )
        except Exception as exc:
            logger.warning("batch LLM 裁决失败，逐条 fallback: %s", exc)

        return list(fallbacks)

    def _format_single_decision_prompt(
        self, item: Dict[str, Any], decision_type: str
    ) -> str:
        fields = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in item.items())
        output_schemas = {
            "new_entity": (
                '输出格式: {\"action\": \"tree_create_leaf|tree_create_branch|'
                'merge_into_existing|needs_tree_session|discard\", '
                '\"enrich_fields\": {}, \"reason\": \"...\"}'
            ),
            "conflict": (
                '输出格式: {\"decision\": \"accept_new|keep_skeleton|merge\", '
                '\"resolved_value\": ..., \"rationale\": \"...\"}'
            ),
            "overflow": (
                '输出格式: {\"decision\": \"create_new|merge_into|discard\", '
                '\"target_node_id\": \"...\", \"rationale\": \"...\"}'
            ),
            "ambiguous": (
                '输出格式: {\"decision\": \"pick|discard\", '
                '\"target_node_id\": \"...\", \"rationale\": \"...\"}'
            ),
        }
        schema = output_schemas.get(decision_type, output_schemas["new_entity"])
        return f"你是知识图谱维护裁决者，只返回 JSON。\n{schema}\n\n{fields}"

    def _parse_json_array(self, raw: str) -> list:
        if not raw:
            return []
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
        if fence:
            raw = fence.group(1).strip()
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            return []
        try:
            parsed = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

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
        if target_node_id and entry.nearest_matches:
            top_match = entry.nearest_matches[0]
            similarity = float(top_match.get("similarity", 0.0) or 0.0)
            if similarity >= 0.9:
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

    _GAP_TYPE_CONFIDENCE: Dict[str, float] = {
        "new_entity": 0.7,
        "conflict": 0.85,
        "overflow": 0.6,
        "ambiguous_match": 0.7,
        "new_entity_attribute": 0.8,
        "non_knowledge": 0.3,
    }

    _ROOT_GUARD_KEYWORDS = frozenset({
        "架构", "协议栈", "转发面", "控制面", "平台", "硬件", "系统设计",
        "architecture", "protocol stack", "forwarding plane", "control plane",
    })

    def _build_chunk_meta_patch(
        self,
        ctx: TreeContext,
        gap_type: str = "",
        action: str = "",
        chunk_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        tree_level = ctx.tree_level
        if tree_level == "unknown":
            tree_level = self._infer_tree_level_from_meta(chunk_meta) if chunk_meta else "unknown"
        if tree_level == "unknown":
            tree_level = self._llm_infer_tree_level(ctx, chunk_meta)
        knowledge_role = infer_knowledge_role(tree_level)
        confidence = self._GAP_TYPE_CONFIDENCE.get(gap_type, 0.5)
        if ctx.exists_in_tree:
            confidence = max(confidence, 0.9)

        content_len = len((chunk_meta or {}).get("_content_preview", "").strip()) if chunk_meta else 0
        if content_len <= 50 and content_len > 0:
            confidence *= 0.5

        if tree_level == "root" and not ctx.exists_in_tree:
            _check = f"{ctx.entity_title} {(chunk_meta or {}).get('_content_preview', '')}".lower()
            if not any(kw in _check for kw in self._ROOT_GUARD_KEYWORDS):
                logger.debug("root guard: '%s' 降级为 trunk (无架构关键词)", ctx.entity_title)
                tree_level = "trunk"
                confidence = min(confidence, 0.4)
                knowledge_role = infer_knowledge_role(tree_level)

        linked_nodes = []
        if ctx.matched_node_id:
            linked_nodes.append(ctx.matched_node_id)

        patch: Dict[str, Any] = {
            "document_category": infer_document_category(tree_level),
            "tree_position": build_tree_position(
                tree_level=tree_level,
                linked_nodes=linked_nodes,
                confidence=confidence,
                knowledge_role=knowledge_role,
            ),
        }
        # 只在有确定值时同步写入 flat tree_level，避免用 "unknown" 覆盖已有有效值
        _KNOWN_LEVELS = {"leaf", "new_leaf", "branch", "trunk", "root"}
        if tree_level in _KNOWN_LEVELS:
            patch["tree_level"] = tree_level
        if action == "discard":
            patch["owner_excluded"] = True
        return patch

    @staticmethod
    def _is_garbage_block(page_content: str) -> bool:
        from INAGENT.utils.chunk_text_quality import is_garbage_page_content

        return is_garbage_page_content(page_content)

    def classify_uncovered_chunks(
        self,
        chunks: List[Dict[str, Any]],
        report: FarmOwnerReport,
    ) -> List[FillRequest]:
        requests: List[FillRequest] = []
        garbage_count = 0
        for chunk in chunks:
            meta = chunk.get("metadata") or {}
            tp = meta.get("tree_position")
            if isinstance(tp, dict) and tp.get("tree_level"):
                continue
            block_id = meta.get("chunk_id") or meta.get("block_id") or meta.get("node_id") or ""
            if not block_id:
                continue

            pc = chunk.get("page_content", "")
            if self._is_garbage_block(pc):
                garbage_count += 1
                patch = self._build_chunk_meta_patch(
                    TreeContext(entity_title=""), gap_type="uncovered", action="discard",
                )
                req = FillRequest(
                    entity_title=block_id,
                    source_evidence="garbage_block_detected",
                    action="discard",
                    target_block_id=block_id,
                    chunk_meta_patch=patch,
                )
                requests.append(req)
                continue

            entity_title = meta.get("tree_node_id") or meta.get("node_id") or ""
            ctx = self._query_tree_context(entity_title) if entity_title else TreeContext(entity_title="")

            chunk_meta_for_patch = dict(meta)
            chunk_meta_for_patch["_content_preview"] = pc[:300]
            patch = self._build_chunk_meta_patch(ctx, gap_type="uncovered", action="update", chunk_meta=chunk_meta_for_patch)
            req = FillRequest(
                entity_title=entity_title or block_id,
                source_evidence="uncovered_chunk_classification",
                action="update",
                target_block_id=block_id,
                chunk_meta_patch=patch,
            )
            requests.append(req)

        if requests:
            report.fill_requests.extend(requests)
            logger.info("[农场主] classify_uncovered: %d 块补充分类, %d 垃圾块排除",
                        len(requests) - garbage_count, garbage_count)
        return requests

    def _query_tree_context(self, entity_title: str) -> TreeContext:
        """纯读树查询，无副作用。同一 process_gap_entries 批次内按 entity_title 缓存结果。"""
        if entity_title and entity_title in self._tree_context_cache:
            return self._tree_context_cache[entity_title]
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

        # Step 1b: 对未命中的多-token 单纯 ASCII 命令，逐步截短查找父节点
        # 示例: "ipv6 ospf advertise" -> 试 "ipv6 ospf" -> CT命中 -> parent_candidate
        if not ctx.exists_in_tree and cli is not None and entity_title:
            _title_tokens = entity_title.strip().split()
            _all_ascii = all(t.isascii() for t in _title_tokens)
            if _all_ascii and len(_title_tokens) >= 3:
                for _end in range(len(_title_tokens) - 1, 1, -1):
                    _parent = " ".join(_title_tokens[:_end])
                    try:
                        _p_exists, _ = cli.command_exists(_parent)
                        if _p_exists:
                            ctx.parent_candidate_id = _parent
                            logger.debug(
                                "_query_tree_context: 前缀回退找到父节点 %s -> %s",
                                entity_title, _parent,
                            )
                            break
                    except Exception:
                        pass

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

        self._tree_context_cache[entity_title] = ctx
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
                return "trunk" if branches else "leaf"
            if ntype in ("command", "operation_command"):
                return "leaf"
            return "branch"
        except Exception:
            return "unknown"

    @staticmethod
    def _infer_tree_level_from_meta(chunk_meta: Optional[Dict[str, Any]]) -> str:
        """从 chunk metadata 结构信号推断 tree_level（不依赖 CLI graph）。

        信号优先级：
          0. LLM 在 auto_convert 阶段直接判定的 tree_level → 直接采用
          1. function_hierarchy 只有一级（纯模块名，无 > 分隔）→ trunk
          2. function_hierarchy 多级 + section_path 浅层且含概述关键词 → trunk
          3. function_hierarchy 多级 → branch
          4. 无法判定 → "unknown"
        """
        if not chunk_meta:
            return "unknown"

        llm_level = chunk_meta.get("tree_level", "")
        if llm_level in ("leaf", "new_leaf", "branch", "trunk", "root"):
            return llm_level

        fh = chunk_meta.get("function_hierarchy", "").strip()
        if fh:
            parts = [p.strip() for p in fh.replace(">", "/").split("/") if p.strip()]
            if len(parts) == 1:
                return "trunk"
            if len(parts) > 1:
                sp = chunk_meta.get("section_path", "").strip()
                sp_parts = [p.strip() for p in sp.split(">") if p.strip()] if sp else []
                _OVERVIEW_KW = ("功能原理", "工作机制", "概述", "简介", "总体介绍", "体系结构", "架构")
                if len(sp_parts) <= 1 and any(kw in sp for kw in _OVERVIEW_KW):
                    return "trunk"
                return "branch"

        return "unknown"

    def _llm_infer_tree_level(
        self, ctx: TreeContext, chunk_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """LLM 判定 tree_level。失败时兜底 branch + 低置信度标记。"""
        from INAGENT.rag.knowledge_schema import (
            NON_KNOWLEDGE_TITLE_PATTERNS,
            NON_KNOWLEDGE_CONTENT_KEYWORDS,
        )
        section_title = (chunk_meta or {}).get("section_title", ctx.entity_title or "")
        content_preview = (chunk_meta or {}).get("_content_preview", "")
        _check_text = f"{section_title} {content_preview}".lower()
        for pat in NON_KNOWLEDGE_TITLE_PATTERNS:
            if pat.lower() in _check_text:
                logger.debug("_llm_infer_tree_level: 废料拦截 title_pat=%s", pat)
                return "branch"
        for kw in NON_KNOWLEDGE_CONTENT_KEYWORDS:
            if kw.lower() in _check_text:
                logger.debug("_llm_infer_tree_level: 废料拦截 content_kw=%s", kw)
                return "branch"

        if self._chat_agent is None:
            return "branch"

        section_title = (chunk_meta or {}).get("section_title", ctx.entity_title or "")
        section_path = (chunk_meta or {}).get("section_path", "")
        product_module = (chunk_meta or {}).get("product_module", "")
        content_preview = (chunk_meta or {}).get("_content_preview", "")

        prompt = (
            "你是知识树分类专家，只返回 JSON。\n"
            "输出格式: {\"tree_level\": \"leaf|branch|trunk|root\", \"reason\": \"...\"}\n\n"
            "树层级定义:\n"
            "  leaf — CLI 命令/操作级知识（单条命令语法、参数）\n"
            "  branch — 子功能/配置场景（具体配置步骤、单一功能介绍）\n"
            "  trunk — 大功能模块概述（功能原理、工作机制、多子功能的综合描述）\n"
            "  root — 系统架构/协议栈顶层设计\n\n"
            f"标题: {section_title}\n"
            f"层级路径: {section_path}\n"
            f"所属模块: {product_module}\n"
            f"内容摘要: {content_preview[:300]}\n"
            f"树上是否存在: {ctx.exists_in_tree}\n"
            f"相似命令: {json.dumps(ctx.similar_commands[:3], ensure_ascii=False) if ctx.similar_commands else '[]'}\n"
            "请判断此知识块应归入哪个树层级。"
        )
        result = self._run_decision(
            prompt, {"tree_level": "branch", "reason": "llm_fallback"}
        )
        level = result.get("tree_level", "branch")
        valid = {"leaf", "branch", "trunk", "root"}
        if level not in valid:
            logger.warning("LLM 返回无效 tree_level '%s'，兜底 branch", level)
            return "branch"
        return level

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
