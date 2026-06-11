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


def test_load_products_for_upload_listed_at_order(tmp_path) -> None:
    import sqlite3
    from pathlib import Path

    from product_feed_kr.db.store_sqlite import ensure_sqlite_schema_at, sqlite_load_products_for_upload

    db_path = tmp_path / "order.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_sqlite_schema_at(conn, db_path)
    aid = "album_test"
    rows = [
        ("g_new", "2026-05-20T10:00:00+08:00"),
        ("g_old", "2026-05-01T08:00:00+08:00"),
        ("g_mid", "2026-05-10T12:00:00+08:00"),
        ("g_empty", None),
    ]
    for gid, listed in rows:
        conn.execute(
            """
            INSERT INTO pf_store_item (album_id, goods_id, tag_id, goods_url, wecatalog_listed_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (aid, gid, f"https://example/{gid}", listed),
        )
    conn.commit()
    loaded = sqlite_load_products_for_upload(conn, aid, skip_uploaded=False)
    assert [r["goods_id"] for r in loaded] == ["g_old", "g_mid", "g_new", "g_empty"]
