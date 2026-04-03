"""Project-level config loader for INAGENT.

配置优先级:
1) 显式环境变量（若提供 env 参数）
2) project.yaml 配置值
3) 调用处默认值
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency
    yaml = None


_CACHE: Dict[str, Any] | None = None


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_str(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


def _get_by_path(data: Dict[str, Any], path: str, default: Any) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _default_config_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "INAGENT" / "config" / "project.yaml"


def load_project_config() -> Dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    config_path = Path(
        os.getenv(
            "INAGENT_PROJECT_CONFIG",
            os.getenv("BUG_TO_CASE_CONFIG", str(_default_config_path())),
        )
    )
    if not config_path.exists() or yaml is None:
        _CACHE = {}
        return _CACHE
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        _CACHE = raw if isinstance(raw, dict) else {}
    except Exception:
        _CACHE = {}
    return _CACHE


def cfg_bool(path: str, default: bool, env: str | None = None) -> bool:
    if env:
        raw = os.getenv(env)
        if raw is not None:
            return _to_bool(raw, default)
    raw_cfg = _get_by_path(load_project_config(), path, default)
    return _to_bool(raw_cfg, default)


def cfg_int(path: str, default: int, env: str | None = None) -> int:
    if env:
        raw = os.getenv(env)
        if raw is not None:
            return _to_int(raw, default)
    raw_cfg = _get_by_path(load_project_config(), path, default)
    return _to_int(raw_cfg, default)


def cfg_float(path: str, default: float, env: str | None = None) -> float:
    if env:
        raw = os.getenv(env)
        if raw is not None:
            return _to_float(raw, default)
    raw_cfg = _get_by_path(load_project_config(), path, default)
    return _to_float(raw_cfg, default)


def cfg_str(path: str, default: str, env: str | None = None) -> str:
    if env:
        raw = os.getenv(env)
        if raw is not None:
            return _to_str(raw, default)
    raw_cfg = _get_by_path(load_project_config(), path, default)
    return _to_str(raw_cfg, default)
