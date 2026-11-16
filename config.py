"""
集中读取本机配置。

优先级（高 → 低）：真实环境变量 → 项目根 `.env` → 代码默认值。
真实环境变量始终优先，所以临时覆盖（如 `$env:SUBMIT_INDEX_BASE="0"`）
不必改动 `.env`。空白值视为未设置，会回落到 `.env`。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

try:
    from dotenv import dotenv_values
except ImportError:  # 未安装 dotenv 时只读环境变量
    dotenv_values = None

ENV_PATH = Path(__file__).resolve().with_name(".env")
_file_values: Optional[Dict[str, str]] = None


def _read_file() -> Dict[str, str]:
    """读取 `.env` 的键值，只读一次。"""
    global _file_values
    if _file_values is None:
        if dotenv_values is not None and ENV_PATH.exists():
            _file_values = dict(dotenv_values(ENV_PATH))
        else:
            _file_values = {}
    return _file_values


def load_env() -> None:
    """把 `.env` 载入进程环境，供未走本模块的依赖（如 transformers）读取。"""
    values = _read_file()
    for key, value in values.items():
        if not value:
            continue
        if not os.environ.get(key, "").strip():
            os.environ[key] = value


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    """读取配置项。空白值视为未设置，继续向下一层回落。"""
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    file_value = _read_file().get(name)
    if file_value is not None and str(file_value).strip():
        return str(file_value).strip()
    return default


def get_float(name: str, default: float) -> float:
    """读取浮点配置，非法值回落到默认值。"""
    raw = get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def get_int(name: str, default: int) -> int:
    """读取整数配置，非法值回落到默认值。"""
    raw = get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def api_key(name: str) -> str:
    """读取密钥；未配置时返回空串，由调用方决定降级行为。"""
    return get(name, "") or ""
