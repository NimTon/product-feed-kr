"""pf_scrape_skip：失效商品永久跳过表。"""

from __future__ import annotations

import sqlite3

from product_feed_kr.db.store_sqlite import (
    _sqlite_clear_scrape_skip_unlocked,
    connect_sqlite_path,
    ensure_sqlite_schema_at,
    sqlite_load_scrape_skip_goods_ids,
    sqlite_record_scrape_skip,
)


def test_migrate_legacy_scrape_skip_schema(tmp_path) -> None:
    """旧表 skip_reason/created_at → 新列 reason/first_seen_at。"""
    db = tmp_path / "legacy.db"
    conn = connect_sqlite_path(db)
    conn.execute(
        """
        CREATE TABLE pf_scrape_skip (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          album_id TEXT NOT NULL,
          goods_id TEXT NOT NULL,
          goods_url TEXT,
          skip_reason TEXT NOT NULL DEFAULT '',
          errcode INTEGER,
          errmsg TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
          updated_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
          UNIQUE (album_id, goods_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO pf_scrape_skip (album_id, goods_id, skip_reason, created_at, updated_at)
        VALUES ('_a', '_g', 'popups_invalid', '2026-01-01 00:00:00', '2026-01-02 00:00:00')
        """
    )
    conn.commit()

    ensure_sqlite_schema_at(conn, db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pf_scrape_skip)").fetchall()}
    assert "reason" in cols
    assert "hit_count" in cols

    row = conn.execute(
        "SELECT reason, first_seen_at, last_seen_at, hit_count FROM pf_scrape_skip "
        "WHERE album_id = ? AND goods_id = ?",
        ("_a", "_g"),
    ).fetchone()
    assert row[0] == "popups_invalid"
    assert row[1] == "2026-01-01 00:00:00"
    assert row[2] == "2026-01-02 00:00:00"
    assert int(row[3]) == 1

    sqlite_record_scrape_skip(conn, "_a", "_g", reason="list_incomplete")
    row2 = conn.execute(
        "SELECT reason, hit_count FROM pf_scrape_skip WHERE album_id = ? AND goods_id = ?",
        ("_a", "_g"),
    ).fetchone()
    assert row2[0] == "list_incomplete"
    assert int(row2[1]) == 2

    conn.close()


def test_scrape_skip_record_and_load(tmp_path) -> None:
    db = tmp_path / "t.db"
    conn = connect_sqlite_path(db)
    ensure_sqlite_schema_at(conn, db)
    album = "_album1"
    gid = "_goods_invalid"

    assert sqlite_load_scrape_skip_goods_ids(conn, album) == set()

    sqlite_record_scrape_skip(
        conn,
        album,
        gid,
        reason="popups_invalid",
        errcode=2530002,
        errmsg="商品已失效",
        goods_url="https://example/g",
    )
    assert sqlite_load_scrape_skip_goods_ids(conn, album) == {gid}

    sqlite_record_scrape_skip(
        conn,
        album,
        gid,
        reason="popups_invalid",
        errcode=2530002,
    )
    row = conn.execute(
        "SELECT hit_count FROM pf_scrape_skip WHERE album_id = ? AND goods_id = ?",
        (album, gid),
    ).fetchone()
    assert row is not None
    assert int(row[0]) == 2

    assert _sqlite_clear_scrape_skip_unlocked(conn, album, gid) == 1
    conn.commit()
    assert sqlite_load_scrape_skip_goods_ids(conn, album) == set()


def test_rescrape_clears_skip(tmp_path) -> None:
    db = tmp_path / "t2.db"
    conn = connect_sqlite_path(db)
    ensure_sqlite_schema_at(conn, db)
    album = "_album1"
    gid = "_goods_x"

    conn.execute(
        """
        INSERT INTO pf_store_item (
          album_id, goods_id, tag_id, goods_url, commodity_title
        ) VALUES (?, ?, 0, 'https://x', 't')
        """,
        (album, gid),
    )
    conn.commit()

    sqlite_record_scrape_skip(conn, album, gid, reason="popups_invalid")
    assert gid in sqlite_load_scrape_skip_goods_ids(conn, album)

    from product_feed_kr.db.store_sqlite import sqlite_request_item_rerun

    row_id = conn.execute(
        "SELECT id FROM pf_store_item WHERE album_id = ? AND goods_id = ?",
        (album, gid),
    ).fetchone()[0]
    sqlite_request_item_rerun(conn, int(row_id), "rescrape")
    assert gid not in sqlite_load_scrape_skip_goods_ids(conn, album)

    conn.close()
