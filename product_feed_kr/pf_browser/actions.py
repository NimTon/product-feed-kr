"""商品库浏览器：单条重跑写库。"""

from __future__ import annotations

from typing import Any

from product_feed_kr.pf_browser.queries import _finalize_items, _row_to_item
from product_feed_kr.db.store_sqlite import (
    connect_sqlite,
    ensure_sqlite_schema,
    sqlite_request_item_rerun,
)


def request_item_rerun(item_id: int, action: str) -> dict[str, Any]:
    """执行重跑并返回与列表一致的展示字段。"""
    conn = connect_sqlite()
    try:
        ensure_sqlite_schema(conn)
        row = sqlite_request_item_rerun(conn, item_id, action)
        if row is None:
            return {"ok": False, "error": "not_found"}
        item = _row_to_item(row)
        _finalize_items(conn, [item])
        return {"ok": True, "action": action, "item": item}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()
