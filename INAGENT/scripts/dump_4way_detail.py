"""
输出 4-way 诊断的详细原始数据到 Markdown 文件，用于人工审查。
包括：原始 KB 块、CommandTree 节点、GraphRAG 实体+关系、向量检索结果。

用法:
    python -m INAGENT.scripts.dump_4way_detail --seed 42
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

INAGENT_ROOT = Path(__file__).resolve().parent.parent
KB_DIR = INAGENT_ROOT / "knowledge_base"
REFERENCE_DIR = KB_DIR / "reference"
DOC_LOCAL_REF = KB_DIR / "doc_local_reference"
CT_PATH = REFERENCE_DIR / "commandtree_base.json"
KB_PATH = REFERENCE_DIR / "knowledge_base.json"
GRAPHRAG_OUTPUT = INAGENT_ROOT / "graphrag_index" / "output"
OUTPUT_MD = KB_DIR / "logs" / "4way_detail_dump.md"


def _load_json(p: Path) -> list:
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _md_code(obj, lang="json"):
    if isinstance(obj, (dict, list)):
        txt = json.dumps(obj, ensure_ascii=False, indent=2)
    else:
        txt = str(obj)
    return f"```{lang}\n{txt}\n```"


def _md_meta(meta: dict) -> str:
    keys = ["section_title", "product_module", "document_category", "source_file",
            "section_path", "command_prefix", "node_id", "block_id", "chunk_id",
            "tree_level", "enhanced_title"]
    lines = []
    for k in keys:
        v = meta.get(k, "")
        if isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
        if v:
            lines.append(f"| `{k}` | {v} |")
    tp = meta.get("tree_position", {})
    if isinstance(tp, dict) and tp:
        lines.append(f"| `tree_position` | {json.dumps(tp, ensure_ascii=False)} |")
    return "| 字段 | 值 |\n|---|---|\n" + "\n".join(lines) if lines else "(无元数据)"


class DetailDumper:
    def __init__(self, seed: int):
        self.seed = seed
        self.lines: List[str] = []
        self.ct_data = _load_json(CT_PATH)
        self.kb = _load_json(KB_PATH)

        self.ent_df = pd.read_parquet(GRAPHRAG_OUTPUT / "entities.parquet")
        self.rel_df = pd.read_parquet(GRAPHRAG_OUTPUT / "relationships.parquet")
        self.tu_df = pd.read_parquet(GRAPHRAG_OUTPUT / "text_units.parquet")
        self.comm_df = pd.read_parquet(GRAPHRAG_OUTPUT / "communities.parquet")

        self._titles_lower = self.ent_df["title"].str.lower().tolist()
        self._types = self.ent_df["type"].str.upper().tolist()
        self._descs = self.ent_df["description"].str.lower().tolist()

        self._hybrid = None

    def w(self, text=""):
        self.lines.append(text)

    def _init_retriever(self):
        if self._hybrid is not None:
            return
        from INAGENT.utils.env_utils import load_inagent_env
        load_inagent_env()
        from INAGENT.workflow_config_generator import initialize_rag_system
        self._hybrid, _, _ = initialize_rag_system()

    def _graphrag_detail(self, query: str, keywords: List[str]) -> Dict[str, Any]:
        matched = []
        match_detail = []
        for kw in keywords:
            kw_lower = kw.lower()
            found = False
            for i, t in enumerate(self._titles_lower):
                if str(t) == kw_lower:
                    eid = self.ent_df.iloc[i]["id"]
                    etype = self._types[i]
                    desc = self.ent_df.iloc[i]["description"][:200] if pd.notna(self.ent_df.iloc[i]["description"]) else ""
                    tu_ids = self.ent_df.iloc[i]["text_unit_ids"]
                    tu_count = len(tu_ids) if tu_ids is not None and hasattr(tu_ids, '__len__') else 0
                    matched.append(str(t))
                    match_detail.append({
                        "keyword": kw, "match_type": "exact_title",
                        "entity_title": str(t), "entity_type": etype,
                        "entity_id": eid[:12] + "...",
                        "description": desc[:150],
                        "text_unit_count": tu_count,
                    })
                    found = True
                    break
            if found:
                continue
            for i, t in enumerate(self._titles_lower):
                if self._types[i] in ("SECTION", "DOCUMENT"):
                    continue
                if kw_lower in str(t) or (len(kw_lower) >= 3 and any(kw_lower[j:j+2] in str(t) for j in range(len(kw_lower)-1) if '\u4e00' <= kw_lower[j] <= '\u9fff')):
                    eid = self.ent_df.iloc[i]["id"]
                    etype = self._types[i]
                    desc = self.ent_df.iloc[i]["description"][:200] if pd.notna(self.ent_df.iloc[i]["description"]) else ""
                    matched.append(str(t))
                    match_detail.append({
                        "keyword": kw, "match_type": "cjk_overlap",
                        "entity_title": str(t), "entity_type": etype,
                        "entity_id": eid[:12] + "...",
                        "description": desc[:150],
                    })
                    found = True
                    break
            if not found:
                match_detail.append({
                    "keyword": kw, "match_type": "NO_MATCH",
                })

        rels_detail = []
        for ent in matched[:3]:
            src_col = "source" if "source" in self.rel_df.columns else "source_id"
            tgt_col = "target" if "target" in self.rel_df.columns else "target_id"
            mask = (self.rel_df[src_col].str.lower() == ent) | (self.rel_df[tgt_col].str.lower() == ent)
            matched_rels = self.rel_df[mask]
            if len(matched_rels) > 5:
                matched_rels = matched_rels.sample(n=5, random_state=hash(ent) & 0x7FFFFFFF)
            for _, row in matched_rels.iterrows():
                rels_detail.append({
                    "source": row[src_col],
                    "target": row[tgt_col],
                    "description": str(row.get("description", ""))[:100],
                })

        return {
            "keywords": keywords,
            "entity_matches": match_detail,
            "relationships_sample": rels_detail,
            "total_rel_count": sum(
                int(((self.rel_df["source"].str.lower() == ent) | (self.rel_df["target"].str.lower() == ent)).sum())
                for ent in matched[:3]
            ),
        }

    def _vector_detail(self, query: str) -> Dict[str, Any]:
        self._init_retriever()
        META_PREFIX = "INAGENT_META_JSON:"
        try:
            raw = self._hybrid.query(query, top_k=5, return_detailed_info=True)
            items = raw.get("Retrieved Context", []) if isinstance(raw, dict) else raw
            if not items:
                return {"hit": False, "results": []}
            results = []
            for i, h in enumerate(items[:5]):
                if not isinstance(h, dict):
                    continue
                text = str(h.get("text", ""))
                score = float(h.get("rrf_score", 0) or h.get("score", 0))
                meta = {}
                if text.startswith(META_PREFIX):
                    nl = text.find("\n")
                    if nl > 0:
                        try:
                            meta = json.loads(text[len(META_PREFIX):nl])
                            text = text[nl + 1:]
                        except Exception:
                            pass
                results.append({
                    "rank": i + 1,
                    "score": round(score, 4),
                    "meta": {k: v for k, v in meta.items() if k in (
                        "section_title", "product_module", "document_category",
                        "source_file", "command_prefix", "tree_level", "node_id",
                    )},
                    "text_preview": text[:200],
                })
            return {"hit": results[0]["score"] >= 0.008 if results else False, "results": results}
        except Exception as exc:
            return {"hit": False, "error": str(exc)}

    def _ct_lookup(self, query: str) -> Dict[str, Any]:
        query_lower = query.lower().strip()
        for entry in self.ct_data:
            m = entry.get("metadata", {})
            prefix = m.get("command_prefix", "").lower()
            if prefix == query_lower:
                return {
                    "found": True,
                    "command_prefix": m.get("command_prefix", ""),
                    "product_module": m.get("product_module", ""),
                    "tree_level": m.get("tree_position", {}).get("tree_level", "?"),
                    "content_len": len(entry.get("page_content", "")),
                    "content_preview": entry.get("page_content", "")[:200],
                }
        return {"found": False}

    def _kb_lookup(self, section_title: str, source_file: str = "") -> List[Dict]:
        results = []
        sf_stem = Path(source_file).stem if source_file else ""
        for entry in self.kb:
            m = entry.get("metadata", {})
            if m.get("section_title", "").strip() == section_title.strip():
                if sf_stem and Path(m.get("source_file", "")).stem != sf_stem:
                    continue
                results.append({
                    "node_id": m.get("node_id", ""),
                    "product_module": m.get("product_module", ""),
                    "document_category": m.get("document_category", ""),
                    "source_file": m.get("source_file", ""),
                    "tree_level": m.get("tree_position", {}).get("tree_level", "")
                                  if isinstance(m.get("tree_position"), dict) else "",
                    "content_len": len(entry.get("page_content", "")),
                })
        return results

    def dump_entry(self, idx: int, entry: dict, track: str):
        meta = entry.get("metadata", {})
        section = meta.get("section_title", "").strip()
        module = meta.get("product_module", "")
        source_file = meta.get("source_file", "")
        path = meta.get("section_path", "")
        content = entry.get("page_content", "")
        cmd_prefix = meta.get("command_prefix", "")

        self.w(f"## {track} #{idx}: {section}")
        self.w()

        self.w("### 1. 原始数据 (doc_local_reference 块)")
        self.w()
        self.w(_md_meta(meta))
        self.w()
        self.w(f"**内容长度**: {len(content)} 字符")
        self.w()
        self.w(f"**内容预览** (前300字):")
        self.w(f"```")
        self.w(content[:300])
        self.w(f"```")
        self.w()

        self.w("### 2. knowledge_base.json 中匹配条目")
        self.w()
        kb_hits = self._kb_lookup(section, source_file)
        if kb_hits:
            self.w(f"找到 {len(kb_hits)} 条 KB 匹配:")
            self.w(_md_code(kb_hits))
        else:
            self.w("**未找到 KB 匹配**")
        self.w()

        self.w("### 3. CommandTree 节点")
        self.w()
        ct_query = cmd_prefix if cmd_prefix else section
        ct_result = self._ct_lookup(ct_query)
        self.w(f"查询: `{ct_query}`")
        self.w(_md_code(ct_result))
        self.w()

        if track == "CLI":
            query = section + " " + module
            kws = [section] + ([module] if module else [])
        else:
            query = section + " " + (module or "") + " " + (path or "")
            kws = [section] + ([module] if module else [])

        self.w("### 4. GraphRAG 检索")
        self.w()
        self.w(f"**查询**: `{query.strip()}`")
        self.w(f"**关键词**: `{kws}`")
        self.w()
        gr = self._graphrag_detail(query, kws)
        self.w("#### 实体匹配:")
        self.w(_md_code(gr["entity_matches"]))
        self.w()
        self.w(f"**关系总数**: {gr['total_rel_count']}")
        self.w()
        if gr["relationships_sample"]:
            self.w("#### 关系样本 (每实体前5条):")
            self.w(_md_code(gr["relationships_sample"]))
        self.w()

        self.w("### 5. 向量检索")
        self.w()
        self.w(f"**查询**: `{query.strip()}`")
        self.w()
        vec = self._vector_detail(query.strip())
        self.w(f"**命中**: {'是' if vec.get('hit') else '否'}")
        self.w()
        if vec.get("results"):
            self.w("#### Top-5 返回:")
            for r in vec["results"]:
                self.w(f"**Rank {r['rank']}** (score={r['score']})")
                if r.get("meta"):
                    self.w(_md_code(r["meta"]))
                self.w(f"```\n{r['text_preview']}\n```")
                self.w()
        if vec.get("error"):
            self.w(f"**错误**: {vec['error']}")
        self.w()
        self.w("---")
        self.w()

    def run(self, cli_count=10, app_count=5, arch_count=5, haslb_count=5):
        self.w("# 4-Way 详细诊断数据转储")
        self.w()
        self.w(f"- Seed: {self.seed}")
        self.w(f"- GraphRAG 实体数: {len(self.ent_df)}")
        self.w(f"- GraphRAG 关系数: {len(self.rel_df)}")
        self.w(f"- GraphRAG 文本单元数: {len(self.tu_df)}")
        self.w(f"- GraphRAG 社区数: {len(self.comm_df)}")
        self.w(f"- KB 总块数: {len(self.kb)}")
        self.w(f"- CommandTree 节点数: {len(self.ct_data)}")
        self.w()

        type_counts = self.ent_df["type"].value_counts().to_dict()
        self.w("### 实体类型分布")
        self.w(_md_code(type_counts))
        self.w()

        rel_types = defaultdict(int)
        if "description" in self.rel_df.columns:
            for desc in self.rel_df["description"].tolist():
                d = str(desc)
                if "属于模块" in d:
                    rel_types["BELONGS_TO"] += 1
                elif "来自文档" in d:
                    rel_types["FROM_DOCUMENT"] += 1
                elif "包含子章节" in d:
                    rel_types["PARENT_OF"] += 1
                elif "引用命令" in d:
                    rel_types["REFERENCES_COMMAND"] += 1
                else:
                    rel_types["CLI_EDGE"] += 1
        self.w("### 关系类型分布")
        self.w(_md_code(dict(rel_types)))
        self.w()
        self.w("---")
        self.w()

        tracks = [
            ("CLI", DOC_LOCAL_REF / "cli_1-82.json", cli_count),
            ("APP", DOC_LOCAL_REF / "app_1-40.json", app_count),
            ("ARCH", DOC_LOCAL_REF / "ustack设计架构V3.json", arch_count),
            ("HA-SLB", DOC_LOCAL_REF / "app_65-72.json", haslb_count),
        ]

        for track_name, ref_path, count in tracks:
            if not ref_path.exists():
                self.w(f"## {track_name}: 跳过 ({ref_path.name} 不存在)")
                continue
            blocks = _load_json(ref_path)
            self.w(f"# {track_name} Track ({len(blocks)} 源块, 采样 {count})")
            self.w()

            candidates = []
            seen = set()
            for e in blocks:
                if len(e.get("page_content", "")) <= 80:
                    continue
                m = e.get("metadata", {})
                title = m.get("section_title", "").strip()
                if not title or title == "⽬录" or title in seen:
                    continue
                seen.add(title)
                if track_name == "CLI":
                    mod = m.get("product_module", "unknown")
                    if mod == "unknown":
                        continue
                    cp = m.get("command_prefix", "").strip()
                    if not cp:
                        continue
                candidates.append(e)

            seed_offset = 0 if track_name != "HA-SLB" else 1
            random.seed(self.seed + seed_offset)
            random.shuffle(candidates)
            sample = candidates[:min(count, len(candidates))]

            for i, entry in enumerate(sample, 1):
                print(f"  Processing {track_name} #{i}/{len(sample)}: {entry.get('metadata',{}).get('section_title','')[:30]}...")
                self.dump_entry(i, entry, track_name)

        OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_MD.write_text("\n".join(self.lines), encoding="utf-8")
        print(f"\n输出: {OUTPUT_MD}")
        print(f"总行数: {len(self.lines)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cli-count", type=int, default=10)
    ap.add_argument("--app-count", type=int, default=5)
    ap.add_argument("--arch-count", type=int, default=5)
    ap.add_argument("--haslb-count", type=int, default=5)
    args = ap.parse_args()

    dumper = DetailDumper(seed=args.seed)
    dumper.run(args.cli_count, args.app_count, args.arch_count, args.haslb_count)


if __name__ == "__main__":
    main()
