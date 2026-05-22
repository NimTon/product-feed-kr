"""抓取阶段 CNY→KRW 换算（千韩元取整）。"""
from __future__ import annotations

from product_feed_kr.cny_krw_rate import cny_amount_to_krw_won_str, cny_listing_amount_to_krw_won_str
from product_feed_kr.wecatalog_scrape_fields import apply_price_krw_from_cny


def test_krw_rounds_to_thousand_won() -> None:
    assert cny_listing_amount_to_krw_won_str("100", 200.0) == "20000"
    assert cny_listing_amount_to_krw_won_str("340", 195.5) == "66000"


def test_apply_price_krw_from_cny_on_fields() -> None:
    fields = {"price_cny": "100"}
    apply_price_krw_from_cny(fields, krw_per_cny=200.0)
    assert fields["price_krw"] == "20000"


def test_cny_amount_none_when_no_cny() -> None:
    assert cny_amount_to_krw_won_str("", 200.0) is None
    assert cny_amount_to_krw_won_str("0", 200.0) is None
