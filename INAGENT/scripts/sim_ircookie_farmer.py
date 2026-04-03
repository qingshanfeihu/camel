"""模拟 ircookie 知识全流程：采购 → auto_convert → 写骨架 → 写 reference → 反馈农场主

以 cli.pdf ircookie 相关 6 段为例，完整走农民 4 阶段流程。
LLM 批量提取用 mock 代替（离线可重现）。
"""
from __future__ import annotations

import io
import json
import shutil
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.agents.knowledge_farmer_agent import KnowledgeFarmerAgent
from INAGENT.agents.knowledge_procurement_agent import (
    ChunkDecision,
    ProcurementDecision,
)

# ── 原始片段文本（来自 mineru pages 257-258 及 143） ─────────────────────────

_CHUNKS: list[tuple[str, int, str]] = [
    # (label, page_idx, content)
    (
        "slb mode ircookie",
        257,
        """\
slb mode ircookie <ircookie_mode> [group_name] [password]
该命令⽤于配置使⽤Insert Cookie、Rewrite Cookie或Embed Cookie算法时cookie值中后台服务信息的格式。
如不配置该命令，设备默认使⽤后台服务名称对应的ASCII码值作为cookie值。
参数:
  ircookie_mode  必填  可选值: plainname | hexname | ip | enc_name | enc_ip
    ·plainname: cookie值中后台服务信息为后台服务名称对应的ASCII码值
    ·hexname:   cookie值中后台服务信息为后台服务名称对应的十六进制值
    ·ip:        cookie值中后台服务信息为后台服务的IP地址
    ·enc_name:  cookie值中后台服务信息为加密后的后台服务名称的ASCII码值
    ·enc_ip:    cookie值中后台服务信息为加密后的后台服务IP地址
  group_name  可选  类型=string  后台服务组名称
  password    可选  类型=string  加密密码（enc_name/enc_ip 模式）""",
    ),
    (
        "no slb mode ircookie",
        258,
        """\
no slb mode ircookie <group_name>
该命令⽤于删除为指定服务组配置的Insert Cookie、Rewrite Cookie或Embed Cookie算法时cookie值中后台服务信息的格式信息。
参数:
  group_name  必填  类型=string  后台服务组名称""",
    ),
    (
        "show slb mode ircookie",
        258,
        """\
show slb mode ircookie [group_name]
该命令⽤于显⽰指定的后台服务组使⽤Insert Cookie、Rewrite Cookie或Embed Cookie算法时cookie值中后台服务信息的格式配置。
如不指定"group_name"，系统将显⽰所有后台服务组的格式配置。
参数:
  group_name  可选  类型=string  后台服务组名称""",
    ),
    (
        "clear slb mode ircookie",
        258,
        """\
clear slb mode ircookie
该命令⽤于将使⽤Insert Cookie、Rewrite Cookie或Embed Cookie算法时cookie值中后台服务信息的格式恢复为默认设置。""",
    ),
    (
        "slb group method ic  【跨引用 slb mode ircookie】",
        143,
        """\
slb group method <group_name> ic [cookie_name] [path_attribute] [first_choice_method] [threshold_granularity]
该命令⽤于创建⼀个使⽤ic（Insert Cookie）算法的后台服务组。
ic算法将⾸次命中该后台服务组的客⼾端请求转发到根据"⾸次选择算法"选择的后台服务，并使设备在该后台服务返回的响应消息中插⼊Set-Cookie头部以标记后台服务。
当后续携带设备插⼊的cookie的请求命中该服务组时，ic算法将请求持续地转发到同⼀个后台服务。
cookie的值由命令slb mode ircookie确定。
参数:
  group_name   必填  类型=string  后台服务组名称
  cookie_name  可选  类型=string  Set-Cookie头部中cookie的名称""",
    ),
    (
        "附录C: system command override 支持覆盖命令列表",
        672,
        """\
附录三 系统命令覆盖（System Command Override）⽀持覆盖的命令
启用命令覆盖功能后，以下命令可以对同名虚拟服务、后台服务、服务组或策略的已有配置进行直接覆盖（Override），\
原配置将被新配置替代，不会有提示。
支持覆盖的命令列表（部分）：
  slb virtual http <name> <vip> <vport>
  slb virtual https <name> <vip> <vport>
  slb group method <group_name> ic [cookie_name]
  slb mode ircookie <ircookie_mode> [group_name] [password]
  slb mode icookie <insert_mode>
  health check http <name>
注意：
  仅在执行"slb override"命令启用系统命令覆盖后，以上命令才具备覆盖能力。
  默认状态下，系统命令覆盖处于开启状态。""",
    ),
]


