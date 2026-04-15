# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Tests for farm_owner_ingest classification pipeline."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.data_tools.farm_owner_ingest import (
    _ChunkTreeContext,
    _chunk_key,
    _reconcile_decisions,
    _stage1_rules_prefilter,
    _stage3_rules_classification,
    _stage5_assemble_decisions,
    _build_discard_row,
    _fallback_batch,
    _useful_content_length,
    run_farm_owner_pipeline,
)


def _make_chunk(
    source_file: str = "cli.json",
    block_id: str = "1",
    section_title: str = "SLB概述",
    page_content: str = "SLB概述\nslb virtual server的配置说明，支持负载均衡的多种算法和会话保持功能。",
    **extra_meta: object,
) -> dict:
    meta = {
        "source_file": source_file,
        "block_id": block_id,
        "section_title": section_title,
        "document_category": "cli/reference",
    }
    meta.update(extra_meta)
    return {"page_content": page_content, "metadata": meta}


# ── Stage 1 tests ──────────────────────────────────────────────────


def test_stage1_garbage_detection() -> None:
    chunks = [
        _make_chunk(page_content="..."),
        _make_chunk(page_content="ab", block_id="2"),
        _make_chunk(block_id="3", page_content="SLB概述\n正常的技术文档内容，包含slb virtual server配置说明，足够长。"),
    ]
    candidates, garbage, non_kw, wrong_prod = _stage1_rules_prefilter(chunks)
    assert len(garbage) == 2
    assert len(candidates) == 1
    assert len(non_kw) == 0


def test_stage1_non_knowledge_by_title() -> None:
    chunks = [
        _make_chunk(section_title="版权声明", page_content="版权声明\nArray Networks 版权所有 2024年 保留一切权利"),
        _make_chunk(section_title="商标声明", page_content="商标声明\n本文档中提及的商标均为各自所有者的注册商标", block_id="2"),
        _make_chunk(section_title="SLB配置", block_id="3"),
    ]
    candidates, garbage, non_kw, wrong_prod = _stage1_rules_prefilter(chunks)
    assert len(non_kw) == 2
    assert len(candidates) == 1


def test_stage1_non_knowledge_by_body_keyword() -> None:
    chunks = [
        _make_chunk(
            section_title="说明",
            page_content="说明\n版权所有 Array Networks Inc. 2024 未经许可不得复制或分发本文档",
        ),
        _make_chunk(section_title="功能配置", block_id="2"),
    ]
    candidates, garbage, non_kw, wrong_prod = _stage1_rules_prefilter(chunks)
    assert len(non_kw) == 1
    assert len(candidates) == 1


def test_stage1_thin_content_discarded() -> None:
    """Pure section headers with no real body should be discarded as garbage."""
    chunks = [
        _make_chunk(page_content="VLAN\n6.1.1. VLAN ", section_title="VLAN", block_id="h1"),
        _make_chunk(page_content="NAT\n6.1.4. NAT ", section_title="NAT", block_id="h2"),
        _make_chunk(
            page_content="NAT配置\n使用nat命令配置静态NAT映射规则，支持源地址和目标地址转换。",
            section_title="NAT配置",
            block_id="ok",
        ),
    ]
    candidates, garbage, non_kw, wrong_prod = _stage1_rules_prefilter(chunks)
    assert len(garbage) == 2
    assert len(candidates) == 1
    assert candidates[0]["metadata"]["block_id"] == "ok"


def test_useful_content_length() -> None:
    assert _useful_content_length("VLAN\n6.1.1. VLAN ", "VLAN") == 0
    assert _useful_content_length("NAT\n6.1.4. NAT ", "NAT") == 0
    assert _useful_content_length("AAA\n20.1. AAA ", "AAA") == 0
    assert _useful_content_length(
        "NAT配置\n使用nat命令配置静态NAT映射", "NAT配置"
    ) > 10


