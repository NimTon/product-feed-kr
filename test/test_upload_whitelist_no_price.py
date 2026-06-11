"""无价白名单：上架不换算、不填 it_price。"""

from __future__ import annotations

from product_feed_kr.seven17.seven17_upload import (
    _upload_db_write_needed,
    _upload_skip_price_fill,
)


def test_upload_skip_price_fill_whitelist_no_krw(monkeypatch) -> None:
    monkeypatch.setattr(
        "product_feed_kr.listing.listing_llm_enrich.record_is_no_price_allowed_by_map_category",
        lambda rec: rec.get("wecatalog_tag") == "구찌",
    )
    rec = {"wecatalog_group": "女士包专区", "wecatalog_tag": "구찌", "price_krw": None}
    assert _upload_skip_price_fill(rec, "")
    assert not _upload_skip_price_fill(rec, "120000")


def test_upload_skip_price_fill_not_whitelist() -> None:
    rec = {"wecatalog_group": "其他", "wecatalog_tag": "x", "price_krw": None}
    assert not _upload_skip_price_fill(rec, "")


def test_upload_db_write_skipped_for_whitelist_no_price() -> None:
    assert not _upload_db_write_needed("whitelist_no_price")
