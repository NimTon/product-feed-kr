"""微猫上架时间与 config 阈值。"""

from __future__ import annotations

from product_feed_kr.wecatalog.wecatalog_listed_at import (
    ms_to_wecatalog_listed_at_iso,
    parse_min_listed_at_threshold,
    record_listed_before_threshold,
    record_skipped_as_listed_too_old,
    time_stamp_ms_from_list_item,
    wecatalog_listed_at_iso_from_list_item,
)
from product_feed_kr.wecatalog.wecatalog_scrape_fields import fields_from_list_item


def test_time_stamp_from_list_item():
    it = {"time_stamp": 1779877426765, "title": "x", "imgsSrc": ["https://a/b.jpg"]}
    assert time_stamp_ms_from_list_item(it) == 1779877426765
    iso = wecatalog_listed_at_iso_from_list_item(it)
    assert iso == ms_to_wecatalog_listed_at_iso(1779877426765)
    fields = fields_from_list_item(it)
    assert fields["wecatalog_listed_at"] == iso


def test_parse_min_listed_at_threshold_formats():
    y = parse_min_listed_at_threshold("2026")
    ym = parse_min_listed_at_threshold("2026-05")
    ymd = parse_min_listed_at_threshold("2026-05-27")
    assert y < ym < ymd
    assert ms_to_wecatalog_listed_at_iso(y).startswith("2026-01-01")
    assert ms_to_wecatalog_listed_at_iso(ym).startswith("2026-05-01")
    assert ms_to_wecatalog_listed_at_iso(ymd).startswith("2026-05-27")


def test_record_skipped_as_listed_too_old(monkeypatch) -> None:
    monkeypatch.setattr(
        "product_feed_kr.wecatalog.wecatalog_listed_at.wecatalog_min_listed_at_ms",
        lambda: parse_min_listed_at_threshold("2026-05-27"),
    )
    old = {"wecatalog_listed_at": "2026-05-26T12:00:00+08:00"}
    new = {"wecatalog_listed_at": "2026-05-27T00:00:01+08:00"}
    empty = {}
    assert record_skipped_as_listed_too_old(old)
    assert not record_skipped_as_listed_too_old(new)
    assert not record_skipped_as_listed_too_old(empty)


def test_record_listed_before_threshold_exact_boundary():
    min_ms = parse_min_listed_at_threshold("2026-05-27")
    on_day = {"wecatalog_listed_at": ms_to_wecatalog_listed_at_iso(min_ms)}
    assert not record_listed_before_threshold(on_day, min_ms)
