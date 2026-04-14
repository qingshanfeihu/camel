"""采购管线编排与块级工具。

`run_procurement_document_pipeline`：扫描 knowledge_base → MinerU / Office / TXT
→ reference + merge；MinerU 细节在 `mineru_procurement`。

农民 / 农场主仍从此模块导入 `_extract_chunk_metadata`、`_extract_text_from_block`、
`_apply_llm_metadata_extraction_batch` 等。
"""
import asyncio
import hashlib
import html as html_stdlib
import json
import logging
import math
import os
import random
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.request

try:
    import json_repair
except ImportError:
    json_repair = None
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from camel.loaders.local_mineru_reader import LocalMinerUReader
from camel.logger import set_log_file, set_log_level
from INAGENT.config.project_config import cfg_bool, cfg_float, cfg_int, cfg_str
from INAGENT.data_tools.spec_parser import parse_spec_document
from INAGENT.data_tools.testlist_parser import parse_test_list
from INAGENT.utils.env_utils import load_inagent_env, resolve_env_placeholder

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency at runtime
    OpenAI = None

BASE_DIR = Path(__file__).parent
# DOC_LOCAL_DIR 指向 INAGENT/knowledge_base（与 manage_database.py 一致）
DOC_LOCAL_DIR = BASE_DIR.parent / "knowledge_base"
REFERENCE_DIR = DOC_LOCAL_DIR / "reference"
DOC_LOCAL_REF_DIR = DOC_LOCAL_DIR / "doc_local_reference"

LOG_DIR = DOC_LOCAL_DIR / "logs"
LOG_FILE = LOG_DIR / "auto_convert.log"

logger = logging.getLogger("auto_convert")

AUTO_CONVERT_SCHEMA_VERSION = "2026-06-30.1"


def _match_protocols_word_boundary(
    protocol_map: Dict[str, list], text: str
) -> List[str]:
    lower_text = text.lower()
    found: List[str] = []
    for proto, keywords in protocol_map.items():
        for k in keywords:
            if re.search(rf"\b{re.escape(k.lower())}\b", lower_text):
                found.append(proto)
                break
    return found


def _remove_section_number(title: str) -> str:
    """去除章节标题开头的编号前缀。

    支持格式：
    - 数字编号（含可选末尾点）: "4. " "4.1 " "1.2.3. "
    - 大写字母附录编号: "A. " "B.1 "
    - 括号数字: "(1) " "1) "
    - 中文章节: "第4章 " "第四章 " "第4节 "
    """
    if not title:
        return title
    # 数字编号
    title = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", title)
    # 大写字母附录编号（如 "A. Overview"）
    title = re.sub(r"^[A-Z](?:\.\d+)*\.?\s+", "", title)
    # 括号数字: "(1) " 或 "1) "
    title = re.sub(r"^\(\d+\)\s+", "", title)
    title = re.sub(r"^\d+\)\s+", "", title)
    # 中文章节（支持阿拉伯数字和中文数字）
    title = re.sub(r"^第[0-9一二三四五六七八九十百千]+[章节篇]\s*", "", title)
    return title.strip()


def _is_plausible_heading(text: str) -> bool:
    """Check if text looks like a section heading rather than body text.

    Rejects numbered list items and long sentences that happen to start
    with a digit pattern matching ``_infer_section_level_from_heading``.
    """
    if not text:
        return False
    if len(text) > 80:
        return False
    stripped = text.rstrip()
    if stripped.endswith(('\u3002', '\uff01', '\uff1f', '\uff1b', '\uff1a',
                          '.', '!', '?', ';', ':')):
        return False
    digits = sum(1 for c in text if c.isdigit())
    if len(text) > 10 and digits / len(text) > 0.3:
        return False
    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in text)
    if not has_cjk and len(text) > 20:
        return False
    return True


def _sanitize_section_path(path: str) -> str:
    """Clean section_path by dropping segments that are clearly not headings."""
    if not path:
        return path
    segments = path.split(' > ')
    valid = [s for s in segments if _is_plausible_heading(s)]
    return ' > '.join(valid)


def _infer_section_level_from_heading(title: str) -> Optional[int]:
    """Infer section level from numbered headings.

    Examples:
        "11.3.1. HTTP配置" -> level 3
        "11. 服务器负载均衡" -> level 1
        "第11章 服务器负载均衡" -> level 1
    """
    if not title:
        return None
    number_match = re.match(r"^(\d+(?:\.\d+)*)\s*[.．]?\s+", title)
    if number_match:
        return len(number_match.group(1).split("."))
    chapter_match = re.match(r"^第\d+[章节]\s+", title)
    if chapter_match:
        return 1
    return None


# vLLM probing timeouts
VLLM_MODELS_TIMEOUT = cfg_float(
    "auto_convert.vllm.models_timeout_seconds",
    8.0,
    env="VLLM_MODELS_TIMEOUT",
)
VLLM_INFER_TIMEOUT = cfg_float(
    "auto_convert.vllm.infer_timeout_seconds",
    30.0,
    env="VLLM_INFER_TIMEOUT",
)


_project_config_cache: Optional[Dict] = None


def _load_project_config() -> Dict:
    global _project_config_cache
    if _project_config_cache is not None:
        return _project_config_cache
    load_inagent_env()
    config_path = cfg_str(
        "auto_convert.mineru_tools_config_json",
        "",
        env="MINERU_TOOLS_CONFIG_JSON",
    )
    candidate_paths = []

    if config_path:
        candidate_paths.append(Path(config_path))
    candidate_paths.append(BASE_DIR.parent / "mineru.json")
    candidate_paths.append(Path.cwd() / "mineru.json")
    candidate_paths.append(Path.home() / "magic-pdf.json")

    for path in candidate_paths:
        if path.exists():
            try:
                config_data = json.loads(path.read_text(encoding="utf-8"))

                def recursive_resolve(data):
                    if isinstance(data, dict):
                        return {k: recursive_resolve(v) for k, v in data.items()}
                    if isinstance(data, list):
                        return [recursive_resolve(v) for v in data]
                    if isinstance(data, str):
                        return resolve_env_placeholder(data)
                    return data

                resolved = recursive_resolve(config_data)
                result = resolved if isinstance(resolved, dict) else {}
                _project_config_cache = result
                return result
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse config %s: %s", path, exc)
                _project_config_cache = {}
                return {}
    _project_config_cache = {}
    return {}


def _load_product_modules_registry() -> Dict[str, Any]:
    """Load product module registry from knowledge_base.

    Supports JSON/YAML. Missing file will return empty dict.
    """
    registry_paths = [
        DOC_LOCAL_DIR / "product_modules_registry.yaml",
        DOC_LOCAL_DIR / "product_modules_registry.yml",
        DOC_LOCAL_DIR / "product_modules_registry.json",
    ]
    for path in registry_paths:
        if not path.exists():
            continue
        try:
            if path.suffix in {".yaml", ".yml"}:
                try:
                    import yaml  # type: ignore
                except Exception:
                    logger.warning(
                        "product_modules_registry.yaml detected but PyYAML is not installed."
                    )
                    return {}
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("Failed to load product module registry %s: %s", path, exc)
            return {}
    return {}


def _merge_product_modules_map(base_map: Dict[str, List[str]]) -> Dict[str, List[str]]:
    registry = _load_product_modules_registry()
    modules = registry.get("modules", {}) if isinstance(registry, dict) else {}
    merged = {**base_map}
    if isinstance(modules, dict):
        for module_id, module_info in modules.items():
            keywords = []
            if isinstance(module_info, dict):
                keywords = module_info.get("keywords", [])
            if keywords:
                merged.setdefault(module_id, [])
                merged[module_id].extend(list(keywords))
    elif isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            module_id = module.get("id") or module.get("display_name")
            keywords = module.get("keywords", [])
            if module_id and keywords:
                merged.setdefault(str(module_id), [])
                merged[str(module_id)].extend(list(keywords))
    # Normalize duplicates
    for key, values in merged.items():
        seen = set()
        merged[key] = [v for v in values if not (v in seen or seen.add(v))]
    return merged


# MinerU PDF 阶段（convert_one、输出目录、云端/vLLM 配置）见 ``mineru_procurement``。
from INAGENT.data_tools import mineru_procurement as _mp
from INAGENT.data_tools.mineru_procurement import (
    MINERU_BACKUP_DIR,
    MINERU_OUTPUT_DIR,
    convert_one,
)
from INAGENT.data_tools.mineru_procurement import (
    refresh_mineru_vllm_settings as _refresh_mineru_vllm_settings,
)
from INAGENT.data_tools.mineru_procurement import (
    setup_mineru_config as _setup_mineru_config,
)

# Cached availability checks
_LLM_METADATA_AVAILABLE: Optional[bool] = None
_LLM_GATEWAY_AVAILABLE: Optional[bool] = None

# LLM Gateway 模型配置，优先从 LLM_GATEWAY_CHAT_MODEL 环境变量读取
LLM_GATEWAY_MODEL = (
    cfg_str("llm.gateway.chat_model", "", env="LLM_GATEWAY_CHAT_MODEL").strip()
    or cfg_str("llm.siliconflow.chat_model", "mineru-vlm", env="SILICONFLOW_CHAT_MODEL")
)
SILICONFLOW_MODEL = LLM_GATEWAY_MODEL  # compat alias

# 简单的 cache：logs 下存在同名 cache.json 就视为已转换

NET_RETRY_ATTEMPTS = cfg_int(
    "auto_convert.network.retry_attempts",
    3,
    env="AUTO_CONVERT_NET_RETRIES",
)
NET_RETRY_DELAY = cfg_float(
    "auto_convert.network.retry_delay_seconds",
    5.0,
    env="AUTO_CONVERT_NET_RETRY_DELAY",
)
NET_TIMEOUT = cfg_float(
    "auto_convert.network.timeout_seconds",
    8.0,
    env="AUTO_CONVERT_NET_TIMEOUT",
)


def _llm_temperature() -> float:
    """获取 LLM 温度参数，默认 0.7。"""
    raw = cfg_str("llm.siliconflow.temperature", "0.7", env="SILICONFLOW_TEMPERATURE")
    try:
        value = float(raw)
    except Exception:
        value = 0.7
    return max(0.0, min(2.0, value))


_siliconflow_temperature = _llm_temperature  # compat alias
_qianfan_temperature = _llm_temperature  # compat alias


