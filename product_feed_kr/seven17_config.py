"""seven17 站点相关配置：优先读环境变量，否则读本地 JSON。

默认配置文件路径：仓库根目录下 ``config/seven17.json``（勿提交密码）。
可通过环境变量 ``SEVEN17_CONFIG`` 指定其它路径。

示例：复制 ``config/seven17.example.json`` 为 ``config/seven17.json`` 并填写。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CONFIG_DATA: dict[str, Any] | None = None


def seven17_config_path() -> Path:
    override = os.environ.get("SEVEN17_CONFIG", "").strip()
    if override:
        return Path(override)
    root = Path(__file__).resolve().parent.parent
    return root / "config" / "seven17.json"


def load_seven17_config() -> dict[str, Any]:
    global _CONFIG_DATA
    if _CONFIG_DATA is not None:
        return _CONFIG_DATA
    path = seven17_config_path()
    data: dict[str, Any] = {}
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    _CONFIG_DATA = data
    return data


def reload_seven17_config() -> None:
    """测试或进程内改文件后可调用以重新读取。"""
    global _CONFIG_DATA
    _CONFIG_DATA = None


def getenv(key: str, default: str | None = None) -> str | None:
    """先 ``os.environ``，再配置文件；布尔与数字在 JSON 中会转成字符串。"""
    ev = os.environ.get(key)
    if ev is not None and str(ev).strip() != "":
        return str(ev).strip()
    cfg = load_seven17_config()
    if key not in cfg:
        return default
    cv = cfg[key]
    if cv is None:
        return default
    if isinstance(cv, bool):
        return "1" if cv else "0"
    s = str(cv).strip()
    return s if s else default


def getenv_required(key: str) -> str:
    v = getenv(key)
    if not v:
        p = seven17_config_path()
        raise RuntimeError(
            f"缺少配置 {key}：请设置环境变量或在 {p} 中填写（可参考 config/seven17.example.json）",
        )
    return v


def bool_env(key: str, default: bool = True) -> bool:
    raw = getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "")


# 拉取 / LLM 处理 / 上传累计完成 N 条后进程以该退出码结束，供外层脚本立即重启以刷新 DB、配置与会话。
EXIT_RESTART_FRESH_DATA = 75


def restart_after_n(key: str, default: int = 1000) -> int:
    """读配置或环境变量：本 run 完成多少条后触发「退出 75 以便重跑」。0 表示关闭。"""
    raw = getenv(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip())
    except ValueError:
        return default
    return max(0, v)