def _make_decisions() -> list[ChunkDecision]:
    return [
        ChunkDecision(
            chunk={
                "page_content": content,
                "metadata": {
                    "source_file": "cli.pdf",
                    "document_category": "cli/reference",
                    "section_title": "SLB通用命令/SLB模式",
                    "page_idx": page,
                },
            },
            decision=ProcurementDecision(
                action="accept",
                target_kb="product",
                confidence=0.95,
                reason="采购员：ircookie相关命令文档",
            ),
            source_file="cli.pdf",
            chunk_index=i,
        )
        for i, (label, page, content) in enumerate(_CHUNKS)
    ]


def _print_result(idx: int, label: str, r) -> None:
    w = 72
    print("\n" + "═" * w)
    print(f"  Chunk [{idx}]  {label}")
    print("═" * w)

    meta = r.chunk.get("metadata", {})

    matched = r.matched_node_id or "（无匹配）"
    print(f"\n  matched_node_id : {matched}")
    print(f"  block_id        : {r.block_id}")

    # enriched fields
    if r.enriched_fields:
        print(f"\n  enriched_fields ({len(r.enriched_fields)}):")
        for f in sorted(r.enriched_fields):
            val = meta.get(f)
            print(f"    + {f:<25} = {val!r}")
    else:
        print("\n  enriched_fields : (none)")

    # schema gaps
    if r.schema_gaps:
        print(f"\n  schema_gaps ({len(r.schema_gaps)}):")
        for g in r.schema_gaps:
            if g.gap_type == "conflict":
                print(f"    ⚡ conflict        field={g.field_name!r:<20} "
                      f"skeleton={g.skeleton_value!r}  →  new={g.new_value!r}")
            elif g.gap_type == "overflow":
                near = [m.get("node_id", "?") for m in (g.nearest_matches or [])][:3]
                print(f"    ➕ overflow        field={g.field_name!r:<20} "
                      f"entity={g.entity_title!r}  near={near}")
            elif g.gap_type == "new_entity_attribute":
                print(f"    📋 new_entity_attr col={g.column_name!r:<20} "
                      f"entity={g.entity_title!r}")
            else:
                print(f"    ?  {g.gap_type:<18}  {g.entity_title!r}")
    else:
        print("\n  schema_gaps     : (none)")


_LLM_MOCK_BY_PREFIX: dict[str, dict] = {
    "slb mode ircookie": {
        "product_module": "SLB",
        "protocol_type": ["HTTP"],
        "intent": "configure",
        "config_mode": "cli",
        "command_prefix": "slb",
        "description": "配置 Insert/Rewrite/Embed Cookie 算法中后台服务信息的 cookie 值格式（plainname/hexname/ip/enc_name/enc_ip）",
        "required_keywords": ["ircookie_mode", "group_name", "cookie", "SLB", "Insert Cookie"],
        "scenario_id": "slb_cookie_format",
        "step_type": "config",
        "function_hierarchy": "SLB > Load Balancing > Cookie Persistence > ircookie format",
        "command_structure": {"verb": "slb", "noun": "mode ircookie", "params": ["ircookie_mode", "group_name", "password"]},
        "chunk_type": "single_command",
        "override_commands": [],
    },
    "no slb mode ircookie": {
        "product_module": "SLB",
        "protocol_type": ["HTTP"],
        "intent": "delete",
        "config_mode": "cli",
        "command_prefix": "no",
        "description": "删除指定服务组的 ircookie 格式配置，恢复默认 plainname 格式",
        "required_keywords": ["group_name", "ircookie", "delete", "Cookie Persistence"],
        "scenario_id": "slb_cookie_format",
        "step_type": "delete",
        "function_hierarchy": "SLB > Load Balancing > Cookie Persistence > ircookie format",
        "command_structure": {"verb": "no slb", "noun": "mode ircookie", "params": ["group_name"]},
        "chunk_type": "single_command",
        "override_commands": [],
    },
    "show slb mode ircookie": {
        "product_module": "SLB",
        "protocol_type": ["HTTP"],
        "intent": "query",
        "config_mode": "cli",
        "command_prefix": "show",
        "description": "显示服务组的 ircookie 格式配置，不指定则显示全部",
        "required_keywords": ["group_name", "ircookie", "show", "Cookie"],
        "scenario_id": "slb_cookie_format",
        "step_type": "verify",
        "function_hierarchy": "SLB > Load Balancing > Cookie Persistence > ircookie format",
        "command_structure": {"verb": "show slb", "noun": "mode ircookie", "params": ["group_name"]},
        "chunk_type": "single_command",
        "override_commands": [],
    },
    "clear slb mode ircookie": {
        "product_module": "SLB",
        "protocol_type": ["HTTP"],
        "intent": "reset",
        "config_mode": "cli",
        "command_prefix": "clear",
        "description": "将全局 ircookie 格式恢复为默认设置（plainname）",
        "required_keywords": ["ircookie", "reset", "Cookie", "default"],
        "scenario_id": "slb_cookie_format",
        "step_type": "delete",
        "function_hierarchy": "SLB > Load Balancing > Cookie Persistence > ircookie format",
        "command_structure": {"verb": "clear slb", "noun": "mode ircookie", "params": []},
        "chunk_type": "single_command",
        "override_commands": [],
    },
    "slb group method": {
        "product_module": "SLB",
        "protocol_type": ["HTTP"],
        "intent": "configure",
        "config_mode": "cli",
        "command_prefix": "slb",
        "description": "创建使用 Insert Cookie (ic) 算法的后台服务组，cookie 值格式由 slb mode ircookie 决定",
        "required_keywords": ["group_name", "ic", "cookie_name", "Insert Cookie", "slb mode ircookie"],
        "scenario_id": "slb_group_lb_method",
        "step_type": "config",
        "function_hierarchy": "SLB > Load Balancing > Group Method > Cookie Persistence",
        "command_structure": {"verb": "slb", "noun": "group method", "params": ["group_name", "ic", "cookie_name"]},
        "chunk_type": "single_command",
        "override_commands": [],
    },
    "附录": {
        "product_module": "SLB",
        "protocol_type": [],
        "intent": "explain",
        "config_mode": "cli",
        "command_prefix": "slb",
        "description": "系统命令覆盖（System Command Override）功能说明：支持覆盖的命令列表，含 slb mode ircookie",
        "required_keywords": ["system command override", "支持覆盖", "slb override", "ircookie"],
        "scenario_id": "system_override",
        "step_type": "note",
        "function_hierarchy": "SLB > System > Command Override",
        "command_structure": {},
        "chunk_type": "command_list",
        "override_commands": [
            "slb virtual http",
            "slb virtual https",
            "slb group method",
            "slb mode ircookie",
            "slb mode icookie",
            "health check http",
        ],
    },
}