def test_stage5_wrong_product_generates_feedback() -> None:
    wrong_prod = [_make_chunk(
        source_file="HuaWei NAT64.json", block_id="h1",
        section_title="华为 USG9000 配置",
        page_content="在 USG9000 上配置 NAT64 需要进入 system-view",
    )]
    rows, stats, feedback = _stage5_assemble_decisions(
        [], [], wrong_prod, [], [], {},
    )
    assert stats["discarded"] == 1
    assert stats["wrong_product"] == 1
    wp_feedback = [f for f in feedback if f["reason_code"] == "wrong_product"]
    assert len(wp_feedback) == 1


# ── Stage 3 tests ──────────────────────────────────────────────────


def test_stage3_exact_tree_match_rule() -> None:
    chunk = _make_chunk(section_title="slb virtual")
    key = _chunk_key(chunk)
    ctx = _ChunkTreeContext(
        chunk_key=key,
        exists_in_tree=True,
        matched_node_id="slb_virtual",
        tree_level="leaf",
        skeleton_artifact_exists=True,
    )
    decided, undecided = _stage3_rules_classification(
        [chunk], {key: ctx}, {},
    )
    assert len(decided) == 1
    assert decided[0]["action"] == "merge_into_existing"
    assert decided[0]["target_node_id"] == "slb_virtual"
    assert decided[0]["tree_level"] == "leaf"
    assert decided[0]["source_evidence"] == "tree_exact_match_with_artifact"
    assert len(undecided) == 0


def test_stage3_parent_prefix_match_leaf() -> None:
    chunk = _make_chunk(
        section_title="slb virtual show",
        page_content="show slb virtual 命令参数说明。command reference",
    )
    key = _chunk_key(chunk)
    ctx = _ChunkTreeContext(
        chunk_key=key,
        exists_in_tree=False,
        parent_candidate_id="slb_virtual",
    )
    decided, undecided = _stage3_rules_classification(
        [chunk], {key: ctx}, {},
    )
    assert len(decided) == 1
    assert decided[0]["action"] == "tree_create_leaf"
    assert decided[0]["target_node_id"] == "slb_virtual"
    assert decided[0]["enrich_fields"]["parent_candidate"] == "slb_virtual"


def test_stage3_parent_prefix_match_branch() -> None:
    chunk = _make_chunk(
        section_title="SLB会话保持",
        page_content="会话保持功能介绍与应用场景说明。支持多种保持方式。",
    )
    key = _chunk_key(chunk)
    ctx = _ChunkTreeContext(
        chunk_key=key,
        exists_in_tree=False,
        parent_candidate_id="slb",
    )
    decided, undecided = _stage3_rules_classification(
        [chunk], {key: ctx}, {},
    )
    assert len(decided) == 1
    assert decided[0]["action"] == "tree_create_branch"
    assert decided[0]["target_node_id"] == "slb"
    assert decided[0]["tree_level"] == "branch"


def test_stage3_undecided_passes_to_llm() -> None:
    chunk = _make_chunk(section_title="未知功能文档")
    key = _chunk_key(chunk)
    ctx = _ChunkTreeContext(chunk_key=key)
    decided, undecided = _stage3_rules_classification(
        [chunk], {key: ctx}, {},
    )
    assert len(decided) == 0
    assert len(undecided) == 1


# ── Stage 5 tests ──────────────────────────────────────────────────


