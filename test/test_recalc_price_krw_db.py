"""recalc_price_krw_db 计划逻辑单测。"""

from __future__ import annotations

from product_feed_kr.tools.recalc_price_krw_db import _plan_updates


def test_plan_updates_from_cny():
    rows = [
        {"id": 1, "price_cny": "100", "price_krw": "19000"},
        {"id": 2, "price_cny": "100", "price_krw": "20000"},
        {"id": 3, "price_cny": "", "price_krw": "1000"},
    ]
    updates, stats = _plan_updates(rows, rate=200.0, force=False, clear_no_cny=False)
    assert stats["to_update"] == 1
    assert updates[0]["id"] == 1
    assert updates[0]["price_krw_new"] == "20000"
    assert stats["unchanged"] == 1
    assert stats["no_cny"] == 1


def test_plan_force_updates_same_krw():
    rows = [{"id": 1, "price_cny": "100", "price_krw": "20000"}]
    updates, stats = _plan_updates(rows, rate=200.0, force=True, clear_no_cny=False)
    assert stats["to_update"] == 1
    assert updates[0]["price_krw_new"] == "20000"
