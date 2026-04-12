# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""MinerU 采购阶段：PDF → Cloud（及可选本地 CLI）→ reference JSON / doc_local_reference。

物理上与 ``auto_convert`` 解耦：原矿 PDF 的结构化、初挂与落盘在本模块完成，
农民侧仅消费已写入 ``reference`` / ``doc_local_reference`` 的块。

``convert_one(..., allow_local_mineru_fallback=...)``：采购入口 ``run_procurement_document_pipeline``
传入 ``allow_local_mineru_fallback=False``（默认，见 ``auto_convert.mineru.cloud.allow_local_fallback``），
即 **仅 MinerU 云端 API**；本地 mineru CLI 需显式允许或 ``max_pages`` 测试模式（云端分支不跑）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from camel.loaders.local_mineru_reader import LocalMinerUReader

from INAGENT.config.project_config import cfg_bool, cfg_int, cfg_str
from INAGENT.data_tools.mineru_cloud_client import (
    DEFAULT_API_BASE as MINERU_CLOUD_DEFAULT_API_BASE,
    mineru_cloud_parse_one_pdf,
    validate_content_list,
)
from INAGENT.utils.env_utils import resolve_env_placeholder

_DATA_TOOLS_DIR = Path(__file__).resolve().parent
_INAGENT_ROOT = _DATA_TOOLS_DIR.parent
DOC_LOCAL_DIR = _INAGENT_ROOT / "knowledge_base"
MINERU_OUTPUT_DIR = DOC_LOCAL_DIR / "mineru_output"
MINERU_BACKUP_DIR = DOC_LOCAL_DIR / "mineru_backup"

logger = logging.getLogger("auto_convert")

USE_DOCKER_VLLM: bool = False
DOCKER_VLLM_URL = (
    cfg_str("auto_convert.vllm.url", "", env="MINERU_VLLM_URL")
    or cfg_str("auto_convert.vllm.url", "", env="VLLM_URL")
    or "http://127.0.0.1:8000"
)


def refresh_mineru_vllm_settings() -> None:
    """Reload vLLM flags after load_inagent_env() so .env / project.yaml apply."""
    global USE_DOCKER_VLLM, DOCKER_VLLM_URL
    USE_DOCKER_VLLM = cfg_bool(
        "auto_convert.vllm.enable",
        False,
        env="AUTO_CONVERT_USE_MINERU_VLLM",
    )
    DOCKER_VLLM_URL = (
        cfg_str("auto_convert.vllm.url", "", env="MINERU_VLLM_URL")
        or cfg_str("auto_convert.vllm.url", "", env="VLLM_URL")
        or "http://127.0.0.1:8000"
    )


def _mineru_cloud_token() -> str:
    return (
        os.environ.get("MINERU_API_TOKEN", "").strip()
        or os.environ.get("MINERU_API_KEY", "").strip()
    )


def _use_mineru_cloud() -> bool:
    """Prefer cloud when credentials exist unless explicitly disabled."""
    from INAGENT.data_tools.auto_convert import _load_project_config

    env_raw = os.environ.get("AUTO_CONVERT_MINERU_CLOUD")
    if env_raw is not None:
        return str(env_raw).strip().lower() in {"1", "true", "yes", "on"}
    raw_cfg = _load_project_config()
    if isinstance(raw_cfg, dict):
        ac = raw_cfg.get("auto_convert")
        if isinstance(ac, dict):
            mineru = ac.get("mineru")
            if isinstance(mineru, dict):
                cloud = mineru.get("cloud")
                if isinstance(cloud, dict) and "enable" in cloud:
                    v = cloud["enable"]
                    if isinstance(v, bool):
                        return v
                    return str(v).strip().lower() in {"1", "true", "yes", "on"}
    return bool(_mineru_cloud_token())


def _mineru_cloud_api_base() -> str:
    u = cfg_str(
        "auto_convert.mineru.cloud.api_base",
        MINERU_CLOUD_DEFAULT_API_BASE,
        env="MINERU_CLOUD_API_BASE",
    ).strip()
    return u or MINERU_CLOUD_DEFAULT_API_BASE