def test_stage5_assembles_all_categories() -> None:
    garbage = [_make_chunk(page_content="...", block_id="g1")]
    non_kw = [_make_chunk(section_title="版权声明", block_id="n1")]
    rules = [{
        "schema_version": "1.0",
        "source_file": "cli.json",
        "entity_title": "slb virtual",
        "action": "merge_into_existing",
        "target_node_id": "slb_virtual",
        "target_block_id": "r1",
        "tree_level": "leaf",
        "fill_fields": {},
        "enrich_fields": {},
        "chunk_meta_patch": {"source_file": "cli.json"},
        "source_evidence": "tree_exact_match",
        "resolved_value": {},
    }]
    llm = [{
        "schema_version": "1.0",
        "source_file": "cli.json",
        "entity_title": "新功能",
        "action": "tree_create_branch",
        "target_node_id": "",
        "target_block_id": "l1",
        "tree_level": "branch",
        "fill_fields": {},
        "enrich_fields": {},
        "chunk_meta_patch": {"source_file": "cli.json"},
        "source_evidence": "llm_classification:new_feature",
        "resolved_value": {},
    }]

    rows, stats, feedback = _stage5_assemble_decisions(
        garbage, non_kw, [], rules, llm, {},
    )
    assert len(rows) == 4
    assert stats["discarded"] == 2
    assert stats["direct_fill"] == 1
    assert stats["structural"] == 1
    assert stats["rules_decided"] == 3
    assert stats["llm_decided"] == 1
    assert len(feedback) > 0


def test_stage5_llm_discard_generates_feedback() -> None:
    llm_discard = [{
        "schema_version": "1.0",
        "source_file": "cli.json",
        "entity_title": "无用内容",
        "action": "discard",
        "target_node_id": "",
        "target_block_id": "d1",
        "tree_level": "",
        "fill_fields": {},
        "enrich_fields": {},
        "chunk_meta_patch": {"source_file": "cli.json"},
        "source_evidence": "llm_classification:useless",
        "resolved_value": {},
    }]

    rows, stats, feedback = _stage5_assemble_decisions(
        [], [], [], [], llm_discard, {},
    )
    assert stats["discarded"] == 1
    llm_feedback = [f for f in feedback if f["reason_code"] == "llm_classified_useless"]
    assert len(llm_feedback) == 1


# ── Discard row and fallback tests ──────────────────────────────────


def test_build_discard_row() -> None:
    chunk = _make_chunk()
    row = _build_discard_row(chunk, "test_reason", {})
    assert row["action"] == "discard"
    assert row["source_evidence"] == "test_reason"
    assert row["chunk_meta_patch"]["owner_excluded"] is True


def test_fallback_batch_marks_needs_tree_session() -> None:
    chunks = [_make_chunk(), _make_chunk(block_id="2")]
    rows = _fallback_batch(chunks, {})
    assert all(r["action"] == "needs_tree_session" for r in rows)
    assert all(r["source_evidence"] == "llm_unavailable_fallback" for r in rows)


# ── Pipeline integration tests ──────────────────────────────────────


