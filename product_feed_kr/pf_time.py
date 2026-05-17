"""入库时间统一为东八区（UTC+8 / Asia/Shanghai）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 固定 +8，不依赖系统时区
TZ_CST8 = timezone(timedelta(hours=8))

# SQLite 表达式：UTC now 再加 8 小时
SQLITE_NOW_CST8 = "datetime('now', '+8 hours')"


def now_cst8() -> datetime:
    return datetime.now(TZ_CST8)


def now_cst8_iso() -> str:
    """写入 DB / 业务字段用的 ISO 8601 时间（含 +08:00 偏移）。"""
    return now_cst8().isoformat(timespec="seconds")
