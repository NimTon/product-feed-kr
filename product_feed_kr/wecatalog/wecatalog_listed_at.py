"""微猫列表 ``time_stamp``（毫秒）→ 上架时间；config 最早上架阈值。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from product_feed_kr.common.pf_time import TZ_CST8
from product_feed_kr.common.seven17_config import getenv as _cfg_get

_THRESHOLD_RE = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$")


def time_stamp_ms_from_raw(raw: Any) -> int | None:
    """列表项 ``time_stamp`` → 毫秒整数。"""
    if raw is None:
        return None
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return ms


def time_stamp_ms_from_list_item(item: dict[str, Any] | None) -> int | None:
    if not isinstance(item, dict):
        return None
    return time_stamp_ms_from_raw(item.get("time_stamp"))


def ms_to_wecatalog_listed_at_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=TZ_CST8).isoformat(timespec="seconds")


def wecatalog_listed_at_iso_from_list_item(item: dict[str, Any] | None) -> str | None:
    ms = time_stamp_ms_from_list_item(item)
    if ms is None:
        return None
    return ms_to_wecatalog_listed_at_iso(ms)


def parse_min_listed_at_threshold(raw: str | None) -> int | None:
    """配置 ``年`` / ``年-月`` / ``年-月-日`` → 该区间起始时刻（CST+8）的毫秒时间戳。

    例：``2026`` → 2026-01-01 00:00:00+08:00；``2026-05`` → 2026-05-01；``2026-05-27`` → 当日 0 点。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    m = _THRESHOLD_RE.match(s)
    if not m:
        raise ValueError(f"WECATALOG_MIN_LISTED_AT 格式无效: {raw!r}（应为 年 / 年-月 / 年-月-日）")
    year = int(m.group(1))
    month = int(m.group(2) or 1)
    day = int(m.group(3) or 1)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"WECATALOG_MIN_LISTED_AT 日期无效: {raw!r}")
    dt = datetime(year, month, day, 0, 0, 0, tzinfo=TZ_CST8)
    return int(dt.timestamp() * 1000)


def wecatalog_min_listed_at_ms() -> int | None:
    raw = (_cfg_get("WECATALOG_MIN_LISTED_AT") or "").strip()
    if not raw:
        return None
    return parse_min_listed_at_threshold(raw)


def record_listed_at_ms(rec: dict[str, Any] | None) -> int | None:
    if not isinstance(rec, dict):
        return None
    raw = rec.get("wecatalog_listed_at")
    if raw is None or not str(raw).strip():
        return None
    s = str(raw).strip()
    try:
        if s.isdigit():
            return time_stamp_ms_from_raw(s)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_CST8)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError, OSError):
        return None


def record_listed_before_threshold(rec: dict[str, Any], min_ms: int) -> bool:
    """有上架时间且早于阈值（不含等于）。"""
    listed_ms = record_listed_at_ms(rec)
    if listed_ms is None:
        return False
    return listed_ms < min_ms


def record_skipped_as_listed_too_old(rec: dict[str, Any]) -> bool:
    """config 设阈值且本条上架时间早于阈值时返回 True（无上架时间不跳过）。"""
    min_ms = wecatalog_min_listed_at_ms()
    if min_ms is None:
        return False
    return record_listed_before_threshold(rec, min_ms)
