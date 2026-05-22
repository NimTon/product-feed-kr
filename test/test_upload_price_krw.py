"""上架韩元价：优先使用库内 price_krw。"""

from __future__ import annotations

from product_feed_kr.seven17_upload import (
    _resolve_upload_listing_price_krw,
    _upload_db_write_needed,
)


def test_resolve_price_prefers_db_krw_without_fx():
    rec = {
        "price_krw": "142000",
        "price_cny": "640",
        "commodity_min": {"price_krw": "142000", "price_raw": "640"},
    }
    krw, src = _resolve_upload_listing_price_krw(
        rec,
        llm_on=True,
        llm_data={},
        prod={"price": "640"},
        krw_per_cny=222.0,
    )
    assert krw == "142000"
    assert src == "price_krw"


def test_resolve_price_from_commodity_min_krw():
    rec = {
        "commodity_min": {"price_krw": "99000"},
        "price_cny": "500",
    }
    krw, src = _resolve_upload_listing_price_krw(
        rec,
        llm_on=False,
        llm_data={},
        prod={"price": "500"},
        krw_per_cny=200.0,
    )
    assert krw == "99000"
    assert src == "price_krw"


def test_upload_db_write_skipped_when_price_from_db():
    assert _upload_db_write_needed("price_krw") is False
    assert _upload_db_write_needed("price_cny") is True
