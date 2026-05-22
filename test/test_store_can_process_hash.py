"""首图 hash 去重：can_process 每组应保留 id 最小者为 1。"""

import sqlite3

from product_feed_kr.store_sqlite import (
    _resolve_can_process_for_image_hash,
    sqlite_reconcile_can_process_dup_hashes,
)


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE pf_store_item (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          album_id TEXT NOT NULL,
          goods_id TEXT NOT NULL,
          tag_id INTEGER NOT NULL DEFAULT 0,
          can_process INTEGER NOT NULL DEFAULT 1,
          first_image_hash TEXT,
          updated_at TEXT DEFAULT 'now',
          UNIQUE(album_id, goods_id, tag_id)
        );
        """
    )
    return conn


def test_canonical_keeps_can_process_on_rescrape() -> None:
    conn = _mem_conn()
    h = "abc123"
    aid = "album1"
    conn.execute(
        "INSERT INTO pf_store_item (album_id, goods_id, tag_id, can_process, first_image_hash)"
        " VALUES (?, 'g1', 1, 1, ?)",
        (aid, h),
    )
    conn.execute(
        "INSERT INTO pf_store_item (album_id, goods_id, tag_id, can_process, first_image_hash)"
        " VALUES (?, 'g2', 1, 0, ?)",
        (aid, h),
    )
    cp = _resolve_can_process_for_image_hash(conn, aid, h, "g1", 1)
    assert cp == 1
    cp2 = _resolve_can_process_for_image_hash(conn, aid, h, "g2", 1)
    assert cp2 == 0


def test_reconcile_fixes_all_zero_group() -> None:
    conn = _mem_conn()
    h = "deadbeef"
    aid = "album1"
    conn.execute(
        "INSERT INTO pf_store_item (album_id, goods_id, tag_id, can_process, first_image_hash)"
        " VALUES (?, 'a', 1, 0, ?), (?, 'b', 1, 0, ?)",
        (aid, h, aid, h),
    )
    n = sqlite_reconcile_can_process_dup_hashes(conn, album_id=aid)
    assert n == 2
    rows = conn.execute(
        "SELECT id, can_process FROM pf_store_item WHERE first_image_hash = ? ORDER BY id",
        (h,),
    ).fetchall()
    assert rows[0]["can_process"] == 1
    assert rows[1]["can_process"] == 0