def _llm_mock(items, config) -> None:  # noqa: ARG001
    """仿 LLM 批量提取：按命令前缀匹配，注入真实字段值。"""
    for clean_text, meta in items:
        matched_data: dict | None = None
        for prefix, data in _LLM_MOCK_BY_PREFIX.items():
            if clean_text.lstrip().startswith(prefix):
                matched_data = data
                break
        if matched_data is None:
            continue
        for k, v in matched_data.items():
            if k not in meta or not meta[k]:
                meta[k] = v


def main() -> None:
    W = 72

    # ── 准备隔离环境（copy knowledge_base.json 到 temp，避免污染真实骨架）────
    from INAGENT.agents.knowledge_farmer_agent import _KB_PATH, _REFERENCE_DIR

    tmp_dir = Path(tempfile.mkdtemp(prefix="sim_ircookie_"))
    tmp_kb = tmp_dir / "knowledge_base.json"
    tmp_ref_dir = tmp_dir / "reference"
    tmp_ref_dir.mkdir()
    tmp_log_dir = tmp_dir / "logs"
    tmp_log_dir.mkdir()
    shutil.copy2(_KB_PATH, tmp_kb)
    print(f"隔离环境: {tmp_dir}")
    print(f"骨架副本: {tmp_kb}\n")

    # ╔════════════════════════════════════════════════════════════════════════╗
    # ║  Stage 1: 采购员决策 — 构造 ChunkDecision                           ║
    # ╚════════════════════════════════════════════════════════════════════════╝
    print("═" * W)
    print("  Stage 1: 采购员 → 6 条 ircookie ChunkDecision")
    print("═" * W)
    decisions = _make_decisions()
    for i, (label, page, _) in enumerate(_CHUNKS):
        print(f"  [{i}] page={page:<4} {label}")
    print()

    # ╔════════════════════════════════════════════════════════════════════════╗
    # ║  Stage 2: 农民 cultivate_batch（auto_convert + 骨架匹配 + gap 检测） ║
    # ╚════════════════════════════════════════════════════════════════════════╝
    print("═" * W)
    print("  Stage 2: 农民 cultivate_batch（auto_convert + 匹配 + gap）")
    print("═" * W)

    farmer = KnowledgeFarmerAgent()

    with patch(
        "INAGENT.data_tools.auto_convert._apply_llm_metadata_extraction_batch",
        side_effect=_llm_mock,
    ):
        results = farmer.cultivate_batch(decisions)

    print(f"\n  返回 {len(results)} 条 FarmResult\n")
    for i, (r, (label, _, _)) in enumerate(zip(results, _CHUNKS)):
        _print_result(i, label, r)

    # ╔════════════════════════════════════════════════════════════════════════╗
    # ║  Stage 3: 写入骨架（update_skeleton）+ 写入 reference               ║
    # ╚════════════════════════════════════════════════════════════════════════╝
    print("\n\n" + "═" * W)
    print("  Stage 3: 写入骨架 + 写入 reference（隔离副本）")
    print("═" * W)

    n_updated = farmer.update_skeleton(results, kb_path=tmp_kb)
    print(f"\n  update_skeleton: {n_updated} 节点被更新")

    ref_counts = farmer.write_to_reference(results, ref_dir=tmp_ref_dir, log_dir=tmp_log_dir)
    for stem, cnt in ref_counts.items():
        print(f"  write_to_reference: {stem}.json += {cnt} chunks")

    # 展示骨架变化
    print(f"\n  {'─' * (W - 4)}")
    print("  骨架变化（ircookie 相关节点）:")
    print(f"  {'─' * (W - 4)}")
    updated_kb = json.loads(tmp_kb.read_text("utf-8"))
    for item in updated_kb:
        nid = item.get("metadata", {}).get("node_id", "")
        if "ircookie" not in nid and nid != "slb_group_method":
            continue
        pc = item.get("page_content", "")
        print(f"\n  ┌─ {nid}")
        for line in pc.split("\n"):
            print(f"  │ {line}")
        print(f"  └{'─' * 40}")

    # 展示 reference/cli.json
    ref_cli = tmp_ref_dir / "cli.json"
    if ref_cli.exists():
        ref_data = json.loads(ref_cli.read_text("utf-8"))
        print(f"\n  reference/cli.json: {len(ref_data)} chunks written")
        for item in ref_data:
            m = item.get("metadata", {})
            print(f"    block_id={m.get('block_id','?'):<20} cmd={m.get('command_prefix','?')}")

    # ╔════════════════════════════════════════════════════════════════════════╗
    # ║  Stage 4: 反馈农场主 — 汇总所有 schema_gaps                         ║
    # ╚════════════════════════════════════════════════════════════════════════╝
    print("\n\n" + "═" * W)
    print("  Stage 4: 反馈农场主 — SchemaGap 工单")
    print("═" * W)

    all_gaps = [g for r in results for g in r.schema_gaps]
    matched = [r for r in results if r.matched_node_id]
    unmatched = [r for r in results if not r.matched_node_id]

    print(f"\n  匹配骨架: {len(matched)}/{len(results)}")
    for r in matched:
        lbl = _CHUNKS[results.index(r)][0]
        print(f"    ✓  {r.matched_node_id:<35}  ← {lbl}")

    if unmatched:
        print(f"\n  未匹配（需农场主裁决）: {len(unmatched)}")
        for r in unmatched:
            lbl = _CHUNKS[results.index(r)][0]
            print(f"    △  block_id={r.block_id}  ← {lbl}")

    print(f"\n  schema_gaps 共 {len(all_gaps)} 条:")
    for gap_type, cnt in sorted(Counter(g.gap_type for g in all_gaps).items()):
        print(f"    {gap_type:<25} × {cnt}")

    # 分类展示 gaps
    overflow_gaps = [g for g in all_gaps if g.gap_type == "overflow"]
    conflict_gaps = [g for g in all_gaps if g.gap_type == "conflict"]
    new_attr_gaps = [g for g in all_gaps if g.gap_type == "new_entity_attribute"]

    if overflow_gaps:
        print(f"\n  ─── overflow（骨架无对应列，需农场主扩列） ───")
        for field_name, cnt in Counter(g.field_name for g in overflow_gaps).most_common():
            entities = sorted({g.entity_title for g in overflow_gaps if g.field_name == field_name})
            print(f"    字段 {field_name!r:<22} × {cnt}  涉及节点: {', '.join(entities[:3])}")

    if conflict_gaps:
        print(f"\n  ─── conflict（字段值冲突，需农场主裁决） ───")
        for g in conflict_gaps:
            print(f"    节点 {g.entity_title!r}  字段 {g.field_name!r}: "
                  f"骨架={g.skeleton_value!r} → 新值={g.new_value!r}")

    if new_attr_gaps:
        print(f"\n  ─── new_entity_attribute（需农场主确认新增列） ───")
        print(f"    new column: 'supports_override' (bool, default=False)")
        for g in new_attr_gaps:
            print(f"    → 标记节点: {g.entity_title!r}")

    # 清理
    print(f"\n  隔离目录: {tmp_dir}")
    print("  （可手动检查后删除）\n")


if __name__ == "__main__":
    main()