def _write_test_gate_and_ref(ref: Path, chunks: list, gate_entries: list) -> None:
    (ref / "_quality_reference_for_owner.json").write_text(
        json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    gate_lines = [json.dumps(e, ensure_ascii=False) for e in gate_entries]
    (ref / "_quality_gate_for_owner.jsonl").write_text(
        "\n".join(gate_lines) + "\n", encoding="utf-8",
    )


def test_pipeline_classifies_chunks_with_mocked_stages(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()

    chunks = [_make_chunk(page_content="SLB概述\nslb virtual server配置，负载均衡的多种算法和会话保持功能详解。")]
    gate = [{
        "schema_version": "1.0",
        "source_file": "cli.json",
        "block_id": "1",
        "chunk_index": 0,
        "is_product_knowledge": True,
        "section_title": "SLB概述",
        "document_category": "cli/reference",
    }]
    _write_test_gate_and_ref(ref, chunks, gate)

    with patch(
        "INAGENT.data_tools.farm_owner_ingest._build_quality_passed_llm_patches",
        return_value={},
    ), patch(
        "INAGENT.data_tools.farm_owner_ingest._stage2_tree_context_lookup",
        return_value={
            "cli.json::1": _ChunkTreeContext(
                chunk_key="cli.json::1",
                exists_in_tree=True,
                matched_node_id="slb_virtual",
                tree_level="leaf",
            )
        },
    ):
        result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["processed"] is True
    assert result["quality_passed_blocks"] == 1
    assert result["classified_blocks"] == 1
    assert result["direct_fill_blocks"] == 1
    assert result["discarded_blocks"] == 0

    rows = [
        json.loads(line)
        for line in (ref / "_owner_decisions_for_farmer.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["action"] == "merge_into_existing"
    assert rows[0]["target_node_id"] == "slb_virtual"


def test_pipeline_requires_quality_reference_file(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "_quality_gate_for_owner.jsonl").write_text(
        json.dumps({
            "schema_version": "1.0", "source_file": "cli.json",
            "block_id": "blk-1", "chunk_index": 0, "is_product_knowledge": True,
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    result = run_farm_owner_pipeline(reference_dir=ref)
    assert result["processed"] is False
    assert result["reason"] == "missing quality filtered reference"


def test_pipeline_requires_quality_gate_file(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "_quality_reference_for_owner.json").write_text(
        json.dumps([_make_chunk()], ensure_ascii=False), encoding="utf-8",
    )
    result = run_farm_owner_pipeline(reference_dir=ref)
    assert result["processed"] is False
    assert result["reason"] == "missing quality gate file"


def test_pipeline_drops_quality_blocked_chunks(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()

    chunks = [
        _make_chunk(block_id="pass", page_content="SLB概述\n通过的块，内容与SLB负载均衡的多种算法和会话保持功能相关。"),
        _make_chunk(block_id="block", page_content="SLB概述\n被拦截的块，内容与SLB虚拟服务器配置细节不重要。"),
    ]
    gate = [
        {
            "schema_version": "1.0", "source_file": "cli.json",
            "block_id": "pass", "chunk_index": 0, "is_product_knowledge": True,
        },
        {
            "schema_version": "1.0", "source_file": "cli.json",
            "block_id": "block", "chunk_index": 1, "is_product_knowledge": False,
        },
    ]
    _write_test_gate_and_ref(ref, chunks, gate)

    with patch(
        "INAGENT.data_tools.farm_owner_ingest._build_quality_passed_llm_patches",
        return_value={},
    ), patch(
        "INAGENT.data_tools.farm_owner_ingest._stage2_tree_context_lookup",
        return_value={
            "cli.json::pass": _ChunkTreeContext(
                chunk_key="cli.json::pass",
                exists_in_tree=True,
                matched_node_id="slb",
                tree_level="leaf",
            ),
        },
    ):
        result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["processed"] is True
    assert result["quality_passed_blocks"] == 1

    rows = [
        json.loads(line)
        for line in (ref / "_owner_decisions_for_farmer.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["target_block_id"] == "pass"


def test_pipeline_gap_entries_produce_deferred_decisions(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()

    chunks = [_make_chunk(page_content="SLB概述\nslb virtual server的配置说明文档，包含多种负载均衡算法详解。")]
    gate = [{
        "schema_version": "1.0", "source_file": "cli.json",
        "block_id": "1", "chunk_index": 0, "is_product_knowledge": True,
    }]
    _write_test_gate_and_ref(ref, chunks, gate)

    (ref / "schema_gaps.jsonl").write_text(
        json.dumps({
            "gap_type": "non_knowledge", "entity_title": "版权声明",
            "entity_description": "文档尾部版权段落", "source_file": "cli.json",
            "chunk_block_id": "1", "evidence": "copyright section",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with patch(
        "INAGENT.data_tools.farm_owner_ingest._build_quality_passed_llm_patches",
        return_value={},
    ), patch(
        "INAGENT.data_tools.farm_owner_ingest._stage2_tree_context_lookup",
        return_value={
            "cli.json::1": _ChunkTreeContext(
                chunk_key="cli.json::1",
                exists_in_tree=True,
                matched_node_id="slb",
                tree_level="leaf",
            ),
        },
    ):
        result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["processed"] is True
    assert result["gap_entries"] == 1
    assert result["delegated_gap_entries"] == 1

    rows = [
        json.loads(line)
        for line in (ref / "_owner_decisions_for_farmer.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    deferred = [r for r in rows if r["action"] == "needs_tree_session"]
    assert len(deferred) == 1
    assert deferred[0]["source_evidence"] == "quality_rule_candidate_for_inspector"


def test_pipeline_garbage_chunk_gets_discarded(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()

    chunks = [
        _make_chunk(block_id="g1", page_content="...", section_title="目录"),
        _make_chunk(block_id="ok", page_content="SLB配置\n正常的SLB配置文档内容，描述负载均衡的虚拟服务器创建和健康检查功能。"),
    ]
    gate = [
        {
            "schema_version": "1.0", "source_file": "cli.json",
            "block_id": "g1", "chunk_index": 0, "is_product_knowledge": True,
        },
        {
            "schema_version": "1.0", "source_file": "cli.json",
            "block_id": "ok", "chunk_index": 1, "is_product_knowledge": True,
        },
    ]
    _write_test_gate_and_ref(ref, chunks, gate)

    with patch(
        "INAGENT.data_tools.farm_owner_ingest._build_quality_passed_llm_patches",
        return_value={},
    ), patch(
        "INAGENT.data_tools.farm_owner_ingest._stage2_tree_context_lookup",
        return_value={
            "cli.json::ok": _ChunkTreeContext(
                chunk_key="cli.json::ok",
                exists_in_tree=True,
                matched_node_id="slb",
                tree_level="leaf",
            ),
        },
    ):
        result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["discarded_blocks"] >= 1

    rows = [
        json.loads(line)
        for line in (ref / "_owner_decisions_for_farmer.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    discard_rows = [r for r in rows if r["action"] == "discard"]
    assert len(discard_rows) >= 1
    assert any(r["chunk_meta_patch"].get("owner_excluded") for r in discard_rows)


# ── Reconciliation tests ──────────────────────────────────────────


def _make_decision(
    entity_title: str = "SLB配置",
    action: str = "tree_create_leaf",
    target_node_id: str = "SLB",
    tree_level: str = "leaf",
    source_evidence: str = "test",
) -> dict:
    return {
        "schema_version": "1.0",
        "source_file": "cli.json",
        "entity_title": entity_title,
        "action": action,
        "target_node_id": target_node_id,
        "target_block_id": "1",
        "tree_level": tree_level,
        "fill_fields": {},
        "enrich_fields": {},
        "chunk_meta_patch": {},
        "source_evidence": source_evidence,
        "resolved_value": {},
    }


def test_reconcile_promotes_leaf_to_branch() -> None:
    rules = [
        _make_decision(action="tree_create_leaf", tree_level="leaf", source_evidence="r1"),
        _make_decision(action="tree_create_leaf", tree_level="leaf", source_evidence="r2"),
    ]
    llm = [
        _make_decision(action="tree_create_branch", tree_level="branch", source_evidence="l1"),
    ]
    _reconcile_decisions(rules, llm)
    assert rules[0]["action"] == "tree_create_branch"
    assert rules[0]["tree_level"] == "branch"
    assert "+promoted_from_leaf_to_branch" in rules[0]["source_evidence"]
    assert rules[1]["action"] == "tree_create_branch"
    assert "+promoted_from_leaf_to_branch" in rules[1]["source_evidence"]
    # Branch row stays unchanged
    assert llm[0]["action"] == "tree_create_branch"
    assert "+promoted" not in llm[0]["source_evidence"]


def test_reconcile_leaves_merge_unchanged() -> None:
    rules = [
        _make_decision(action="merge_into_existing", tree_level="leaf", source_evidence="merge1"),
        _make_decision(action="tree_create_leaf", tree_level="leaf", source_evidence="leaf1"),
    ]
    llm: list = []
    _reconcile_decisions(rules, llm)
    # merge row untouched
    assert rules[0]["action"] == "merge_into_existing"
    assert "+promoted" not in rules[0]["source_evidence"]
    # leaf stays leaf (no higher tree_create in the group)
    assert rules[1]["action"] == "tree_create_leaf"
    assert "+promoted" not in rules[1]["source_evidence"]


def test_reconcile_mixed_creates_promote() -> None:
    rules = [
        _make_decision(action="merge_into_existing", tree_level="leaf", source_evidence="m"),
        _make_decision(action="tree_create_leaf", tree_level="leaf", source_evidence="leaf"),
    ]
    llm = [
        _make_decision(action="tree_create_branch", tree_level="branch", source_evidence="branch"),
    ]
    _reconcile_decisions(rules, llm)
    # merge untouched
    assert rules[0]["action"] == "merge_into_existing"
    # leaf promoted to branch
    assert rules[1]["action"] == "tree_create_branch"
    assert rules[1]["tree_level"] == "branch"
    assert "+promoted_from_leaf_to_branch" in rules[1]["source_evidence"]
    # branch stays
    assert llm[0]["action"] == "tree_create_branch"


def test_reconcile_no_conflicts_noop() -> None:
    rules = [
        _make_decision(entity_title="A", action="tree_create_leaf", target_node_id="X"),
        _make_decision(entity_title="B", action="tree_create_branch", target_node_id="Y"),
    ]
    llm = [
        _make_decision(entity_title="C", action="tree_create_trunk", target_node_id="Z"),
    ]
    _reconcile_decisions(rules, llm)
    assert rules[0]["action"] == "tree_create_leaf"
    assert rules[1]["action"] == "tree_create_branch"
    assert llm[0]["action"] == "tree_create_trunk"
    assert all("+promoted" not in str(r["source_evidence"]) for r in rules + llm)


# ── Cross-parent reconciliation (pass 2) ────────────────────────────


def test_reconcile_cross_parent_promotes_leaf_to_branch() -> None:
    """Same entity_title under different parents at different levels.

    '系统健康检查' is branch under '基础网络' but leaf under '基础网络 > 系统健康检查'.
    Pass 2 should promote the leaf to branch.
    """
    rules = [
        _make_decision(
            entity_title="系统健康检查",
            action="tree_create_branch",
            target_node_id="基础网络",
            tree_level="branch",
            source_evidence="r1",
        ),
    ]
    llm = [
        _make_decision(
            entity_title="系统健康检查",
            action="tree_create_leaf",
            target_node_id="基础网络 > 系统健康检查",
            tree_level="leaf",
            source_evidence="l1",
        ),
        _make_decision(
            entity_title="系统健康检查",
            action="tree_create_leaf",
            target_node_id="高可用性",
            tree_level="leaf",
            source_evidence="l2",
        ),
    ]
    _reconcile_decisions(rules, llm)
    # branch row unchanged
    assert rules[0]["action"] == "tree_create_branch"
    assert "+promoted" not in rules[0]["source_evidence"]
    # both leaf rows promoted to branch
    assert llm[0]["action"] == "tree_create_branch"
    assert llm[0]["tree_level"] == "branch"
    assert "+promoted_from_leaf_to_branch" in llm[0]["source_evidence"]
    assert llm[1]["action"] == "tree_create_branch"
    assert "+promoted_from_leaf_to_branch" in llm[1]["source_evidence"]


def test_reconcile_cross_parent_different_entities_no_change() -> None:
    """Different entity_titles under the same parent at different levels — no change."""
    rules = [
        _make_decision(
            entity_title="SSL客户端认证",
            action="tree_create_branch",
            target_node_id="安全",
            tree_level="branch",
            source_evidence="r1",
        ),
    ]
    llm = [
        _make_decision(
            entity_title="静态NAT",
            action="tree_create_leaf",
            target_node_id="安全",
            tree_level="leaf",
            source_evidence="l1",
        ),
    ]
    _reconcile_decisions(rules, llm)
    # Different entities — no cross-parent promotion
    assert rules[0]["action"] == "tree_create_branch"
    assert llm[0]["action"] == "tree_create_leaf"
    assert "+promoted" not in rules[0]["source_evidence"]
    assert "+promoted" not in llm[0]["source_evidence"]