def _get_llm_gateway_runtime() -> Dict[str, object]:
    """获取 LLM Gateway 运行时设置。"""
    try:
        from INAGENT.utils.llm_config import get_siliconflow_config
        config = get_siliconflow_config()

        base_url = config.get("base_url", "")
        if not base_url or not str(base_url).strip():
            raise ValueError(
                "LLM_GATEWAY_BASE_URL 未配置，auto_convert 禁止直连 API。"
            )

        return {
            "api_key": config.get("api_key", ""),
            "base_url": base_url,
            "model": LLM_GATEWAY_MODEL,
            "timeout": config.get("timeout", 60),
        }
    except Exception as e:
        logger.warning("从统一配置获取运行时设置失败: %s", e)
        raise ValueError(
            "LLM_GATEWAY_BASE_URL 未配置，auto_convert 禁止直连 API。"
        )


_get_siliconflow_runtime = _get_llm_gateway_runtime  # compat alias
_get_qianfan_runtime = _get_llm_gateway_runtime  # compat alias


def _call_llm_with_retry_and_fallback(
    client: Any,
    base_url: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.0,
    response_format: Optional[Dict[str, str]] = None,
    stream: bool = False,
    max_tokens: Optional[int] = None,
    max_retries: int = 6,
    initial_delay: float = 1.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    allow_fallback: bool = True,
    fallback_config: Optional[Dict[str, Any]] = None,
) -> Any:
    """LLM Gateway API 调用，含指数退避重试。allow_fallback/fallback_config 保留签名兼容性但已忽略。"""

    num_retries = 0
    delay = initial_delay

    while True:
        try:
            request_kwargs: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            if response_format:
                request_kwargs["response_format"] = response_format
            if stream:
                request_kwargs["stream"] = stream
            if max_tokens is not None:
                request_kwargs["max_tokens"] = max_tokens
            return client.chat.completions.create(**request_kwargs)
        except Exception as e:
            num_retries += 1
            if num_retries > max_retries:
                logger.error(
                    "[LLM] 达到最大重试次数 (%d)，model=%s: %s", max_retries, model, e
                )
                raise
            delay_with_jitter = delay * (1 + random.random()) if jitter else delay
            logger.warning(
                "[LLM] 调用失败，第 %d/%d 次重试，等待 %.2f 秒 (model=%s): %s",
                num_retries, max_retries, delay_with_jitter, model, e,
            )
            time.sleep(delay_with_jitter)
            delay *= exponential_base


def _probe_llm_gateway_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> Tuple[bool, str]:
    """Probe LLM Gateway OpenAI-compatible chat completions.

    Returns:
        (ok, message)
    """
    if not base_url:
        return False, "LLM Gateway base_url 未配置"
    if not api_key:
        return False, "LLM Gateway api_key 未配置"
    if not model:
        return False, "LLM Gateway model 未配置"

    if OpenAI is not None:
        try:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            response = _call_llm_with_retry_and_fallback(
                client=client,
                base_url=base_url,
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                temperature=_llm_temperature(),
                max_tokens=1,
                max_retries=3,
            )
            if response and response.choices:
                return True, "LLM Gateway 预检成功"
            return False, "LLM Gateway 预检返回空响应"
        except Exception as exc:
            return False, f"LLM Gateway 预检失败: {exc}"

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": _llm_temperature(),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            return True, f"LLM Gateway 预检成功 (HTTP {status})"
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return False, f"LLM Gateway HTTP {exc.code}: {err_body or str(exc)}"
    except Exception as exc:
        return False, f"LLM Gateway 预检失败: {exc!s}"


_probe_siliconflow_chat = _probe_llm_gateway_chat  # compat alias
_probe_qianfan_chat = _probe_llm_gateway_chat  # compat alias


def _is_under_doc_local(path: Path) -> bool:
    try:
        path.resolve().relative_to(DOC_LOCAL_DIR.resolve())
        return True
    except Exception:
        return False


