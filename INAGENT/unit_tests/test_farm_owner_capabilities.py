# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
Mock 级能力检查：农场主与「树/叶子」、GraphRAG 挖槽、向量索引更新的关系。

- 「树架构」在此指 knowledge_base.json / 骨架文件；农场主代码路径不应写入这些文件。
- 「叶子挖槽」在 GraphRAG 侧对应实体动态列 add_entity_columns；填属性通过 FillRequest 交给下游。
- 「知识库更新」：农场主仅 snapshot_backup + 图 API + reload()；不调用 Qdrant/合并 KB 重建。
"""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent
from INAGENT.rag.knowledge_schema import SchemaGapEntry


_GRAPHRAG_SPEC = (
    "snapshot_backup",
    "reload",
    "add_entity_columns",
    "upsert_entities",
    "reembed_entities",
    "update_entity_fields",
    "replace_entity_embeddings",
    "add_relationships",
    "get_entity_neighbors",
)


def _make_graphrag_mock() -> MagicMock:
    """仅允许农场主实际会调用的 GraphRAG API；若代码误调 Qdrant 等会 AttributeError。"""
    g = MagicMock(spec=list(_GRAPHRAG_SPEC))
    g.snapshot_backup.return_value = Path("/tmp/mock_snap")
    g.add_entity_columns.return_value = ["new_col"]
    g.upsert_entities.return_value = 1
    g.reembed_entities.return_value = 1
    g.update_entity_fields.return_value = True
    g.replace_entity_embeddings.return_value = 1
    g.add_relationships.return_value = 1
    g.get_entity_neighbors.return_value = {"entities": [], "relationships": []}
    return g


def test_farm_owner_dig_slot_on_graph_via_add_entity_columns() -> None:
    """new_entity_attribute → 调用 graphrag.add_entity_columns（图侧挖槽）。"""
    g = _make_graphrag_mock()
    owner = KnowledgeFarmOwnerAgent(g, _chat_agent=None)
    entries = [
        SchemaGapEntry(
            gap_type="new_entity_attribute",
            entity_title="leaf_node_id",
            column_name="supports_override",
            column_dtype="bool",
            default_value=False,
            evidence="ev",
            source_file="cli.pdf",
        ),
    ]
    owner.process_gap_entries(entries)
    g.add_entity_columns.assert_called_once()
    specs = g.add_entity_columns.call_args[0][0]
    assert specs[0]["name"] == "supports_override"
    assert specs[0]["dtype"] == "bool"
    assert specs[0]["default"] is False


def test_farm_owner_new_leaf_entity_on_graph_via_upsert() -> None:
    """new_entity → upsert_entities + reembed_entities（图上的新「叶子」实体，非 KB JSON）。"""
    g = _make_graphrag_mock()
    owner = KnowledgeFarmOwnerAgent(g, _chat_agent=None)
    owner.process_gap_entries([
        SchemaGapEntry(
            gap_type="new_entity",
            entity_title="cli_new_command",
            entity_description="新命令说明",
            entity_type="CONFIGURATION",
            source_file="cli.pdf",
        ),
    ])
    g.upsert_entities.assert_called_once()
    patches = g.upsert_entities.call_args[0][0]
    assert patches[0]["title"] == "cli_new_command"
    g.reembed_entities.assert_called_once_with(["cli_new_command"])


def test_farm_owner_fill_attribute_intent_via_fill_request_not_graph_file() -> None:
    """有 default_value 时产出 FillRequest，由下游写 reference；农场主不写树文件。"""
    g = _make_graphrag_mock()
    owner = KnowledgeFarmOwnerAgent(g, _chat_agent=None)
    report = owner.process_gap_entries([
        SchemaGapEntry(
            gap_type="new_entity_attribute",
            entity_title="leaf_node_id",
            column_name="flag_col",
            column_dtype="bool",
            default_value=True,
            evidence="x",
            source_file="s.pdf",
        ),
    ])
    assert len(report.fill_requests) == 1
    fr = report.fill_requests[0]
    assert fr.fill_fields == {"flag_col": True}
    assert fr.target_node_id == "leaf_node_id"


def test_process_gap_entries_refresh_hybrid_vectors_calls_workflow(monkeypatch) -> None:
    called: dict[str, bool] = {}

    def fake_refresh(*, force: bool = True, use_graphrag=None):
        called["force"] = force
        called["ok"] = True
        return (None, None, None)

    monkeypatch.setattr(
        "INAGENT.workflow_config_generator.refresh_hybrid_vector_index",
        fake_refresh,
    )
    g = _make_graphrag_mock()
    owner = KnowledgeFarmOwnerAgent(g, _chat_agent=None)
    owner.process_gap_entries(
        [
            SchemaGapEntry(
                gap_type="new_entity",
                entity_title="E",
                entity_description="d",
            ),
        ],
        refresh_hybrid_vectors=True,
        hybrid_vectors_force=False,
    )
    assert called.get("ok") is True
    assert called.get("force") is False


def test_farm_owner_calls_reload_after_graph_writes() -> None:
    """process_gap_entries 末尾调用 graphrag.reload()（进程内图视图刷新）。"""
    g = _make_graphrag_mock()
    owner = KnowledgeFarmOwnerAgent(g, _chat_agent=None)
    owner.process_gap_entries([
        SchemaGapEntry(
            gap_type="new_entity",
            entity_title="E",
            entity_description="d",
        ),
    ])
    g.reload.assert_called_once()


def test_farm_owner_spec_mock_rejects_extra_apis() -> None:
    """spec Mock：农场主若调用未声明的检索/向量 API 会立即失败。"""
    g = _make_graphrag_mock()
    owner = KnowledgeFarmOwnerAgent(g, _chat_agent=None)
    owner.process_gap_entries([
        SchemaGapEntry(
            gap_type="conflict",
            entity_title="n",
            field_name="f",
            skeleton_value="a",
            new_value="b",
        ),
    ])
    with pytest.raises(AttributeError):
        g.rebuild_qdrant()  # 类型检查：spec 上不存在


def test_graphrag_retriever_reload_resets_cache_flags() -> None:
    """集成级说明：真实 GraphRAGRetriever.reload 会清空 _initialized（向量库不在此模块）。"""
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever

    src = inspect.getsource(GraphRAGRetriever.reload)
    assert "_initialized" in src
    assert "False" in src or "= False" in src


@pytest.mark.parametrize("gap_type,expect_tree_op", [
    ("new_entity", False),
    ("new_entity_attribute", False),
    ("conflict", False),
    ("overflow", False),
])
def test_farm_owner_never_opens_tree_json_for_write(
    tmp_path: Path, gap_type: str, expect_tree_op: bool,
) -> None:
    """通过拦截 open：process_gaps 只应打开 gaps 文件，不应以写模式打开 knowledge_base.json。"""
    assert expect_tree_op is False
    gaps = tmp_path / "g.jsonl"
    line = {
        "gap_type": gap_type,
        "entity_title": "n1",
        "entity_description": "d",
        "field_name": "f",
        "column_name": "c",
        "default_value": False,
        "skeleton_value": "x",
        "new_value": "y",
        "evidence": "e",
        "source_file": "s",
    }
    if gap_type == "new_entity_attribute":
        line.pop("field_name", None)
        line.pop("skeleton_value", None)
        line.pop("new_value", None)
    elif gap_type == "new_entity":
        line = {k: v for k, v in line.items() if k in (
            "gap_type", "entity_title", "entity_description", "source_file",
        )}
    elif gap_type == "conflict":
        line = {
            "gap_type": "conflict",
            "entity_title": "n1",
            "field_name": "f",
            "skeleton_value": "x",
            "new_value": "y",
            "evidence": "e",
            "source_file": "s",
        }
    elif gap_type == "overflow":
        line = {
            "gap_type": "overflow",
            "entity_title": "n1",
            "field_name": "extra",
            "new_value": 1,
            "evidence": "e",
            "source_file": "s",
            "chunk_content": "cc",
        }
    import json
    gaps.write_text(json.dumps(line) + "\n", encoding="utf-8")

    kb = tmp_path / "knowledge_base.json"
    kb.write_text("[]", encoding="utf-8")

    opened_for_write: list[str] = []
    real_open = open

    def tracking_open(file, mode="r", *a, **kw):
        p = str(file)
        if "w" in str(mode) or "a" in str(mode):
            opened_for_write.append(p)
        return real_open(file, mode, *a, **kw)

    g = _make_graphrag_mock()
    owner = KnowledgeFarmOwnerAgent(g, _chat_agent=None)

    import builtins
    builtins.open = tracking_open  # type: ignore[misc]
    try:
        owner.process_gaps(gaps)
    finally:
        builtins.open = real_open  # type: ignore[misc]

    kb_writes = [
        p for p in opened_for_write
        if "knowledge_base.json" in p.replace("\\", "/")
    ]
    assert kb_writes == [], f"农场主不应写 knowledge_base.json，实际写模式打开: {kb_writes}"
