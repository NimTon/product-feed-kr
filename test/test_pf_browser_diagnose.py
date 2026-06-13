"""pf_browser 微猫商品不上架诊断。"""

from __future__ import annotations

import sqlite3

from product_feed_kr.pf_browser.diagnose import (
    diagnose_wecatalog_goods,
    parse_wecatalog_goods_ref,
    scrape_skip_reason_zh,
)
from product_feed_kr.db.store_sqlite import ensure_sqlite_schema


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_sqlite_schema(conn)
    return conn


def test_parse_wecatalog_goods_url() -> None:
    aid, gid = parse_wecatalog_goods_ref(
        "https://www.wecatalog.cn/weshop/goods/_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg/_dI0qfD6FBI8YWUGnGmoc1Er281Tw6Y_4DtHyGcg",
    )
    assert aid == "_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg"
    assert gid == "_dI0qfD6FBI8YWUGnGmoc1Er281Tw6Y_4DtHyGcg"


def test_parse_wecatalog_goods_id_only() -> None:
    aid, gid = parse_wecatalog_goods_ref("_dI0qfD6FBI8YWUGnGmoc1Er281Tw6Y_4DtHyGcg")
    assert aid is None
    assert gid == "_dI0qfD6FBI8YWUGnGmoc1Er281Tw6Y_4DtHyGcg"


def test_diagnose_not_in_db(monkeypatch) -> None:
    conn = _mem_conn()
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.diagnose.connect_sqlite",
        lambda: conn,
    )
    out = diagnose_wecatalog_goods(
        "https://www.wecatalog.cn/weshop/goods/_album/_goods_missing",
        live_probe=False,
    )
    assert out["pipeline_status"] == "not_in_db"
    assert out["in_store"] is False
    assert out["scrape_skipped"] is False
    assert out["steps"][0]["status"] == "fail"
    assert "尚未写入商品库" in out["conclusion_zh"]


def test_diagnose_scrape_skipped(monkeypatch) -> None:
    conn = _mem_conn()
    conn.execute(
        """
        INSERT INTO pf_scrape_skip (album_id, goods_id, reason, goods_url)
        VALUES ('_album', '_gid_skip', 'list_no_price', 'https://example/g')
        """,
    )
    conn.commit()
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.diagnose.connect_sqlite",
        lambda: conn,
    )
    out = diagnose_wecatalog_goods("https://www.wecatalog.cn/weshop/goods/_album/_gid_skip", live_probe=False)
    assert out["pipeline_status"] == "scrape_skipped"
    assert out["scrape_skipped"] is True
    assert out["steps"][0]["reason_code"] == "list_no_price"
    assert scrape_skip_reason_zh("list_no_price") in out["conclusion_zh"]


def test_diagnose_in_db_not_uploaded(monkeypatch) -> None:
    conn = _mem_conn()
    conn.execute(
        """
        INSERT INTO pf_store_item (
          album_id, goods_id, tag_id, commodity_title, can_process, can_upload,
          uploaded_to_platform, goods_url, commodity_image_urls_json
        ) VALUES (
          '_album', '_gid_db', 1, '测试商品', 1, 0, 0, 'https://example/g',
          '["https://example.com/thumb.jpg"]'
        )
        """,
    )
    conn.commit()
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.diagnose.connect_sqlite",
        lambda: conn,
    )
    out = diagnose_wecatalog_goods("https://www.wecatalog.cn/weshop/goods/_album/_gid_db", live_probe=False)
    assert out["pipeline_status"] == "in_db_blocked"
    assert out["in_store"] is True
    assert len(out["store_items"]) == 1
    assert out["steps"][0]["status"] == "ok"
    assert out["steps"][2]["status"] == "fail"
    assert out["thumbnail_url"] == "https://example.com/thumb.jpg"