def _output_stem_for_file(file_path: Path) -> str:
    """为输出 JSON 生成稳定 stem；外部文件附加路径哈希避免同名冲突。"""
    if _is_under_doc_local(file_path):
        return file_path.stem
    path_hash = hashlib.md5(str(file_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{file_path.stem}_{path_hash}"


def _source_file_label(file_path: Path) -> str:
    """标准化 source_file 标识，外部文件保留可追溯标签。"""
    if _is_under_doc_local(file_path):
        return file_path.name
    path_hash = hashlib.md5(str(file_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{file_path.name} [external:{path_hash}]"


def _ensure_procurement_base_fields(
    knowledge_blocks: List[Dict[str, Any]],
    source_path: Path,
    source_label: str,
) -> None:
    """统一补齐采购阶段必须落盘的基础结构字段。"""
    default_title = source_path.stem
    source_path_str = str(source_path)

    for block in knowledge_blocks:
        if not isinstance(block, dict):
            continue
        meta = block.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
            block["metadata"] = meta

        meta["source_file"] = source_label
        meta["source_pdf"] = source_path_str

        section_title = str(meta.get("section_title") or "").strip()
        if not section_title:
            meta["section_title"] = default_title


def _collect_extra_input_paths() -> List[Path]:
    """收集额外知识输入路径（文件或目录），支持逗号/分号分隔。"""
    raw = cfg_str(
        "auto_convert.input.extra_paths",
        "",
        env="AUTO_CONVERT_EXTRA_PATHS",
    ).strip()
    if not raw:
        return []
    tokens = [p.strip() for p in re.split(r"[,\n;]+", raw) if p.strip()]
    paths: List[Path] = []
    for token in tokens:
        p = Path(token)
        if not p.exists():
            logger.warning("[input-extra] 路径不存在，忽略: %s", token)
            continue
        paths.append(p)
    return paths


def _iter_files_by_ext(exts: Set[str]) -> List[Path]:
    """遍历 knowledge_base + 额外路径中的指定后缀文件。"""
    files: List[Path] = []
    seen: Set[str] = set()
    excluded_dirs = {
        DOC_LOCAL_DIR / "mineru_output",
        DOC_LOCAL_DIR / "mineru_backup",
        DOC_LOCAL_DIR / "logs",
        DOC_LOCAL_DIR / "backup",
        DOC_LOCAL_DIR / "backups",
    }

    def _maybe_add(path: Path) -> None:
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        files.append(path)

    for ext in exts:
        for path in DOC_LOCAL_DIR.rglob(f"*{ext}"):
            if REFERENCE_DIR in path.parents:
                continue
            if any(excluded_dir in path.parents for excluded_dir in excluded_dirs):
                continue
            if ext == ".pdf" and path.name.endswith("_layout.pdf"):
                continue
            _maybe_add(path)

    for base in _collect_extra_input_paths():
        if base.is_file():
            if base.suffix.lower() in exts:
                _maybe_add(base)
            continue
        for ext in exts:
            for path in base.rglob(f"*{ext}"):
                if ext == ".pdf" and path.name.endswith("_layout.pdf"):
                    continue
                _maybe_add(path)
    return files

def _iter_pdf_files() -> List[Path]:
    return _iter_files_by_ext({".pdf"})


# ============================================================
# Office 文档（docx/xlsx/doc/xls）处理
# ============================================================
OFFICE_EXTENSIONS = {".docx", ".doc", ".xlsx", ".xls"}
TEXT_EXTENSIONS = {".txt"}


def _iter_office_files() -> List[Path]:
    """遍历 knowledge_base 目录下的 Office 文档"""
    return _iter_files_by_ext(OFFICE_EXTENSIONS)


def _iter_text_files() -> List[Path]:
    """遍历 knowledge_base 目录下的 TXT 文档"""
    return _iter_files_by_ext(TEXT_EXTENSIONS)


def convert_office_file(file_path: Path) -> None:
    """Office（表格走 testlist，其余走 spec_parser）→ reference/{stem}.json。"""
    stem = _output_stem_for_file(file_path)
    json_path = REFERENCE_DIR / f"{stem}.json"
    source_label = json_path.name
    cache_path = LOG_DIR / f"{stem}.cache.json"
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if _can_skip_office_cached(file_path, json_path, cache_path):
        logger.info("[office-cache] 跳过 %s，内容未变更", file_path.name)
        return

    start_time = time.perf_counter()
    logger.info("[office] 开始处理: %s", file_path.name)

    work_path = file_path
    knowledge_blocks: List[Dict[str, Any]] = []
    doc_type = "spec/design"
    procurement_doc_category = "unknown"
    try:
        # 1. 预览（转换后 work_path 已为 docx，可用 MarkItDown）
        content_preview = ""
        try:
            from camel.loaders.markitdown import MarkItDownLoader
            loader = MarkItDownLoader()
            full_text = loader.convert_file(str(work_path))
            content_preview = full_text[:2000] if full_text else ""
        except Exception as e:
            logger.warning("[office] 预览读取失败 %s: %s", work_path.name, e)

        # 路由：按**实际解析文件**扩展名（.doc 已转为 .docx）
        suffix = work_path.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            doc_type = "test/test_list"
        logger.info("[office] 解析路由: %s -> %s", file_path.name, doc_type)

        if doc_type == "test/test_list":
            knowledge_blocks = parse_test_list(
                work_path,
                product_module="",
                document_category=procurement_doc_category,
            )
        else:
            knowledge_blocks = parse_spec_document(
                work_path,
                document_category=procurement_doc_category,
                product_module="",
            )
    except Exception as exc:
        logger.warning("[office] 解析失败 %s: %s", file_path.name, exc)
        knowledge_blocks = []

    if not knowledge_blocks:
        logger.warning("[office] 未生成任何知识块: %s", file_path.name)
        return

    # 3. 运行 metadata 增强（规则 + index）
    config = _load_project_config()
    meta_rules = config.get("metadata_rules", {})
    protocol_map = meta_rules.get("protocol_types", {})

    for block in knowledge_blocks:
        meta = block.get("metadata", {})
        meta["source_file"] = source_label
        meta["document_category"] = "unknown"
        meta.pop("product_module", None)
        content = str(block.get("page_content") or "")

        # protocol_type
        if not meta.get("protocol_type"):
            found_protocols = _match_protocols_word_boundary(protocol_map, content)
            if found_protocols:
                meta["protocol_type"] = found_protocols

    # 增强 scenario_id / step_type
    _enhance_metadata_with_function_index(knowledge_blocks)
    _ensure_procurement_base_fields(knowledge_blocks, file_path, source_label)

    # 4. 采购流程固定只做采购落盘，不再触发农民/农场主/树挂载。
    link_stats = {"total": len(knowledge_blocks), "skipped": True, "procurement_only": True}

    # 5. 写入 JSON
    json_path.write_text(
        json.dumps(knowledge_blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 6. 写入缓存
    cache_payload = {
        "source_file": str(file_path),
        "source_file_fingerprint": _compute_file_fingerprint(file_path),
        "config_fingerprint": _compute_config_fingerprint(),
        "schema_version": AUTO_CONVERT_SCHEMA_VERSION,
        "doc_type": doc_type,
        "link_stats": link_stats,
        "record_count": len(knowledge_blocks),
        "output_json": str(json_path),
    }
    cache_path.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    elapsed = time.perf_counter() - start_time
    logger.info(
        "[office] 完成: %s -> %s (%d 块, %.2fs)",
        file_path.name, json_path.name, len(knowledge_blocks), elapsed,
    )


# ============================================================
# TXT 文档处理
# ============================================================
# Bug fix detail 字段提取模式
_BUG_FIX_FIELD_RE = re.compile(
    r"^(Description|Root Cause|Condition of Occurrence|Fixed Details"
    r"|Testing Suggestions|Affected Release|Extra Impact)\s*:",
    re.MULTILINE,
)


def _parse_bug_fix_text(text: str, source_file: str) -> List[Dict[str, Any]]:
    """将 bug fix detail 文本拆分为结构化知识块。"""
    blocks: List[Dict[str, Any]] = []

    # 提取 Bug ID
    bug_match = re.search(r"Bug\s+(\d+)", text, re.IGNORECASE)
    bug_id = bug_match.group(1) if bug_match else ""

    # 提取关键字段
    fields: Dict[str, str] = {}
    positions = list(_BUG_FIX_FIELD_RE.finditer(text))
    for i, m in enumerate(positions):
        key = m.group(1)
        start = m.end()
        end = positions[i + 1].start() if i + 1 < len(positions) else len(text)
        value = text[start:end].strip().strip(":")
        # 截断到下一个 Comment 行（SVN commit 部分不属于该字段）
        comment_cut = re.search(r"\nComment\s+\d+", value)
        if comment_cut:
            value = value[: comment_cut.start()].strip()
        fields[key] = value

    # 提取 SVN commit 信息
    svn_commits: List[str] = []
    for m in re.finditer(r"New Revision:\s*(\d+)", text):
        rev = m.group(1)
        # 向后找到 Modified 行获取受影响文件
        mod_match = re.search(
            r"Modified:\s*\n\s*(.+?)(?:\nLog:|\n\n)", text[m.start() :]
        )
        file_changed = mod_match.group(1).strip() if mod_match else ""
        svn_commits.append(f"r{rev}: {file_changed}")

    base_meta = {
        "source_file": source_file,
        "document_category": "unknown",
        "bug_id": bug_id,
    }

    # Block 1: 概要
    first_line = text.split("\n")[0].strip()
    blocks.append(
        {
            "page_content": f"Bug {bug_id} 概述: {first_line}",
            "metadata": {
                **base_meta,
                "block_type": "bug_summary",
                "block_id": f"bug_{bug_id}_summary",
                "section_title": "Bug 概述",
            },
        }
    )

    # Block 2: 详情（Description + Root Cause + Condition + Fix）
    detail_parts = []
    for key in [
        "Description",
        "Root Cause",
        "Condition of Occurrence",
        "Fixed Details",
    ]:
        if key in fields:
            detail_parts.append(f"{key}: {fields[key]}")
    if detail_parts:
        blocks.append(
            {
                "page_content": "\n".join(detail_parts),
                "metadata": {
                    **base_meta,
                    "block_type": "bug_detail",
                    "block_id": f"bug_{bug_id}_detail",
                    "section_title": "Bug 详情与修复",
                },
            }
        )

    # Block 3: 代码修改记录
    if svn_commits:
        affected = fields.get("Affected Release", "")
        commit_text = "代码修改记录:\n" + "\n".join(svn_commits)
        if affected:
            commit_text += f"\nAffected Release: {affected}"
        blocks.append(
            {
                "page_content": commit_text,
                "metadata": {
                    **base_meta,
                    "block_type": "code_change",
                    "block_id": f"bug_{bug_id}_code",
                    "section_title": "代码修改记录",
                },
            }
        )

    # Block 4: 测试建议
    if "Testing Suggestions" in fields:
        blocks.append(
            {
                "page_content": f"Testing Suggestions: {fields['Testing Suggestions']}",
                "metadata": {
                    **base_meta,
                    "block_type": "test_suggestion",
                    "block_id": f"bug_{bug_id}_test",
                    "section_title": "测试建议",
                },
            }
        )

    return blocks


def _parse_generic_text(text: str, source_file: str, category: str = "unknown") -> List[Dict[str, Any]]:
    """将通用 TXT 文件按段落拆分为知识块。"""
    blocks: List[Dict[str, Any]] = []
    paragraphs = re.split(r"\n{2,}", text.strip())
    for i, para in enumerate(paragraphs):
        para = para.strip()
        if not para or len(para) < 10:
            continue
        blocks.append(
            {
                "page_content": para,
                "metadata": {
                    "source_file": source_file,
                    "document_category": category,
                    "block_type": "text_paragraph",
                    "block_id": f"para_{i + 1}",
                },
            }
        )
    return blocks


def convert_text_file(file_path: Path) -> None:
    """
    处理单个 TXT 文档，生成知识块 JSON。

    流程：
    1. 读取文本内容
    2. 用 document_classifier 分类
    3. 根据分类选择解析方式（bug_fix 专用解析 / 通用段落解析）
    4. 运行 metadata 增强
    5. 农民匹配 + 农场主决策
    6. 写入 reference/ 目录
    """
    stem = _output_stem_for_file(file_path)
    json_path = REFERENCE_DIR / f"{stem}.json"
    source_label = json_path.name
    cache_path = LOG_DIR / f"{stem}.cache.json"
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if _can_skip_office_cached(file_path, json_path, cache_path):
        logger.info("[text-cache] 跳过 %s，内容未变更", file_path.name)
        return

    start_time = time.perf_counter()
    logger.info("[text] 开始处理: %s", file_path.name)

    text = file_path.read_text(encoding="utf-8", errors="replace")

    # 按内容启发式路由解析器（不调 classify_document）
    _is_bug_fix = bool(re.search(r"Bug\s+\d+", text[:2000], re.IGNORECASE))
    if _is_bug_fix:
        knowledge_blocks = _parse_bug_fix_text(text, source_label)
    else:
        knowledge_blocks = _parse_generic_text(text, source_label, "text")

    if not knowledge_blocks:
        logger.warning("[text] 未生成任何知识块: %s", file_path.name)
        return

    # metadata 增强
    config = _load_project_config()
    meta_rules = config.get("metadata_rules", {})
    protocol_map = meta_rules.get("protocol_types", {})

    for block in knowledge_blocks:
        meta = block.get("metadata", {})
        content = str(block.get("page_content") or "")
        meta["document_category"] = "unknown"
        meta.pop("product_module", None)

        if not meta.get("protocol_type"):
            found_protocols = _match_protocols_word_boundary(protocol_map, content)
            if found_protocols:
                meta["protocol_type"] = found_protocols

    _enhance_metadata_with_function_index(knowledge_blocks)
    _ensure_procurement_base_fields(knowledge_blocks, file_path, source_label)

    # 采购流程固定只做采购落盘，不再触发农民/农场主/树挂载。
    link_stats = {"total": len(knowledge_blocks), "skipped": True, "procurement_only": True}

    # 写入 JSON
    json_path.write_text(
        json.dumps(knowledge_blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 写入缓存
    cache_payload = {
        "source_file": str(file_path),
        "source_file_fingerprint": _compute_file_fingerprint(file_path),
        "config_fingerprint": _compute_config_fingerprint(),
        "schema_version": AUTO_CONVERT_SCHEMA_VERSION,
        "link_stats": link_stats,
        "record_count": len(knowledge_blocks),
        "output_json": str(json_path),
    }
    cache_path.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    elapsed = time.perf_counter() - start_time
    logger.info(
        "[text] 完成: %s -> %s (%d 块, %.2fs)",
        file_path.name,
        json_path.name,
        len(knowledge_blocks),
        elapsed,
    )


def _prompt_continue_on_error(component: str, error: str) -> bool:
    if cfg_str("auto_convert.assume_yes", "", env="AUTO_CONVERT_ASSUME_YES").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }:
        logger.info(
            "[%s] AUTO_CONVERT_ASSUME_YES is set; continue despite error.",
            component,
        )
        return True

    prompt = (
        f"{component} 不可用或配置异常：{error}\n"
        "是否继续？继续输入 Y，取消输入 N: "
    )
    print(prompt, end="", flush=True)
    try:
        answer = sys.stdin.readline().strip().lower()
    except Exception:
        return False
    return answer in {"y", "yes"}


def _target_paths(pdf: Path) -> Tuple[Path, Path]:
    stem = _output_stem_for_file(pdf)
    json_path = REFERENCE_DIR / f"{stem}.json"
    cache_path = LOG_DIR / f"{stem}.cache.json"
    return json_path, cache_path


def _compute_pdf_fingerprint(pdf: Path) -> Dict[str, object]:
    stat = pdf.stat()
    sha256 = hashlib.sha256()
    with pdf.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
    return {
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "sha256": sha256.hexdigest(),
    }


def _compute_file_fingerprint(src: Path) -> Dict[str, object]:
    """任意源文件（Office/TXT 等）的 size/mtime/sha256，用于缓存跳过判定。"""
    stat = src.stat()
    sha256 = hashlib.sha256()
    with src.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
    return {
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "sha256": sha256.hexdigest(),
    }


def _can_skip_office_cached(
    src: Path,
    json_path: Path,
    cache_path: Path,
) -> bool:
    """Office/TXT 与 PDF 共用跳过逻辑：缓存 fingerprint + config fingerprint。"""
    if not (json_path.exists() and cache_path.exists()):
        return False
    try:
        meta = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    cached_config = meta.get("config_fingerprint")
    if cached_config and cached_config != _compute_config_fingerprint():
        logger.info("[cache] config changed for %s, re-processing.", src.name)
        return False
    cached_fp = meta.get("source_file_fingerprint") or meta.get("source_pdf_fingerprint")
    if not isinstance(cached_fp, dict):
        return False
    current = _compute_file_fingerprint(src)
    if (
        cached_fp.get("size") == current.get("size")
        and cached_fp.get("mtime") == current.get("mtime")
    ):
        return True
    logger.info("[cache] mtime/size changed for %s, verifying content...", src.name)
    cached_hash = cached_fp.get("sha256")
    if cached_hash and cached_hash == current.get("sha256"):
        return True
    logger.info("[cache] content changed for %s, re-processing.", src.name)
    return False


def _compute_config_fingerprint() -> str:
    """Compute fingerprint of only the LLM-relevant config sections.

    We ignore general MinerU settings (like vLLM URL, timeouts) to avoid
    unnecessary re-processing when infrastructure settings change.
    """
    config = _load_project_config()
    relevant = {
        "llm-aided-config": config.get("llm-aided-config", {}),
        "metadata_rules": config.get("metadata_rules", {}),
        # Include model source just in case it affects which model is used
        "MINERU_MODEL_SOURCE": os.environ.get("MINERU_MODEL_SOURCE", ""),
        "AUTO_CONVERT_SCHEMA_VERSION": AUTO_CONVERT_SCHEMA_VERSION,
    }
    # Stable JSON dump
    payload = json.dumps(relevant, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _update_cache_mtime(cache_path: Path, new_fingerprint: Dict[str, object]) -> None:
    """Update only the file stats in the cache file to match current reality."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        data["source_pdf_fingerprint"] = new_fingerprint
        cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("[cache] updated mtime/size for %s (content unchanged)", cache_path.name)
    except Exception as e:
        logger.warning("[cache] failed to update mtime: %s", e)


def _can_skip_cached(
    pdf: Path,
    json_path: Path,
    cache_path: Path,
) -> bool:
    if not (json_path.exists() and cache_path.exists()):
        return False
    try:
        meta = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    cached_fingerprint = meta.get("source_pdf_fingerprint", {})
    if not isinstance(cached_fingerprint, dict):
        return False

    # 1. Config Check (Strict)
    cached_config = meta.get("config_fingerprint")
    if cached_config and cached_config != _compute_config_fingerprint():
        logger.info("[cache] config changed for %s, re-processing.", pdf.name)
        return False

    # 2. Fast Path: Mtime & Size Match
    current_stat = pdf.stat()
    if (
        cached_fingerprint.get("size") == current_stat.st_size
        and cached_fingerprint.get("mtime") == current_stat.st_mtime
    ):
        return True

    # 3. Slow Path: Content Hash Check
    # If mtime/size changed, we must verify if content actually changed.
    logger.info("[cache] mtime/size changed for %s, verifying content...", pdf.name)
    cached_hash = cached_fingerprint.get("sha256")
    if not cached_hash:
        return False  # Old cache format without hash

    current_fingerprint = _compute_pdf_fingerprint(pdf)
    if current_fingerprint.get("sha256") == cached_hash:
        # Content is identical! Update cache file with new mtime/size so next check is fast.
        _update_cache_mtime(cache_path, current_fingerprint)
        return True

    logger.info("[cache] content changed for %s, re-processing.", pdf.name)
    return False


def _clean_chunk_text(text: str) -> str:
    """Normalize text by removing excess whitespace and special characters."""
    # Fix hyphenation (word- break -> wordbreak)
    text = re.sub(r'(\w+)-\s+(\w+)', r'\1\2', text)
    # Remove excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove non-printable characters (basic check)
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()

def _apply_llm_metadata_extraction(text: str, meta: Dict[str, object], config: Dict) -> None:
    """Apply LLM metadata extraction for a single chunk (delegates to batch)."""
    _apply_llm_metadata_extraction_batch([(text, meta)], config)


# ---------------------------------------------------------------------------
# Batch LLM metadata extraction
# - Fixed system prompt enables KV-cache on compatible inference servers
# - Token-aware sub-batching stays within model context limits
# ---------------------------------------------------------------------------

_BATCH_LLM_SIZE = cfg_int("auto_convert.batch.size", 40, env="LLM_BATCH_SIZE")
_BATCH_TEXT_LIMIT = 500      # chars per chunk snippet in prompt
_BATCH_TOKEN_BUDGET = cfg_int("auto_convert.batch.token_budget", 6000, env="LLM_BATCH_TOKEN_BUDGET")
_BATCH_MAX_WORKERS = cfg_int("auto_convert.batch.max_workers", 8, env="LLM_BATCH_MAX_WORKERS")
_CHARS_PER_TOKEN = 3.5       # rough estimate for mixed EN/ZH


def _build_batch_system_prompt(meta_rules: Dict) -> str:
    """Build the fixed system prompt (cached by LLM inference servers)."""
    valid_intents = list(meta_rules.get("intents", {}).keys())
    valid_config_modes = list(meta_rules.get("config_modes", {}).keys())
    valid_protocol_types = list(meta_rules.get("protocol_types", {}).keys())
    valid_command_prefixes = list(meta_rules.get("command_prefixes", {}).keys())
    product_modules_map = meta_rules.get("product_modules", {})
    pm_descs = [f"{m} ({', '.join(kws[:3])})" for m, kws in product_modules_map.items()]
    return (
        "You are a batch metadata extractor for technical documentation chunks.\n"
        "Extract metadata for EACH chunk. Return a JSON array (one object per chunk, in order).\n\n"
        "Per-chunk fields:\n"
        "- product_module: functional module (SLB/LLB/基础网络/安全). "
        "Infer from section path (highest priority) > section title > command prefix > content keywords. "
        "The 'path=' header shows the full chapter hierarchy (e.g. path=高可用性（HA） > 概述 means this chunk belongs to HA module). "
        "Use 'unknown' only for pure frontmatter (TOC/copyright/about-us). "
        "Protocols (HTTP/TCP) go in protocol_type, NOT here.\n"
        "- protocol_type: list of protocols mentioned, [] if none\n"
        "- intent: from valid list\n"
        "- config_mode: cli | console | api\n"
        "- command_prefix: first command word if CLI syntax, '' otherwise\n"
        "- description: max 20 words\n"
        "- required_keywords: up to 5 important technical terms\n"
        "- step_type: e.g. basic_config/health_checks/policies_and_algorithms, or ''\n"
        "- chunk_type: single_command | command_list | narrative\n"
        "- override_commands: command names that support override/覆盖, [] otherwise\n"
        f"Valid Intents: {valid_intents}\n"
        f"Valid Config Modes: {valid_config_modes}\n"
        f"Valid Modules: {', '.join(pm_descs[:20])}\n"
        f"Valid Protocols: {valid_protocol_types}\n"
        f"Valid Prefixes: {valid_command_prefixes}\n\n"
        "Return ONLY a JSON array. No markdown, no explanation."
    )


def _merge_llm_meta(llm_meta: Dict, meta_item: Dict, meta_rules: Dict) -> None:
    """Merge LLM-extracted fields into meta_item in-place."""
    if llm_meta.get("intent"):
        meta_item["intent"] = llm_meta["intent"]
    if llm_meta.get("config_mode"):
        meta_item["config_mode"] = llm_meta["config_mode"]
    if llm_meta.get("required_keywords") and isinstance(llm_meta["required_keywords"], list):
        meta_item["required_keywords"] = llm_meta["required_keywords"]
    if llm_meta.get("product_module"):
        pm = str(llm_meta["product_module"]).strip()
        pm = {"未知": "unknown", "未知模块": "unknown"}.get(pm, pm)
        meta_item["product_module"] = "unknown" if pm.lower() in {"unknown", "未知", ""} else pm
    if llm_meta.get("protocol_type") and isinstance(llm_meta["protocol_type"], list):
        meta_item["protocol_type"] = llm_meta["protocol_type"]
    if llm_meta.get("command_prefix"):
        meta_item["command_prefix"] = llm_meta["command_prefix"]
    if llm_meta.get("description"):
        meta_item["description"] = llm_meta["description"]
    if llm_meta.get("section_title"):
        meta_item["section_title"] = _remove_section_number(str(llm_meta["section_title"]))
    if llm_meta.get("parent_section"):
        meta_item["parent_section"] = _remove_section_number(str(llm_meta["parent_section"]))
    if llm_meta.get("scenario_id"):
        meta_item["scenario_id"] = str(llm_meta["scenario_id"])
    if llm_meta.get("step_type"):
        meta_item["step_type"] = str(llm_meta["step_type"])
    if llm_meta.get("function_hierarchy"):
        fh = str(llm_meta["function_hierarchy"]).strip()
        if fh:
            meta_item["function_hierarchy"] = " > ".join(
                _remove_section_number(p.strip()) for p in fh.split(">")
            )
    if llm_meta.get("command_structure") and isinstance(llm_meta["command_structure"], dict):
        meta_item["command_structure"] = llm_meta["command_structure"]
    if llm_meta.get("chunk_type"):
        meta_item["chunk_type"] = str(llm_meta["chunk_type"])
    if llm_meta.get("override_commands") and isinstance(llm_meta["override_commands"], list):
        meta_item["override_commands"] = llm_meta["override_commands"]
    # Header block override: MinerU 有时把命令语法行识别为章节标题（header）
    # 若 clean_text 以小写 ASCII token 开头且含参数标记，修正为 single_command/cli
    if meta_item.get("block_type") == "header":
        _hdr_text = str(meta_item.get("clean_text") or "").strip()
        _hdr_tokens = _hdr_text.split()
        if (
            _hdr_tokens
            and _hdr_tokens[0].isascii()
            and _hdr_tokens[0].islower()
            and re.search(r'[<\[{]', _hdr_text)
        ):
            meta_item["chunk_type"] = "single_command"
            meta_item["document_category"] = "cli"


def _apply_llm_metadata_extraction_batch(
    items: List[Tuple[str, Dict[str, object]]],
    config: Dict,
) -> None:
    """Apply LLM metadata extraction for a batch of (clean_text, meta) pairs.

    Uses a fixed system prompt (KV-cache friendly) and variable user messages.
    Token-aware sub-batching splits items when estimated tokens exceed
    _BATCH_TOKEN_BUDGET to stay within model limits.
    """
    if not items:
        return

    global _LLM_GATEWAY_AVAILABLE, _LLM_METADATA_AVAILABLE
    if _LLM_GATEWAY_AVAILABLE is False or _LLM_METADATA_AVAILABLE is False:
        return

    if OpenAI is None:
        return

    try:
        runtime = _get_llm_gateway_runtime()
    except Exception:
        return

    selected_api_key = str(runtime.get("api_key") or "")
    selected_base_url = str(runtime.get("base_url") or "")
    selected_model = str(runtime.get("model") or "")
    if not selected_api_key:
        return

    if _LLM_METADATA_AVAILABLE is None or not _LLM_METADATA_AVAILABLE:
        if not selected_base_url:
            _LLM_METADATA_AVAILABLE = False
            return
        _LLM_METADATA_AVAILABLE = _ensure_urls_reachable(
            [selected_base_url],
            attempts=NET_RETRY_ATTEMPTS,
            delay=NET_RETRY_DELAY,
            timeout=NET_TIMEOUT,
            label="llm-metadata-batch",
            accept_http_error=True,
        )
        if not _LLM_METADATA_AVAILABLE:
            return

    full_config = _load_project_config()
    meta_rules = full_config.get("metadata_rules", {})
    system_prompt = _build_batch_system_prompt(meta_rules)
    client = OpenAI(
        api_key=selected_api_key,
        base_url=selected_base_url,
        timeout=config.get("timeout", 120),
    )

    def _process_sub_batch(sub_items: List[Tuple[str, Dict[str, object]]]) -> None:
        chunk_lines = []
        for idx, (text, meta_item) in enumerate(sub_items):
            sec = str(meta_item.get("section_title") or "").strip()
            par = str(meta_item.get("parent_section") or "").strip()
            spath = _sanitize_section_path(str(meta_item.get("section_path") or "")).strip()
            src = str(meta_item.get("source_file") or "")
            snippet = text[:_BATCH_TEXT_LIMIT]
            header = f"[CHUNK {idx}]"
            if spath:
                header += f" path={spath}"
            elif sec:
                header += f" section={sec}"
            if par and par != sec and not spath:
                header += f" parent={par}"
            if src:
                header += f" source={src}"
            chunk_lines.append(f"{header}\n{snippet}")

        user_content = "\n---\n".join(chunk_lines)

        try:
            response = _call_llm_with_retry_and_fallback(
                client=client,
                base_url=selected_base_url,
                model=selected_model,
                messages=[
                    {"role": "system", "content": system_prompt + "\n\n必须返回 JSON 数组格式。"},
                    {"role": "user", "content": user_content},
                ],
                temperature=_llm_temperature(),
                allow_fallback=False,
                fallback_config=None,
            )
            content = response.choices[0].message.content or "[]"
            if "```" in content:
                match = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
                if match:
                    content = match.group(1).strip()
            if json_repair:
                parsed = json_repair.loads(content)
            else:
                parsed = json.loads(content)

            if isinstance(parsed, dict):
                for key in ("results", "chunks", "data", "items", "metadata"):
                    if key in parsed and isinstance(parsed[key], list):
                        parsed = parsed[key]
                        break
                else:
                    parsed = [parsed]

            if not isinstance(parsed, list):
                raise ValueError(f"Expected list, got {type(parsed)}")

        except Exception as e:
            logger.warning("[batch-llm] sub-batch parse failed (%s); skipping merge", e)
            return

        for idx, (_, meta_item) in enumerate(sub_items):
            if idx < len(parsed) and isinstance(parsed[idx], dict):
                _merge_llm_meta(parsed[idx], meta_item, meta_rules)

    # 先把所有 items 切分成 sub_batches 列表，再并发提交
    sub_batches: List[List[Tuple[str, Dict[str, object]]]] = []
    sub_batch: List[Tuple[str, Dict[str, object]]] = []
    token_count = 0
    total = len(items)
    for text, meta_item in items:
        item_tokens = math.ceil(len(text[:_BATCH_TEXT_LIMIT]) / _CHARS_PER_TOKEN)
        if sub_batch and (token_count + item_tokens > _BATCH_TOKEN_BUDGET or len(sub_batch) >= _BATCH_LLM_SIZE):
            sub_batches.append(sub_batch)
            sub_batch = []
            token_count = 0
        sub_batch.append((text, meta_item))
        token_count += item_tokens
    if sub_batch:
        sub_batches.append(sub_batch)

    total_sub_batches = len(sub_batches)
    workers = min(_BATCH_MAX_WORKERS, total_sub_batches) if total_sub_batches else 1
    logger.info("[batch-llm] 开始处理 %d 块，%d 个子批次，并发数=%d", total, total_sub_batches, workers)

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process_sub_batch, sb): i for i, sb in enumerate(sub_batches)}
        for future in as_completed(futures):
            completed += 1
            idx = futures[future]
            try:
                future.result()
                logger.info("[batch-llm] 子批次 %d/%d 完成 (%d/%d)", idx + 1, total_sub_batches, completed, total_sub_batches)
            except Exception as e:
                logger.warning("[batch-llm] 子批次 %d 失败: %s", idx + 1, e)

    logger.info("[batch-llm] 全部完成，共 %d 块", total)


def _enhance_metadata_with_function_index(knowledge_blocks: List[Dict[str, any]]) -> None:
    """
    基于功能结构索引增强 knowledge_blocks 的 metadata（添加 scenario_id 和 step_type）。
    
    Args:
        knowledge_blocks: 知识库块列表，每个块包含 page_content 和 metadata
    """
    function_index_path = DOC_LOCAL_DIR / "function_structure_index.json"
    if not function_index_path.exists():
        logger.debug("功能结构索引不存在，跳过 metadata 增强: %s", function_index_path)
        return
    
    try:
        with open(function_index_path, "r", encoding="utf-8") as f:
            function_index = json.load(f)
    except Exception as e:
        logger.warning("加载功能结构索引失败，跳过 metadata 增强: %s", e)
        return
    
    scenarios = function_index.get("scenarios", {})
    
    # 从索引动态加载步骤类型关键词（不硬编码）
    from INAGENT.utils.index_utils import get_step_type_keywords
    step_type_keywords = get_step_type_keywords(function_index)
    
    for block in knowledge_blocks:
        metadata = block.get("metadata", {})
        text = block.get("page_content", "")
        text_lower = text.lower()
        
        # 提取 product_module 和 protocol_type
        product_module = metadata.get("product_module", "")
        protocol_types = metadata.get("protocol_type", [])
        if isinstance(protocol_types, str):
            protocol_types = [protocol_types]
        elif not isinstance(protocol_types, list):
            protocol_types = []
        
        # 匹配场景 ID（每次都重新计算，避免旧值残留）
        scenario_id: Optional[str] = None
        for sid, scenario_config in scenarios.items():
            product_match = any(
                pm.lower() in text_lower or pm == product_module
                for pm in scenario_config.get("product_modules", [])
            )
            protocol_match = (
                not scenario_config.get("protocol_types") or
                any(
                    pt.lower() in text_lower or pt in protocol_types
                    for pt in scenario_config.get("protocol_types", [])
                )
            )
            if product_match and protocol_match:
                scenario_id = sid
                break
        
        # 识别步骤类型
        step_type = None
        for stype, keywords in step_type_keywords.items():
            if any(kw.lower() in text_lower for kw in keywords):
                step_type = stype
                break
        
        # 如果已有 function_hierarchy，尝试从中提取 step_type
        function_hierarchy = metadata.get("function_hierarchy", "")
        if function_hierarchy and not step_type:
            hierarchy_lower = function_hierarchy.lower()
            for stype, keywords in step_type_keywords.items():
                if any(kw.lower() in hierarchy_lower for kw in keywords):
                    step_type = stype
                    break
        
        # 更新 metadata（优先使用LLM提取的值，只在缺失时补充）
        # scenario_id: 如果LLM已提取且不是"unknown"，优先使用；否则使用增强阶段匹配的结果
        current_scenario_id = metadata.get("scenario_id")
        if current_scenario_id and current_scenario_id != "unknown":
            # LLM已提取有效的scenario_id，保留它
            pass
        elif scenario_id:
            # LLM未提取或提取为"unknown"，使用增强阶段匹配的结果
            metadata["scenario_id"] = scenario_id
        elif current_scenario_id == "unknown":
            # 保持"unknown"状态
            pass
        else:
            # 如果之前没有设置，至少设置为"unknown"以便后续处理
            if not current_scenario_id:
                metadata["scenario_id"] = "unknown"
        
        # step_type: 如果LLM已提取，优先使用；否则使用增强阶段识别的结果
        current_step_type = metadata.get("step_type")
        if current_step_type:
            # LLM已提取step_type，保留它
            pass
        elif step_type:
            # LLM未提取，使用增强阶段识别的结果
            metadata["step_type"] = step_type


_PRESERVE_META_FIELDS = (
    "command_prefix", "product_module", "protocol_type", "intent",
    "config_mode", "chunk_type", "function_hierarchy", "document_category",
    "required_keywords", "description", "tree_level",
)


def _extract_chunk_metadata(
    text: str,
    base_meta: Optional[Dict[str, Any]] = None,
    *,
    skip_llm: bool = False,
) -> Dict[str, object]:
    config = _load_project_config()
    llm_config = config.get("llm-aided-config", {}).get("metadata_extraction", {})

    clean_text = _clean_chunk_text(text)
    meta: Dict[str, object] = {"clean_text": clean_text}
    if base_meta:
        section_title = str(base_meta.get("section_title") or "").strip()
        parent_section = str(base_meta.get("parent_section") or "").strip()
        if section_title:
            meta["section_title"] = section_title
        if parent_section:
            meta["parent_section"] = parent_section
        section_path = base_meta.get("section_path")
        if section_path:
            meta["section_path"] = _sanitize_section_path(str(section_path))
        for field in _PRESERVE_META_FIELDS:
            val = base_meta.get(field)
            if val and val != "unknown":
                meta[field] = val

    if not skip_llm and llm_config.get("enable", False):
        _apply_llm_metadata_extraction(clean_text, meta, llm_config)

    return meta


def _build_models_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/models"
    return f"{base_url}/v1/models"


def _prompt_continue_without_vllm() -> bool:
    if cfg_str("auto_convert.assume_yes", "", env="AUTO_CONVERT_ASSUME_YES").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }:
        logger.info(
            "[vllm] AUTO_CONVERT_ASSUME_YES is set; continue without vLLM."
        )
        return True
    prompt = "vLLM 不可用，是否继续使用本地后端？继续输入 Y，取消输入 N: "
    print(prompt, end="", flush=True)
    try:
        # Use sys.stdin.readline to avoid potential input() buffering issues
        answer = sys.stdin.readline().strip().lower()
    except Exception:
        return False
    return answer in {"y", "yes"}


def _check_url_once(
    url: str,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
    *,
    accept_http_error: bool = False,
) -> Tuple[bool, Optional[str]]:
    try:
        request = urllib.request.Request(url, method="GET", headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout):
            return True, None
    except urllib.error.HTTPError as exc:
        # Some providers return 401/403/404 on base URLs even when the host is
        # reachable. For connectivity checks, treat any <500 status as
        # "reachable" when accept_http_error=True.
        if accept_http_error and getattr(exc, "code", 0) < 500:
            return True, None
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def _fetch_vllm_model_name(base_url: str, timeout: float) -> Optional[str]:
    models_url = _build_models_url(base_url)
    request = urllib.request.Request(models_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("[vllm] model list request failed: %s", exc)
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("[vllm] model list response is not JSON.")
        return None

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if isinstance(first, dict):
        return str(first.get("id")) if first.get("id") else None
    return None


def _probe_vllm_completion(base_url: str, model: str, timeout: float) -> bool:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0.0,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except Exception as exc:
        logger.warning("[vllm] inference probe failed: %s", exc)
        return False


def _ensure_urls_reachable(
    urls: List[str],
    *,
    attempts: int,
    delay: float,
    timeout: float,
    label: str,
    headers: Optional[Dict[str, str]] = None,
    accept_http_error: bool = False,
) -> bool:
    if not urls:
        return True
    for attempt in range(1, attempts + 1):
        failures: Dict[str, str] = {}
        for url in urls:
            ok, error = _check_url_once(
                url,
                timeout,
                headers=headers,
                accept_http_error=accept_http_error,
            )
            if not ok:
                failures[url] = error or "unknown error"
        if not failures:
            return True
        logger.warning(
            "[%s] connectivity check failed (attempt %d/%d): %s",
            label,
            attempt,
            attempts,
            failures,
        )
        if attempt < attempts:
            time.sleep(delay * attempt)
    return False


def _get_frontmatter_config() -> Dict:
    config = _load_project_config()
    llm_config = config.get("llm-aided-config", {})
    frontmatter = llm_config.get("frontmatter_filter", {})
    if not frontmatter:
        # Fallback: reuse title_aided config if present
        title_aided = llm_config.get("title_aided", {})
        if title_aided:
            frontmatter = {
                "enable": False,
                "api_key": title_aided.get("api_key"),
                "base_url": title_aided.get("base_url"),
                "model": title_aided.get("model"),
                "timeout": title_aided.get("timeout", 600),
                "stream": False,
                "max_pages": 10,
                "max_chars_per_page": 800,
            }
    return frontmatter


def _mineru_string_list_to_lines(val: Any) -> List[str]:
    """MinerU ``table_caption`` / ``table_footnote`` may be ``[]`` or ``list[str]``."""
    if not val:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, list):
        out: List[str] = []
        for x in val:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    return []


def _table_body_html_to_plain_text(html: str) -> str:
    """Convert MinerU ``table_body`` HTML to plain text for chunks and metadata rules."""
    if not isinstance(html, str) or not html.strip():
        return ""
    raw = html_stdlib.unescape(html)
    # Typical CLI manual: two-column parameter tables
    raw = re.sub(r"(?i)</td>\s*<td>", " — ", raw)
    raw = re.sub(r"(?i)</th>\s*<td>", " — ", raw)
    raw = re.sub(r"(?i)</th>\s*<th>", " — ", raw)
    raw = re.sub(r"(?i)</tr>\s*", "\n", raw)
    raw = re.sub(r"(?i)<br\s*/?>", " ", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    lines: List[str] = []
    for line in raw.splitlines():
        s = " ".join(line.split())
        if s:
            lines.append(s)
    return "\n".join(lines)


def _extract_text_from_block(block: Dict) -> str:
    text = block.get("text")
    if isinstance(text, str) and text.strip():
        return text

    # MinerU ``type: table`` — parameter explanations live in ``table_body`` (HTML).
    tb = block.get("table_body")
    if isinstance(tb, str) and tb.strip():
        parts: List[str] = []
        parts.extend(_mineru_string_list_to_lines(block.get("table_caption")))
        plain = _table_body_html_to_plain_text(tb)
        if plain:
            parts.append(plain)
        parts.extend(_mineru_string_list_to_lines(block.get("table_footnote")))
        if parts:
            return "\n".join(parts)

    content = block.get("content")
    if not isinstance(content, dict):
        return ""

    if "title_content" in content:
        return "".join(
            part.get("content", "")
            for part in content.get("title_content", [])
            if isinstance(part, dict)
        )
    if "paragraph_content" in content:
        return "".join(
            part.get("content", "")
            for part in content.get("paragraph_content", [])
            if isinstance(part, dict)
        )
    if "list_items" in content:
        items = []
        for item in content.get("list_items", []):
            item_content = item.get("item_content", [])
            items.append(
                "".join(
                    part.get("content", "")
                    for part in item_content
                    if isinstance(part, dict)
                )
            )
        return "\n".join(items)
    return ""


def _build_section_context_map(blocks: List[Dict]) -> Dict[int, Dict[str, str]]:
    """Build section context (section_title/parent_section/section_path)
    for each block index using MinerU hierarchy or numbered headings.
    """
    section_stack: List[Tuple[int, str]] = []
    context_map: Dict[int, Dict[str, str]] = {}

    for idx, block in enumerate(blocks):
        raw_text = _extract_text_from_block(block)
        clean_text = _clean_chunk_text(raw_text)
        block_type = str(block.get("type") or "").lower()
        text_level = block.get("text_level")

        heading_level: Optional[int] = None
        heading_title = ""

        first_line_raw = raw_text.strip().split('\n')[0].strip() if raw_text.strip() else ""

        if isinstance(text_level, int) and text_level > 0:
            heading_level = int(text_level)
            heading_title = first_line_raw or clean_text
        else:
            inferred_level = _infer_section_level_from_heading(clean_text)
            if not inferred_level:
                # MinerU 常见格式：第一行是纯中文标题，第二行是"章节号. 标题"
                # clean_text 把换行合并了，所以用 raw_text 逐行尝试
                for line in raw_text.strip().split('\n'):
                    line = line.strip()
                    if line:
                        inferred_level = _infer_section_level_from_heading(line)
                        if inferred_level:
                            break
            if inferred_level:
                heading_level = inferred_level
                heading_title = first_line_raw or clean_text
            elif block_type in {"title", "heading", "section"} and clean_text:
                heading_level = 1
                heading_title = first_line_raw or clean_text

        if heading_level and heading_title:
            normalized_title = _remove_section_number(heading_title)
            if normalized_title and _is_plausible_heading(normalized_title):
                while section_stack and section_stack[-1][0] >= heading_level:
                    section_stack.pop()
                section_stack.append((heading_level, normalized_title))

        if section_stack:
            section_title = section_stack[-1][1]
            parent_section = section_stack[-2][1] if len(section_stack) > 1 else ""
            section_path = " > ".join([item[1] for item in section_stack])
        else:
            section_title = ""
            parent_section = ""
            section_path = ""

        context_map[idx] = {
            "section_title": section_title,
            "parent_section": parent_section,
            "section_path": section_path,
        }

    return context_map


def _build_page_previews(blocks: List[Dict], max_chars: int) -> Dict[int, str]:
    pages: Dict[int, List[str]] = {}
    for block in blocks:
        page_idx = block.get("page_idx")
        if page_idx is None:
            continue
        text = _extract_text_from_block(block)
        if not text:
            continue
        pages.setdefault(int(page_idx), []).append(text)

    previews: Dict[int, str] = {}
    for page_idx, chunks in pages.items():
        joined = "\n".join(chunks)
        previews[page_idx] = joined[:max_chars]
    return previews


def _parse_llm_json(content: str) -> Dict[int, bool]:
    content = content.strip()
    if not content:
        return {}
    # Try to extract the first JSON object from the response.
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        raw = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return {}

    normalized: Dict[int, bool] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                normalized[int(key)] = bool(value)
            except (TypeError, ValueError):
                continue
    return normalized


def _classify_frontmatter_pages(
    previews: Dict[int, str], frontmatter_cfg: Dict
) -> List[int]:
    """
    使用 LLM 分类前置页
    """
    if not previews:
        return []

    if OpenAI is None:
        logger.warning(
            "OpenAI client not available; skipping frontmatter filter."
        )
        return []

    runtime = _get_llm_gateway_runtime()
    api_key = str(runtime.get("api_key") or "")
    base_url = str(runtime.get("base_url") or "")
    model = str(runtime.get("model") or "")

    if not api_key:
        logger.warning("LLM_GATEWAY_API_KEY 未配置，跳过 frontmatter filter")
        return []

    # Simple connectivity check (lenient for 4xx HTTP status).
    if not _ensure_urls_reachable(
        [base_url],
        attempts=NET_RETRY_ATTEMPTS,
        delay=NET_RETRY_DELAY,
        timeout=NET_TIMEOUT,
        label="llm-frontmatter",
        accept_http_error=True,
    ):
        logger.warning(
            "LLM base_url not reachable (base_url=%s, model=%s); skipping.",
            base_url,
            model,
        )
        return []

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=frontmatter_cfg.get("timeout", 600),
    )

    prompt_lines = [
        "You are given page previews from a technical document.",
        "Mark pages that are frontmatter (TOC, copyright, preface,",
        "legal notices, acknowledgements, about/contact).",
        "Return JSON mapping page_idx to true/false.",
        "Only use page_idx keys provided.",
        "",
    ]
    for page_idx, preview in previews.items():
        prompt_lines.append(f"[page_idx={page_idx}]\n{preview}\n")

    try:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[LLM] 调用 frontmatter filter (base_url=%s, model=%s, pages=%d)", 
                        base_url, model, len(previews))
        llm_start_time = time.perf_counter()
        try:
            response = _call_llm_with_retry_and_fallback(
                client=client,
                base_url=base_url,
                model=model,
                messages=[{"role": "user", "content": "\n".join(prompt_lines)}],
                temperature=_llm_temperature(),
                stream=frontmatter_cfg.get("stream", False),
                max_retries=3,
            )
        except Exception as e:
            # frontmatter filter 失败不影响主流程，记录警告即可
            logger.warning(
                "[LLM] frontmatter filter 调用失败，跳过: %s", e
            )
            return []  # 返回空列表，不阻塞处理
        llm_elapsed = time.perf_counter() - llm_start_time
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[LLM] frontmatter filter 调用完成，耗时 %.2f 秒", llm_elapsed)
    except Exception as exc:
        logger.warning(
            "Frontmatter filter failed (base_url=%s, model=%s): %s",
            base_url,
            model,
            exc,
        )
        return []

    if frontmatter_cfg.get("stream", False):
        content_parts = []
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content is not None:
                content_parts.append(chunk.choices[0].delta.content)
        content = "".join(content_parts)
    else:
        content = response.choices[0].message.content or ""

    result = _parse_llm_json(content)
    return [idx for idx, is_front in result.items() if is_front]


def _filter_frontmatter_blocks(blocks: List[Dict]) -> Tuple[List[Dict], List[int]]:
    frontmatter_cfg = _get_frontmatter_config()
    if not frontmatter_cfg.get("enable", False):
        return blocks, []

    max_chars = int(frontmatter_cfg.get("max_chars_per_page", 800))
    max_pages = frontmatter_cfg.get("max_pages", 10)

    previews = _build_page_previews(blocks, max_chars=max_chars)
    if max_pages and len(previews) > max_pages:
        previews = dict(sorted(previews.items())[: int(max_pages)])

    start_time = time.perf_counter()
    frontmatter_pages = _classify_frontmatter_pages(previews, frontmatter_cfg)
    elapsed = time.perf_counter() - start_time
    logger.info(
        "Frontmatter filter: %d page(s) flagged in %.2fs",
        len(frontmatter_pages),
        elapsed,
    )

    if not frontmatter_pages:
        return blocks, []

    # Default to tag-only handoff to procurement, which applies reject/pending policy.
    # Keep drop-mode configurable for backward compatibility.
    drop_in_autoconvert = bool(frontmatter_cfg.get("drop_in_autoconvert", False))
    if not drop_in_autoconvert:
        logger.info(
            "Frontmatter filter: keeping flagged pages for procurement handoff (tag-only mode)."
        )
        return blocks, frontmatter_pages

    filtered = [
        block
        for block in blocks
        if block.get("page_idx") not in frontmatter_pages
    ]
    return filtered, frontmatter_pages


def _cleanup_old_logs(max_age_days: int = 30) -> None:
    """Remove log files older than max_age_days to prevent disk bloat."""
    if not LOG_DIR.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for log_file in LOG_DIR.glob("*.log"):
        # Keep the main auto_convert.log
        if log_file.name == "auto_convert.log":
            continue
        try:
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info("[cleanup] removed %d log files older than %d days", removed, max_age_days)


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Make logging setup idempotent (avoid duplicated logs)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Ensure CAMEL library logs are written to the same file
    set_log_level("INFO")
    set_log_file(str(LOG_FILE))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    existing_files = {
        getattr(h, "baseFilename", None)
        for h in root_logger.handlers
        if isinstance(h, logging.FileHandler)
    }
    if str(LOG_FILE) not in existing_files:
        root_logger.addHandler(file_handler)

    # Avoid multiple console handlers.
    has_stream = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        for h in root_logger.handlers
    )
    if not has_stream:
        root_logger.addHandler(console_handler)


def _cleanup_orphan_files(pdfs: List[Path], office_files: Optional[List[Path]] = None) -> None:
    """Delete JSON/cache files in reference/logs that have no corresponding source file.

    Also cleans up orphan mineru_output directories whose source PDF no longer exists.
    Considers both PDF and Office source files when determining orphans.
    """
    valid_stems = {p.stem for p in pdfs}
    if office_files:
        valid_stems |= {p.stem for p in office_files}
    # Keep merged/derived knowledge base files even though they do not
    # correspond to a single PDF stem.
    reserved_reference_files = {"knowledge_base.json", "commandtree_base.json"}
    
    # 1. Cleanup reference/*.json
    if REFERENCE_DIR.exists():
        for json_file in REFERENCE_DIR.glob("*.json"):
            if json_file.name in reserved_reference_files:
                continue
            if json_file.stem not in valid_stems:
                try:
                    logger.info("[cleanup] removing orphan file: %s", json_file.name)
                    json_file.unlink()
                except Exception as e:
                    logger.warning("[cleanup] failed to remove %s: %s", json_file.name, e)

    # 2. Cleanup logs/*.cache.json
    if LOG_DIR.exists():
        for cache_file in LOG_DIR.glob("*.cache.json"):
            # cache file is stem.cache.json
            stem = cache_file.name.replace(".cache.json", "")
            if stem not in valid_stems:
                 try:
                    logger.info("[cleanup] removing orphan cache: %s", cache_file.name)
                    cache_file.unlink()
                 except Exception as e:
                    logger.warning("[cleanup] failed to remove %s: %s", cache_file.name, e)

    # 3. Backup orphan mineru_output directories (source PDF removed)
    if MINERU_OUTPUT_DIR.exists():
        for task_dir in MINERU_OUTPUT_DIR.iterdir():
            if not task_dir.is_dir():
                continue
            if task_dir.name not in valid_stems:
                try:
                    backup_dest = MINERU_BACKUP_DIR / task_dir.name
                    if backup_dest.exists():
                        shutil.rmtree(backup_dest)
                    backup_dest.parent.mkdir(parents=True, exist_ok=True)
                    logger.info("[backup] moving orphan mineru_output to backup: %s/", task_dir.name)
                    shutil.move(str(task_dir), str(backup_dest))
                except Exception as e:
                    logger.warning("[backup] failed to move %s: %s", task_dir.name, e)


async def run_procurement_document_pipeline() -> None:
    load_inagent_env()
    _refresh_mineru_vllm_settings()
    # 每次完整跑管线重置主日志，避免 FileHandler 默认 append 导致多轮运行后
    # 「[完成]」等行累积（procurement_quality_diagnostic 以计数=1 为健康基线）。
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        LOG_FILE.write_text("", encoding="utf-8")
    except OSError:
        pass
    _setup_logging()
    
    # 调试：输出关键环境变量
    logger.info(
        "[config] HUNYUAN_API_BASE_URL=%s",
        cfg_str("llm.hunyuan.base_url", "NOT SET", env="HUNYUAN_API_BASE_URL"),
    )
    logger.info(
        "[config] HUNYUAN_MODEL=%s",
        cfg_str("llm.hunyuan.model", "NOT SET", env="HUNYUAN_MODEL"),
    )
    logger.info(
        "[config] LLM_GATEWAY_BASE_URL=%s",
        cfg_str("llm.gateway.base_url", "NOT SET", env="LLM_GATEWAY_BASE_URL"),
    )
    
    # 清除 LLM 配置缓存，确保使用最新的环境变量
    try:
        import sys
        if 'INAGENT.utils.llm_config' in sys.modules:
            # 清除模块级单例的缓存
            llm_config_module = sys.modules['INAGENT.utils.llm_config']
            if hasattr(llm_config_module, '_llm_config_instance') and llm_config_module._llm_config_instance:
                llm_config_module._llm_config_instance.clear_cache()
                logger.info("[config] LLM 配置缓存已清除")
            # 强制重新加载环境变量到模块
            llm_config_module.load_inagent_env()
            logger.info("[config] 已重新加载环境变量")
    except Exception as e:
        logger.warning("[config] 清除 LLM 配置缓存失败: %s", e)
    
    os.environ.setdefault("MINERU_MODEL_SOURCE", "modelscope")
    logger.info(
        "[config] MINERU_MODEL_SOURCE=%s",
        os.environ.get("MINERU_MODEL_SOURCE"),
    )
    logger.info("[mode] 采购管线：reference 落盘 + merge（不含农民/农场主/质检）")
    _setup_mineru_config()

    if _mp.USE_DOCKER_VLLM:
        candidate_urls = [_mp.DOCKER_VLLM_URL]
        if _mp.DOCKER_VLLM_URL.endswith(":8000"):
            candidate_urls.append("http://127.0.0.1:30000")

        logger.info(
            "[vllm] 正在探测 VLLM 服务状态 (/v1/models 超时: %ss, 推理探针超时: %ss)...",
            VLLM_MODELS_TIMEOUT,
            VLLM_INFER_TIMEOUT,
        )
        ok = False
        error = None
        for candidate in candidate_urls:
            logger.info("[vllm] 尝试连接: %s", candidate)
            models_url = _build_models_url(candidate)
            ok, error = _check_url_once(models_url, timeout=VLLM_MODELS_TIMEOUT)
            if not ok:
                continue
            model_name = _fetch_vllm_model_name(candidate, timeout=VLLM_MODELS_TIMEOUT)
            if not model_name:
                error = "model list empty"
                ok = False
                continue

            # Some MinerU vLLM deployments host VLM models and may be slow on
            # cold start or not accept trivial text-only probes. Treat the
            # inference probe as best-effort; /v1/models is the source of truth
            # for connectivity.
            if not _probe_vllm_completion(
                candidate, model_name, timeout=VLLM_INFER_TIMEOUT
            ):
                logger.warning(
                    "[vllm] 推理探针未通过(可能是冷启动/多模态模型限制)，但 /v1/models 可达；继续使用该地址。"
                )
            _mp.DOCKER_VLLM_URL = candidate
            os.environ["MINERU_VLM_MODEL"] = model_name
            logger.info("[vllm] 使用可用地址: %s, 模型: %s", _mp.DOCKER_VLLM_URL, model_name)
            ok = True
            break
        if not ok:
            logger.warning(
                "[vllm] 服务不可用: %s。",
                error or "unknown error",
            )
            if not _prompt_continue_without_vllm():
                logger.info("用户取消操作。")
                sys.exit(0)
            _mp.USE_DOCKER_VLLM = False
    else:
        logger.info(
            "[mineru] 未启用本地 vLLM（auto_convert.vllm.enable / AUTO_CONVERT_USE_MINERU_VLLM）；"
            "MinerU 使用 CLI 默认后端（可与 MinerU 云端 API 或本地模型配合，无需探测 127.0.0.1 vLLM）。"
        )

    # LLM Gateway preflight: probe real /chat/completions
    global _LLM_GATEWAY_AVAILABLE
    if _LLM_GATEWAY_AVAILABLE is None:
        runtime = _get_llm_gateway_runtime()
        base_url = str(runtime.get("base_url") or "")
        model = str(runtime.get("model") or "")

        # MINERU_VLM_MODEL 已在 vLLM 探测阶段写入 os.environ，此处无需重复探测

        ok, detail = _probe_llm_gateway_chat(
            base_url=base_url,
            api_key=str(runtime.get("api_key") or ""),
            model=model,
            timeout=float(runtime.get("timeout") or NET_TIMEOUT),
        )
        _LLM_GATEWAY_AVAILABLE = ok
        if not ok:
            logger.warning(
                "[gateway] 预检失败 (base_url=%s, model=%s): %s",
                base_url,
                model,
                detail,
            )
            if not _prompt_continue_on_error("gateway", detail):
                logger.info("用户取消操作。")
                sys.exit(0)

    if not DOC_LOCAL_DIR.exists():
        logger.error("knowledge_base not found: %s", DOC_LOCAL_DIR)
        raise SystemExit(1)

    pdfs = _iter_pdf_files()
    office_files_early = _iter_office_files()
    text_files_early = _iter_text_files()
    _cleanup_orphan_files(pdfs, office_files_early + text_files_early)
    _cleanup_old_logs()

    if not pdfs and not office_files_early and not text_files_early:
        logger.info("no pdf/office/text files found under knowledge_base, nothing to do.")
        return

    MINERU_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ============================================================
    # PDF 处理（MinerU）
    # ============================================================
    if pdfs:
        # convert_one：默认云端优先；本地 CLI 回退见 auto_convert.mineru.cloud.allow_local_fallback
        mineru_cmd = cfg_str("auto_convert.mineru.cli", "", env="MINERU_CLI")
        if not mineru_cmd:
            # 兼容两种目录布局：
            # 1) <repo>/INAGENT/mineru/venv/Scripts/mineru.exe
            # 2) <repo>/mineru/venv/Scripts/mineru.exe
            candidates = [
                BASE_DIR.parent / "mineru" / "venv" / "Scripts" / "mineru.exe",
                BASE_DIR.parent.parent / "mineru" / "venv" / "Scripts" / "mineru.exe",
            ]
            picked = next((c for c in candidates if c.exists()), None)
            mineru_cmd = str(picked) if picked else "mineru"
        reader = LocalMinerUReader(
            output_dir=str(MINERU_OUTPUT_DIR),
            mineru_command=mineru_cmd,
        )

        logger.info("Found %d pdf(s) under %s", len(pdfs), DOC_LOCAL_DIR)
        logger.info("Using MinerU command: %s", mineru_cmd)
        logger.info(
            "MinerU CLI default backend (enable auto_convert.vllm.enable or "
            "AUTO_CONVERT_USE_MINERU_VLLM=1 for local hybrid-http-client + vLLM)"
        )
        max_concurrent_files = cfg_int(
            "auto_convert.parallel.max_files",
            3,
            env="AUTO_CONVERT_MAX_FILES",
        )
        sem = asyncio.Semaphore(max_concurrent_files)

        # Support page limit for testing (via environment variable)
        max_pages_test = cfg_str(
            "auto_convert.parallel.max_pages_test",
            "",
            env="AUTO_CONVERT_MAX_PAGES_TEST",
        )
        max_pages_limit = None
        if max_pages_test:
            try:
                max_pages_limit = int(max_pages_test)
                logger.info("TEST MODE: Processing only first %d pages of each PDF", max_pages_limit)
            except ValueError:
                logger.warning("Invalid AUTO_CONVERT_MAX_PAGES_TEST value: %s", max_pages_test)

        allow_local_mineru_fb = cfg_bool(
            "auto_convert.mineru.cloud.allow_local_fallback",
            False,
            env="MINERU_ALLOW_LOCAL_MINERU_FALLBACK",
        )

        async def _protected_convert(p):
            async with sem:
                await convert_one(
                    reader,
                    p,
                    max_pages=max_pages_limit,
                    allow_local_mineru_fallback=allow_local_mineru_fb,
                )

        tasks = [_protected_convert(p) for p in pdfs]
        logger.info("Starting conversion with file_concurrency=%d", max_concurrent_files)
        
        if tasks:
            await asyncio.gather(*tasks)
    else:
        logger.info("no pdf files found, skipping MinerU phase.")

    # ============================================================
    # Office 文档处理（docx/xlsx/doc/xls/pptx/ppt）
    # ============================================================
    # MinerU 云端 API 原生支持 pdf/doc/docx/ppt/pptx，输出与 PDF 完全一致（块粒度）。
    # • 可走 MinerU 的格式（文档类）：.docx/.doc/.pptx/.ppt → 必须走 MinerU 云端
    # • 不走 MinerU 的格式（表格类）：.xlsx/.xls → 走 testlist_parser
    MINERU_OFFICE_EXTS = {".docx", ".doc", ".pptx", ".ppt"}
    SPREADSHEET_EXTS = {".xlsx", ".xls"}

    office_files = office_files_early
    if office_files:
        logger.info("=" * 80)
        logger.info("[office] 发现 %d 个 Office 文档，开始处理...", len(office_files))

        # 分流：表格类走 testlist_parser，文档类走 MinerU（无回退）
        doc_files = [f for f in office_files if f.suffix.lower() in MINERU_OFFICE_EXTS]
        spreadsheet_files = [f for f in office_files if f.suffix.lower() in SPREADSHEET_EXTS]

        # 表格类（xlsx/xls）—— testlist_parser
        for ofile in spreadsheet_files:
            convert_office_file(ofile)

        # 文档类（docx/doc/pptx/ppt）—— 必须走 MinerU 云端，不可用则报错终止
        if doc_files:
            if not (_use_mineru_cloud() and bool(_mp._mineru_cloud_token())):
                raise RuntimeError(
                    f"发现 {len(doc_files)} 个文档文件需要 MinerU 云端处理，"
                    "但 MinerU 云端不可用（未配置或 token 为空）。请配置 MINERU_CLOUD_TOKEN。"
                )
            logger.info(
                "[office-mineru] %d 个文档文件走 MinerU 管线", len(doc_files)
            )
            doc_tasks = [_protected_convert(f) for f in doc_files]
            await asyncio.gather(*doc_tasks)

        logger.info("[office] Office 文档处理完成")
    else:
        logger.info("[office] 未发现 Office 文档，跳过")

    # ============================================================
    # TXT 文档处理
    # ============================================================
    text_files = _iter_text_files()
    if text_files:
        logger.info("=" * 80)
        logger.info("[text] 发现 %d 个 TXT 文档，开始处理...", len(text_files))
        for tfile in text_files:
            try:
                convert_text_file(tfile)
            except Exception as e:
                logger.error("[text] 处理失败 %s: %s", tfile.name, e)
        logger.info("[text] TXT 文档处理完成")
    else:
        logger.info("[text] 未发现 TXT 文档，跳过")

    # Ensure enhanced metadata is applied even when cached procurement outputs are reused.
    # Procurement阶段只负责文档块落盘与基础/规则元数据增强，不再编排农民、农场主或质检。
    try:
        for json_file in REFERENCE_DIR.glob("*.json"):
            if json_file.name == "knowledge_base.json":
                continue
            try:
                blocks = json.loads(json_file.read_text(encoding="utf-8"))
                if isinstance(blocks, list) and blocks:
                    config = _load_project_config()
                    meta_rules = config.get("metadata_rules", {})
                    protocol_map = meta_rules.get("protocol_types", {})
                    command_prefixes = meta_rules.get("command_prefixes", {})

                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        meta = block.get("metadata")
                        if not isinstance(meta, dict):
                            meta = {}
                            block["metadata"] = meta
                        # 统一语义：source_file 始终指向当前落盘 JSON 文件名。
                        meta["source_file"] = json_file.name
                        meta["document_category"] = "unknown"
                        meta.pop("product_module", None)
                        content = str(block.get("page_content") or "")
                        lower_text = content.lower()

                        if not meta.get("protocol_type"):
                            found_protocols = _match_protocols_word_boundary(
                                protocol_map, content
                            )
                            if found_protocols:
                                meta["protocol_type"] = found_protocols

                        if not meta.get("command_prefix"):
                            for prefix, keywords in command_prefixes.items():
                                if any(k.lower() in lower_text for k in keywords):
                                    meta["command_prefix"] = prefix
                                    break

                    _enhance_metadata_with_function_index(blocks)

                    for _blk in blocks:
                        if not isinstance(_blk, dict):
                            continue
                        _meta = _blk.get("metadata") or {}
                        _title = (_meta.get("section_title") or "").strip()
                        if not _title or len(_title) < 2:
                            continue
                        _content = str(_blk.get("page_content") or "")
                        if not _content.startswith(_title):
                            _blk["page_content"] = _title + "\n" + _content

                    json_file.write_text(
                        json.dumps(blocks, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            except Exception as exc:
                logger.warning("Failed to post-enhance %s: %s", json_file.name, exc)
    except Exception as exc:
        logger.warning("Post-enhance pass failed: %s", exc)

    logger.info("=" * 80)
    logger.info("[merge] 开始合并 procurement knowledge_base.json...")
    try:
        from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base

        reference_dir = REFERENCE_DIR
        output_file = reference_dir / "knowledge_base.json"
        merge_knowledge_base(
            reference_dir,
            output_file,
            deduplicate=True,
            logger=logger,
            validate_ingest=False,
        )
        logger.info("[ok] procurement merged knowledge base -> %s", output_file)
    except Exception as exc:
        logger.warning("Failed to merge knowledge_base.json: %s", exc)

    logger.info("=" * 80)
    logger.info("[完成] procurement stage finished; farmer / farm-owner / quality 已从 auto_convert 剥离")
    logger.info("=" * 80)


# 历史兼容：``main`` 即采购文档管线（MinerU 批处理等），新代码请优先
# ``from INAGENT.data_tools.procurement_ingest import main``。
main = run_procurement_document_pipeline


if __name__ == "__main__":
    try:
        asyncio.run(run_procurement_document_pipeline())
    except KeyboardInterrupt:
        try:
            _setup_logging()
        except Exception:
            pass
        logger.warning("auto_convert interrupted by user (KeyboardInterrupt)")
    except Exception as exc:
        # Best-effort logging: make sure exceptions are recorded in LOG_FILE.
        try:
            _setup_logging()
        except Exception:
            pass
        logger.exception("auto_convert failed: %s", exc)
        raise