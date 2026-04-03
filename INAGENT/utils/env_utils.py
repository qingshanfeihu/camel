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
from __future__ import annotations

import logging
import os
import urllib.request
import json
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

from dotenv import load_dotenv

from INAGENT.config.project_config import cfg_str

try:
    from langchain_core.documents import Document
except ImportError:
    class Document:
        def __init__(self, page_content, metadata=None):
            self.page_content = page_content
            self.metadata = metadata or {}

        def __repr__(self):
            return f"Document(page_content='{self.page_content[:20]}...', metadata={self.metadata})"

logger = logging.getLogger(__name__)

# 缓存已记录的日志，避免重复输出
_env_logged = False


def load_inagent_env(override: bool = True) -> Path:
    r"""Load INAGENT environment variables from a single .env file.

    Args:
        override (bool): Whether to override existing env vars.
            (default: :obj:`True`)

    Returns:
        Path: The .env path used for loading.
    """
    inagent_env = Path(__file__).resolve().parent.parent / ".env"
    if not inagent_env.exists():
        logger.warning("INAGENT .env not found at %s", inagent_env)
        return inagent_env

    project_root = Path(__file__).parent.parent
    env_candidates = []
    try:
        for path in project_root.rglob(".env"):
            try:
                if path.is_file():
                    env_candidates.append(path)
            except (FileNotFoundError, OSError):
                # Ignore broken symlinks or transient filesystem entries
                continue
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Failed to scan .env files under %s: %s", project_root, exc)
    
    # 只在第一次检测到多个.env文件时记录日志，避免重复
    global _env_logged
    if len(env_candidates) > 1 and not _env_logged:
        logger.info(
            "Multiple .env files detected; using INAGENT/.env only: %s",
            ", ".join(str(p) for p in env_candidates),
        )
        _env_logged = True

    load_dotenv(dotenv_path=inagent_env, override=override)
    os.environ.setdefault("INAGENT_ENV_PATH", str(inagent_env))
    return inagent_env


def resolve_env_placeholder(value: Optional[str]) -> Optional[str]:
    r"""Resolve ENV placeholders like ${VAR} or ENV:VAR to env values."""
    if not value:
        return value
    if value.startswith("ENV:"):
        return os.getenv(value[4:])
    if value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1])
    return value


def is_placeholder_value(value: Optional[str]) -> bool:
    if not value:
        return True
    lowered = value.strip().lower()
    return lowered in {
        "your_siliconflow_api_key_here",
        "your_api_key_here",
        "changeme",
        "placeholder",
        "none",
        "null",
    }


def require_gateway_api_key() -> str:
    api_key = cfg_str("llm.siliconflow.api_key", "", env="SILICONFLOW_API_KEY")
    if is_placeholder_value(api_key):
        raise RuntimeError(
            "SILICONFLOW_API_KEY 未配置或仍为占位符。"
            "请在 INAGENT/.env 中填写真实的 SiliconFlow API Key。"
        )
    return api_key


def require_siliconflow_api_key() -> str:
    """兼容旧命名：等价于 require_gateway_api_key。"""
    return require_gateway_api_key()


def get_product_name() -> str:
    r"""Get product display name used by prompts.

    Reads ``INAGENT_PRODUCT_NAME`` from environment. If it is missing or
    empty, fall back to the historical default name.
    """
    name = cfg_str("app.product_name", "", env="INAGENT_PRODUCT_NAME").strip()
    if name:
        return name
    return "NSAE (InfosecOS) 负载均衡器"


def load_knowledge_base(knowledge_dir: Path) -> List[Document]:
    r"""Load precomputed knowledge blocks from JSON files.

    Priority: If knowledge_base.json exists, use it (merged unified file).
    Otherwise, load all individual JSON files.

    Args:
        knowledge_dir (Path): Directory containing JSON knowledge files.

    Returns:
        List[Document]: Loaded LangChain Document objects.
    """
    if not knowledge_dir.exists():
        raise RuntimeError(f"Knowledge directory not found: {knowledge_dir}")

    docs: List[Document] = []

    # Priority 1: Use merged knowledge_base.json if exists (unified single source)
    merged_file = knowledge_dir / "knowledge_base.json"
    if merged_file.exists():
        logger.info(f"Loading merged knowledge base: {merged_file.name}")
        try:
            with open(merged_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, list):
                    logger.warning(f"{merged_file} is not a list, falling back to individual files")
                else:
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        content = item.get("page_content", "")
                        metadata = item.get("metadata", {})
                        if not isinstance(metadata, dict):
                            metadata = {}
                        
                        # Ensure source is set
                        metadata.setdefault("source", str(merged_file))
                        
                        docs.append(Document(page_content=content, metadata=metadata))
                    logger.info(f"Loaded {len(docs)} chunks from merged knowledge_base.json")
                    return docs
        except Exception as e:
            logger.warning(f"Failed to load merged file {merged_file}: {e}, falling back to individual files")
    
    # Priority 2: Load all individual JSON files (excluding knowledge_base.json)
    logger.info("Loading individual JSON files from knowledge directory")
    for json_path in sorted(knowledge_dir.glob("*.json")):
        # Skip the merged file if it exists but failed to load
        if json_path.name == "knowledge_base.json":
            continue
            
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, list):
                    logger.warning(f"Skipping {json_path}: Expected list, got {type(data)}")
                    continue
                
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    content = item.get("page_content", "")
                    metadata = item.get("metadata", {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    
                    # Ensure source is set
                    metadata.setdefault("source", str(json_path))
                    
                    docs.append(Document(page_content=content, metadata=metadata))
        except Exception as e:
            logger.error(f"Failed to load {json_path}: {e}")

    logger.info(f"Loaded {len(docs)} chunks from {len(list(knowledge_dir.glob('*.json')))} individual files")
    return docs


def get_baidu_access_token() -> str:
    """
    兼容旧接口占位函数。
    
    系统已从百度千帆迁移到腾讯混元 Hunyuan，本函数不再实际请求任何 Baidu 接口，
    仅保留以避免历史代码引用报错，始终返回空字符串。
    """
    logger.warning(
        "get_baidu_access_token() 已弃用：系统已切换到腾讯混元 Hunyuan，"
        "不再通过 Baidu 千帆获取 Access Token。"
    )
    return ""
