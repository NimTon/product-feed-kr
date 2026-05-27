"""跨进程单实例：防止同一任务多开（多窗口 / 多 bat 同时跑同一 Python 入口）。

使用 ``data/runlocks/<name>.lock``（filelock）。冲突时向 stderr 打印说明并 ``SystemExit(11)``，
与上架失败返回码 2、重启返回码 75 区分。

设置环境变量 ``PRODUCT_FEED_SKIP_SINGLETON_LOCK=1``（或 ``true``）可跳过加锁（仅排障用）。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from product_feed_kr.common.seven17_config import getenv

from product_feed_kr._paths import REPO_ROOT as _REPO_ROOT
_LOCK_DIR = _REPO_ROOT / "data" / "runlocks"

# 与 seven17_upload 失败码 2、EXIT_RESTART_FRESH_DATA 75 区分
EXIT_SINGLETON_CONFLICT = 11


def _lock_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip() or "default")
    return _LOCK_DIR / f"{safe}.lock"


@contextmanager
def single_instance_lock(name: str) -> Iterator[None]:
    """未获得锁时打印错误并 ``raise SystemExit(11)``。"""
    raw = (getenv("PRODUCT_FEED_SKIP_SINGLETON_LOCK", "") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        yield
        return

    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = _lock_path(name)
    fl = FileLock(str(path))
    try:
        fl.acquire(timeout=0)
    except Timeout:
        p = path.resolve()
        print(
            f"[singleton] task={name!r}: another process is already running this entry.\n"
            f"  lock file: {p}\n"
            f"  how to fix: close the other terminal/window, or if it crashed delete the .lock file "
            f"after confirming no python.exe is still running this module.\n"
            f"  skip lock (not recommended): set PRODUCT_FEED_SKIP_SINGLETON_LOCK=1",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_SINGLETON_CONFLICT) from None
    try:
        yield
    finally:
        try:
            fl.release()
        except OSError:
            pass