def _mineru_cloud_poll_timeout() -> float:
    return float(
        cfg_int(
            "auto_convert.mineru.cloud.poll_timeout_seconds",
            3600,
            env="MINERU_CLOUD_POLL_TIMEOUT",
        )
    )


def _mineru_cloud_model_version() -> str:
    return cfg_str(
        "auto_convert.mineru.cloud.model_version", "vlm",
        env="MINERU_CLOUD_MODEL_VERSION",
    ).strip() or "vlm"


def _mineru_cloud_is_ocr() -> bool:
    v = os.environ.get("MINERU_CLOUD_IS_OCR", "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _mineru_cloud_no_cache() -> bool:
    v = os.environ.get("MINERU_CLOUD_NO_CACHE", "").strip().lower()
    return v in {"1", "true", "yes", "on"}


_VLLM_AVAILABLE: Optional[bool] = None


def _find_existing_mineru_output(task_id: str) -> Optional[Path]:
    r"""Find an existing MinerU content list JSON for a given task."""
    task_dir = MINERU_OUTPUT_DIR / task_id
    if not task_dir.exists():
        return None

    preferred = [
        task_dir / "hybrid_auto" / f"{task_id}_content_list_v2.json",
        task_dir / "hybrid_auto" / f"{task_id}_content_list.json",
    ]
    for candidate in preferred:
        if candidate.exists():
            return candidate

    import os as _os

    for root, _dirs, files in _os.walk(task_dir):
        for file in files:
            if file.endswith("_content_list_v2.json"):
                return Path(root) / file
    for root, _dirs, files in _os.walk(task_dir):
        for file in files:
            if file.endswith("_content_list.json"):
                return Path(root) / file
    return None


def _can_reuse_mineru_output(pdf: Path, content_list_path: Path) -> bool:
    r"""Return True if existing MinerU output is newer than the input PDF."""
    try:
        return content_list_path.stat().st_mtime >= pdf.stat().st_mtime
    except Exception:
        return False


def _infer_pdf_document_category(pdf: Path) -> str:
    """Infer document_category from PDF filename heuristics.

    Convention (based on project naming):
      - cli*.pdf / *命令*.pdf          → "cli/reference"
      - app*.pdf / *应用*.pdf          → "app/reference"
      - *架构* / *设计* / *design*       → "architecture/design"
      - *spec* / *需求* / *prd*          → "spec/design"
      - default                         → "spec/design"
    """
    name_lower = pdf.stem.lower()
    # CLI pattern: starts with "cli" or contains CLI-specific keywords
    if re.match(r"^cli", name_lower) or any(k in name_lower for k in ("命令参考", "cli_ref", "cliref")):
        return "cli/reference"
    # App/feature guide pattern
    if re.match(r"^app", name_lower) or any(k in name_lower for k in ("应用配置", "app_guide", "feature")):
        return "app/reference"
    # Architecture/design pattern
    if any(k in name_lower for k in ("架构", "design", "arch", "architecture", "设计")):
        return "architecture/design"
    # Spec/requirement pattern
    if any(k in name_lower for k in ("spec", "需求", "prd", "requirement")):
        return "spec/design"
    return "spec/design"


async def convert_one(
    reader: LocalMinerUReader,
    pdf: Path,
    max_pages: Optional[int] = None,
    *,
    allow_local_mineru_fallback: bool = True,
) -> None:
    import INAGENT.data_tools.auto_convert as ac

    json_path, cache_path = ac._target_paths(pdf)

    if ac._can_skip_cached(pdf, json_path, cache_path):
        logger.info("[cache] skip %s, content unchanged.", pdf.name)
        return

    start_time = time.perf_counter()
    if max_pages:
        logger.info("[mineru] processing %s (limited to first %d pages for testing)...", pdf, max_pages)
    else:
        logger.info("[mineru] processing %s ...", pdf)
    
    # Local MinerU CLI extras (optional hybrid-http-client + vLLM)
    local_extra_args: List[str] = []
    use_vllm_backend = USE_DOCKER_VLLM
    if use_vllm_backend:
        global _VLLM_AVAILABLE
        if _VLLM_AVAILABLE is None:
            _VLLM_AVAILABLE = ac._ensure_urls_reachable(
                [ac._build_models_url(DOCKER_VLLM_URL)],
                attempts=ac.NET_RETRY_ATTEMPTS,
                delay=ac.NET_RETRY_DELAY,
                timeout=ac.NET_TIMEOUT,
                label="mineru-vllm",
            )
        if not _VLLM_AVAILABLE:
            use_vllm_backend = False
            logger.warning(
                "[mineru] vLLM server not reachable; falling back to local backend."
            )

    if use_vllm_backend:
        # hybrid-http-client: Local Layout + Remote VLM
        # Check docs: https://opendatalab.github.io/MinerU/zh/quick_start/extension_modules/#clientopenai-hybrid-http-client
        local_extra_args = ["-b", "hybrid-http-client", "-u", DOCKER_VLLM_URL]
        logger.info("[mineru] Using Docker vLLM backend: %s", DOCKER_VLLM_URL)

    # Skip network check for MinerU because modelscope handles connectivity internally
    # and we want to avoid blocking on HuggingFace timeouts.
    # if not ac._ensure_urls_reachable(
    #     urls_to_check,
    #     attempts=ac.NET_RETRY_ATTEMPTS,
    #     delay=ac.NET_RETRY_DELAY,
    #     timeout=ac.NET_TIMEOUT,
    #     label="mineru",
    # ):
    #     raise RuntimeError("Network check failed for MinerU dependencies.")

    # Stream MinerU CLI logs to terminal in real time (with retry)
    # NOTE: task_id equals the input filename stem in LocalMinerUReader.
    task_id = pdf.stem
    existing_output = _find_existing_mineru_output(task_id)
    if existing_output and _can_reuse_mineru_output(pdf, existing_output):
        logger.info(
            "[mineru] reuse existing output for task_id=%s (%s)",
            task_id,
            existing_output,
        )
    else:
        task_id = ""
        cloud_attempted = False
        # 采购管线默认仅云端：allow_local_mineru_fallback=False 时禁止本地 CLI；
        # max_pages 测试模式会跳过云端分支，此时允许本地（与历史行为一致）。
        allow_local_effective = allow_local_mineru_fallback or (max_pages is not None)

        # 1) MinerU 云端 API（有凭证且未限制 max_pages 时优先）
        if max_pages is None and _use_mineru_cloud():
            tok = _mineru_cloud_token()
            if tok:
                cloud_attempted = True
                logger.info("[mineru-cloud] trying cloud API for %s ...", pdf.name)
                cloud_json = await asyncio.to_thread(
                    mineru_cloud_parse_one_pdf,
                    pdf,
                    output_root=MINERU_OUTPUT_DIR,
                    token=tok,
                    api_base=_mineru_cloud_api_base(),
                    model_version=_mineru_cloud_model_version(),
                    is_ocr=_mineru_cloud_is_ocr(),
                    no_cache=_mineru_cloud_no_cache(),
                    poll_timeout=_mineru_cloud_poll_timeout(),
                    logger=logger,
                )
                if cloud_json and validate_content_list(cloud_json):
                    task_id = pdf.stem
                    logger.info(
                        "[mineru-cloud] done -> %s",
                        cloud_json,
                    )
                else:
                    if allow_local_effective:
                        logger.warning(
                            "[mineru-cloud] unavailable or empty output; "
                            "falling back to local MinerU CLI (non-vLLM / default backend)."
                        )
                    else:
                        logger.error(
                            "[mineru-cloud] unavailable or empty output; "
                            "local MinerU CLI disabled (procurement cloud-only)."
                        )

        # 2) 本地 MinerU CLI；若已尝试云端失败，强制不用 vLLM 额外参数
        if not task_id:
            if not allow_local_effective:
                raise RuntimeError(
                    "MinerU：采购管线已配置为仅使用云端 API（禁止本地 mineru CLI 回退）。"
                    "请配置 MINERU_API_TOKEN 或 MINERU_API_KEY、检查云端可用性，"
                    "或设置 MINERU_ALLOW_LOCAL_MINERU_FALLBACK=1 / "
                    "project.yaml auto_convert.mineru.cloud.allow_local_fallback: true。"
                    "（若使用 AUTO_CONVERT_MAX_PAGES_TEST 做部分页测试，会自动允许本地解析。）"
                )
            extra_args = [] if cloud_attempted else local_extra_args
            last_error: Optional[Exception] = None
            for attempt in range(1, ac.NET_RETRY_ATTEMPTS + 1):
                try:
                    task_id = await reader.submit_task(
                        str(pdf),
                        extra_args=extra_args,
                        cwd=str(MINERU_OUTPUT_DIR),
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "[mineru] submit failed (attempt %d/%d): %s",
                        attempt,
                        ac.NET_RETRY_ATTEMPTS,
                        exc,
                    )
                    if attempt < ac.NET_RETRY_ATTEMPTS:
                        await asyncio.sleep(ac.NET_RETRY_DELAY * attempt)
            if not task_id:
                raise RuntimeError(
                    f"MinerU submit failed after retries: {last_error}"
                )
    mineru_elapsed_s = time.perf_counter() - start_time
    logger.info(
        "[mineru] finished task_id=%s in %.2fs",
        task_id,
        mineru_elapsed_s,
    )

    # 1) 读取结构化 JSON（如果没有就跳过，保持 blocks 为空）
    blocks: Dict | List[Dict] | List = []
    try:
        raw_json = await reader.get_task_output(task_id)
        blocks = json.loads(raw_json)
    except FileNotFoundError:
        logger.warning(
            "[warn] no *_content_list.json found for task %s, "
            "metadata blocks will be empty.",
            task_id,
        )
    except json.JSONDecodeError as e:
        logger.warning(
            "[warn] failed to parse JSON output for %s: %s",
            task_id,
            e,
        )

    frontmatter_pages: List[int] = []
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
        # Run synchronous frontmatter filter in thread to avoid blocking loop
        blocks, frontmatter_pages = await asyncio.to_thread(ac._filter_frontmatter_blocks, blocks)
    frontmatter_page_set = set(frontmatter_pages)

    ac.REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    ac.LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 文档分类已移至 knowledge_linker（农民匹配+农场主决策）
    # 此处不再调用 classify_document

    knowledge_blocks: List[Dict[str, object]] = []
    if isinstance(blocks, list):
        section_context_map = ac._build_section_context_map(blocks)
        # 1. 预处理：收集所有有效的 Block 和基础元数据
        valid_items = []
        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            
            # Apply page limit for testing (if max_pages is set)
            if max_pages is not None:
                page_idx = block.get("page_idx")
                if page_idx is not None and page_idx >= max_pages:
                    continue
            
            text = ac._extract_text_from_block(block)
            if not text.strip():
                continue

            section_context = section_context_map.get(idx, {})
            # Preserve image path reference if the block has one
            img_path = block.get("img_path", "")
            base_meta = {
                "source_pdf": str(pdf),
                "source_file": json_path.name,
                "page_idx": block.get("page_idx"),
                "block_type": block.get("type"),
                "block_id": idx,
                "mineru_task_id": task_id,
                "section_title": section_context.get("section_title", ""),
                "parent_section": section_context.get("parent_section", ""),
                "section_path": section_context.get("section_path", ""),
                "product_module": "unknown",
                "document_category": _infer_pdf_document_category(pdf),
                "is_frontmatter": block.get("page_idx") in frontmatter_page_set,
                "frontmatter_confidence": (
                    0.95 if block.get("page_idx") in frontmatter_page_set else 0.0
                ),
            }
            if img_path:
                base_meta["img_path"] = img_path
            valid_items.append((text, base_meta))

        # 2. 并行处理：使用线程池并发调用 _extract_chunk_metadata
        default_workers = 16
        max_workers = ac.cfg_int(
            "auto_convert.parallel.max_workers",
            default_workers,
            env="AUTO_CONVERT_MAX_WORKERS",
        )
        if valid_items:
            total = len(valid_items)
            processed = 0
            start_time = time.perf_counter()
            
            # Helper to run blocking processing in a thread with progress tracking
            # Phase 1: rule-based extraction (fast, no LLM)
            # Phase 2: batch LLM extraction (N chunks per API call)
            def _process_batch_sync(items):
                nonlocal processed
                config = ac._load_project_config()
                meta_rules = config.get("metadata_rules", {})
                llm_config = config.get("llm-aided-config", {}).get("metadata_extraction", {})
                llm_enabled = llm_config.get("enable", False)

                # Phase 1: rule-based extraction for all items (fast)
                rule_results = []
                for text, base in items:
                    clean_text = ac._clean_chunk_text(text)
                    meta_out: Dict[str, object] = {"clean_text": clean_text}
                    section_title = str(base.get("section_title") or "").strip()
                    parent_section = str(base.get("parent_section") or "").strip()
                    if section_title:
                        meta_out["section_title"] = section_title
                    if parent_section:
                        meta_out["parent_section"] = parent_section
                    section_path = base.get("section_path")
                    if section_path:
                        meta_out["section_path"] = section_path
                    lower_text = clean_text.lower()
                    lower_section = f"{section_title} {parent_section}".lower()
                    for intent, keywords in meta_rules.get("intents", {}).items():
                        if any(k.lower() in lower_text for k in keywords):
                            meta_out["intent"] = intent
                            break
                    product_modules_map = ac._merge_product_modules_map(meta_rules.get("product_modules", {}))
                    for module, keywords in product_modules_map.items():
                        if any(k.lower() in lower_text for k in keywords) or any(k.lower() in lower_section for k in keywords):
                            meta_out["product_module"] = module
                            break
                    for proto in ac._match_protocols_word_boundary(
                        meta_rules.get("protocol_types", {}), lower_text
                    ):
                        meta_out.setdefault("protocol_type", [])
                        meta_out["protocol_type"].append(proto)
                    for prefix, keywords in meta_rules.get("command_prefixes", {}).items():
                        if any(k.lower() in lower_text for k in keywords):
                            meta_out["command_prefix"] = prefix
                            break
                    for mode, keywords in meta_rules.get("config_modes", {}).items():
                        if any(k.lower() in lower_text for k in keywords):
                            meta_out["config_mode"] = mode
                            break
                    rule_results.append((clean_text, meta_out))

                processed += len(items)
                elapsed = time.perf_counter() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = total - processed
                eta = remaining / rate if rate > 0 else 0
                logger.info(
                    "[parallel] 规则提取完成: %d/%d (%.1f%%) | %.1f 块/秒 | 剩余: %.1f秒",
                    processed, total, 100.0 * processed / total, rate, eta,
                )

                # Phase 2: batch LLM extraction (if enabled)
                # Skip blocks where rule-based extraction already filled key fields
                if llm_enabled:
                    _KEY_FIELDS = {"product_module", "protocol_type", "intent", "config_mode"}
                    needs_llm = []
                    for ct, m in rule_results:
                        filled = sum(1 for f in _KEY_FIELDS if m.get(f))
                        if filled < 3:  # need LLM if <3 of 4 key fields filled
                            needs_llm.append((ct, m))
                    skipped = len(rule_results) - len(needs_llm)
                    if skipped:
                        logger.info(
                            "[batch-llm] 跳过 %d/%d 块 (规则已覆盖), LLM处理 %d 块",
                            skipped, len(rule_results), len(needs_llm),
                        )
                    batch_size = ac._BATCH_LLM_SIZE
                    llm_batches = []
                    for i in range(0, len(needs_llm), batch_size):
                        llm_batches.append(needs_llm[i:i + batch_size])

                    llm_done = 0
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, 32)) as executor:
                        def _do_batch(batch):
                            ac._apply_llm_metadata_extraction_batch(
                                [(ct, m) for ct, m in batch], llm_config
                            )
                        futures = {executor.submit(_do_batch, b): b for b in llm_batches}
                        for future in concurrent.futures.as_completed(futures):
                            try:
                                future.result()
                            except Exception as e:
                                logger.warning("[batch-llm] batch failed: %s", e)
                            llm_done += len(futures[future])
                            llm_total = len(needs_llm)
                            if llm_done % max(1, min(500, llm_total // 5)) < batch_size or llm_done >= llm_total:
                                logger.info(
                                    "[batch-llm] LLM进度: %d/%d (%.1f%%)",
                                    llm_done, llm_total,
                                    100.0 * llm_done / llm_total if llm_total else 100.0,
                                )

                return [meta_out for _, meta_out in rule_results]

            logger.info(
                "[parallel] 开始提取 metadata: %d 个块 (线程数=%d)...",
                total,
                max_workers,
            )
            
            # Run the batch processing in a separate thread to unblock the event loop
            results = await asyncio.to_thread(_process_batch_sync, valid_items)
            
            elapsed = time.perf_counter() - start_time
            logger.info(
                "[parallel] metadata 提取完成: %d 个块，耗时 %.2f 秒 (平均 %.1f 块/秒)",
                total, elapsed, total / elapsed if elapsed > 0 else 0
            )
            
            # 3. 合并结果
            for (text, base_meta), extracted_meta in zip(valid_items, results):
                base_meta.update(extracted_meta)
                knowledge_blocks.append(
                    {
                        "page_content": text,
                        "metadata": base_meta,
                    }
                )
    
    # 3.5 doc_local_reference passthrough: MinerU 原始块快照（用于4way诊断）
    ac.DOC_LOCAL_REF_DIR.mkdir(parents=True, exist_ok=True)
    doc_ref_path = ac.DOC_LOCAL_REF_DIR / f"{ac._output_stem_for_file(pdf)}.json"
    doc_ref_path.write_text(
        json.dumps(knowledge_blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("[doc_ref] passthrough -> %s (%d blocks)", doc_ref_path.name, len(knowledge_blocks))

    # 4. 增强 metadata：基于功能结构索引添加 scenario_id 和 step_type
    logger.info("[metadata] 开始增强 metadata...")
    ac._enhance_metadata_with_function_index(knowledge_blocks)
    logger.info("[metadata] metadata 增强完成，共处理 %d 个块", len(knowledge_blocks))
    
    # 5. 自动识别文档模块和功能（新增）
    logger.info("[auto-identify] 开始自动识别文档模块和功能...")
    try:
        from INAGENT.data_tools.auto_document_integration import (
            auto_identify_document_module,
            load_function_structure_index,
        )
        
        existing_index = load_function_structure_index()
        document_metadata = auto_identify_document_module(
            pdf_path=pdf,
            content_blocks=knowledge_blocks,
            existing_index=existing_index
        )
        
        logger.info(
            "[auto-identify] 文档识别完成: 模块=%s, 协议=%s, 类型=%s, 置信度=%.2f",
            document_metadata.get("product_modules", []),
            document_metadata.get("protocol_types", []),
            document_metadata.get("document_type", "unknown"),
            document_metadata.get("confidence", 0.0)
        )
        
        if document_metadata.get("new_modules_discovered"):
            logger.info(
                "[auto-identify] 发现新模块: %s",
                document_metadata["new_modules_discovered"]
            )
    except Exception as e:
        logger.warning("[auto-identify] 自动识别失败: %s", e)
        document_metadata = {}

    # 农民匹配 + 农场主决策：将知识块挂载到命令树
    knowledge_blocks, link_stats = ac._run_knowledge_linking(knowledge_blocks, pdf)

    # Fallback tree_position：为没有 tree_position 的非 CLI 块按 section_path 深度赋予默认层级
    ac._assign_fallback_tree_position(knowledge_blocks)

    # 自然树生长：branch 过密时自动晋升为 trunk
    ac._auto_promote_branches(knowledge_blocks)

    # 叶子密集晋升：同模块下 leaf 过多时晋升为 branch/trunk
    ac._auto_promote_dense_leaves(knowledge_blocks)

    json_path.write_text(
        json.dumps(knowledge_blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    cache_payload: Dict[str, object] = {
        "source_pdf": str(pdf),
        "source_pdf_fingerprint": ac._compute_pdf_fingerprint(pdf),
        "config_fingerprint": ac._compute_config_fingerprint(),
        "mineru_task_id": task_id,
        "mineru_output_dir": str(MINERU_OUTPUT_DIR),
        "frontmatter_pages": frontmatter_pages,
        "record_count": len(knowledge_blocks),
        "output_json": str(json_path),
        "link_stats": link_stats,
        "document_metadata": document_metadata,
    }
    cache_path.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info("[ok] saved knowledge blocks -> %s", json_path)
    logger.info("[ok] saved cache -> %s", cache_path)

    # Cleanup large MinerU intermediate files to save disk space
    task_dir = MINERU_OUTPUT_DIR / task_id
    if task_dir.exists():
        _cleanup_mineru_intermediate(task_dir)


def setup_mineru_config() -> None:
    """Check and deploy optimized MinerU configuration from current project."""
    try:
        project_root = _INAGENT_ROOT
        src_config = project_root / "mineru.json"

        MINERU_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        dest_config = MINERU_OUTPUT_DIR / "mineru.json"

        if src_config.exists():
            try:
                content = src_config.read_text(encoding="utf-8")
                config_data = json.loads(content)

                def recursive_resolve(data):
                    if isinstance(data, dict):
                        return {k: recursive_resolve(v) for k, v in data.items()}
                    if isinstance(data, list):
                        return [recursive_resolve(v) for v in data]
                    if isinstance(data, str):
                        return resolve_env_placeholder(data)
                    return data

                resolved_config = recursive_resolve(config_data)

                logger.info("[config] writing resolved config to %s", dest_config)
                dest_config.write_text(
                    json.dumps(resolved_config, ensure_ascii=False, indent=4),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.error("[config] Failed to process config file: %s", e)
                shutil.copy(src_config, dest_config)
        else:
            logger.warning(
                "[config] Warning: Local config %s not found. "
                "Using system defaults.",
                src_config,
            )

    except Exception as e:
        logger.exception("[config] Failed to setup MinerU config: %s", e)


def _cleanup_mineru_intermediate(task_dir: Path) -> None:
    """Backup large MinerU intermediate files to mineru_backup/ after conversion."""
    backup_patterns = ["*_layout.pdf", "*_origin.pdf", "*_middle.json", "*_model.json"]
    moved_bytes = 0
    rel = task_dir.relative_to(MINERU_OUTPUT_DIR)
    backup_task_dir = MINERU_BACKUP_DIR / rel
    for pattern in backup_patterns:
        for f in task_dir.rglob(pattern):
            try:
                f_rel = f.relative_to(task_dir)
                dest = backup_task_dir / f_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                size = f.stat().st_size
                shutil.move(str(f), str(dest))
                moved_bytes += size
                logger.debug(
                    "[backup] moved intermediate: %s -> %s (%.1f MB)",
                    f.name,
                    dest,
                    size / 1048576,
                )
            except Exception as e:
                logger.warning("[backup] failed to move %s: %s", f.name, e)
    if moved_bytes > 0:
        logger.info(
            "[backup] moved %.1f MB of intermediate files from %s to backup",
            moved_bytes / 1048576,
            task_dir.name,
        )


_refresh_mineru_vllm_settings = refresh_mineru_vllm_settings
_setup_mineru_config = setup_mineru_config

__all__ = [
    "DOCKER_VLLM_URL",
    "MINERU_BACKUP_DIR",
    "MINERU_OUTPUT_DIR",
    "USE_DOCKER_VLLM",
    "convert_one",
    "refresh_mineru_vllm_settings",
    "setup_mineru_config",
]
