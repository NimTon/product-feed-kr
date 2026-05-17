"""微猫店铺 SQLite 记录与 ``detail_response`` 的共用解析。"""

from __future__ import annotations

from typing import Any


def commodity_from_wecatalog_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """从单条 ``products[]`` / ``pf_store_item`` 行取出 ``detail_response.result.commodity``。"""
    dr = record.get("detail_response")
    if not isinstance(dr, dict):
        return None
    res = dr.get("result")
    if not isinstance(res, dict):
        return None
    com = res.get("commodity")
    return com if isinstance(com, dict) else None
