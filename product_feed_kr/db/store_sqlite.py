"""本地 SQLite：店铺信息 + 商品明细（抓取与上架共用，单文件无需数据库服务）。

配置（环境变量或 ``config/seven17.json``）：``PRODUCT_FEED_SQLITE`` —— ``.db`` 文件路径；
未设置时默认 ``data/product_feed.db``（相对当前工作目录，一般为项目根）。

并发：抓取与上架对同一库文件使用 ``<路径>.lock`` 的 ``filelock`` 独占锁（跨进程）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from filelock import FileLock

from product_feed_kr._paths import REPO_ROOT
from product_feed_kr.common.pf_log import pf_db_row_id_kv, pf_kv, pf_trunc
from product_feed_kr.common.pf_time import SQLITE_NOW_CST8
from product_feed_kr.common.seven17_config import getenv as _cfg_get

_log = logging.getLogger("product_feed_kr.db.store_sqlite")

_SCHEMA_PATH = REPO_ROOT / "sql" / "schema_pf_sqlite.sql"

# 与 schema_pf_sqlite.sql 一致；整型/时间列靠前
_CREATE_PF_STORE_INFO = """
CREATE TABLE pf_store_info (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skip_detail INTEGER NOT NULL DEFAULT 0,
  detail_delay_sec REAL NOT NULL DEFAULT 5,
  last_saved_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  album_id TEXT NOT NULL UNIQUE,
  store_url TEXT NOT NULL,
  trans_lang TEXT NOT NULL DEFAULT 'zh',
  stats_json TEXT
)
"""

_CREATE_PF_STORE_ITEM = """
CREATE TABLE pf_store_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tag_id INTEGER NOT NULL DEFAULT 0,
  llm_attempt_count INTEGER NOT NULL DEFAULT 0,
  can_process INTEGER NOT NULL DEFAULT 1,
  can_upload INTEGER NOT NULL DEFAULT 0,
  rescrape_pending INTEGER NOT NULL DEFAULT 0,
  uploaded_to_platform INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  llm_processed_at TEXT,
  seven17_uploaded_at TEXT,
  seven17_ca_id TEXT,
  album_id TEXT NOT NULL,
  goods_id TEXT NOT NULL,
  wecatalog_group TEXT NOT NULL DEFAULT '',
  wecatalog_tag TEXT NOT NULL DEFAULT '',
  shop_category_path_json TEXT,
  goods_url TEXT NOT NULL,
  commodity_title TEXT NOT NULL DEFAULT '',
  price_cny TEXT,
  commodity_goods_num TEXT,
  commodity_image_urls_json TEXT,
  commodity_tag_names_json TEXT,
  commodity_sizes_json TEXT,
  commodity_colors_json TEXT,
  first_image_hash TEXT,
  price_krw TEXT,
  sizes_ko_json TEXT,
  colors_ko_json TEXT,
  llm_name_zh TEXT,
  llm_name_ko TEXT,
  llm_desc_zh TEXT,
  llm_desc_ko TEXT,
  llm_source TEXT,
  llm_reason TEXT,
  UNIQUE (album_id, goods_id, tag_id)
)
"""

_CREATE_PF_SCRAPE_SKIP = """
CREATE TABLE IF NOT EXISTS pf_scrape_skip (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  album_id TEXT NOT NULL,
  goods_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT 'popups_invalid',
  errcode INTEGER,
  errmsg TEXT,
  goods_url TEXT,
  first_seen_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  last_seen_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  hit_count INTEGER NOT NULL DEFAULT 1,
  UNIQUE (album_id, goods_id)
);
"""

_PF_STORE_ITEM_COLS: tuple[str, ...] = (
    "id",
    "tag_id",
    "llm_attempt_count",
    "can_process",
    "can_upload",
    "rescrape_pending",
    "uploaded_to_platform",
    "created_at",
    "updated_at",
    "llm_processed_at",
    "seven17_uploaded_at",
    "seven17_ca_id",
    "album_id",
    "goods_id",
    "wecatalog_group",
    "wecatalog_tag",
    "shop_category_path_json",
    "goods_url",
    "commodity_title",
    "price_cny",
    "commodity_goods_num",
    "commodity_image_urls_json",
    "commodity_tag_names_json",
    "commodity_sizes_json",
    "commodity_colors_json",
    "first_image_hash",
    "price_krw",
    "sizes_ko_json",
    "colors_ko_json",
    "llm_name_zh",
    "llm_name_ko",
    "llm_desc_zh",
    "llm_desc_ko",
    "llm_source",
    "llm_reason",
)


def sqlite_db_path() -> Path:
    root = REPO_ROOT
    raw = (_cfg_get("PRODUCT_FEED_SQLITE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p.resolve() if p.is_absolute() else (root / p).resolve()
    return (root / "data" / "product_feed.db").resolve()


def _write_lock(db_path: Path) -> FileLock:
    return FileLock(str(db_path) + ".lock", timeout=-1)


def _sqlite_text_factory(value: bytes | str | None) -> str | None:
    """TEXT 列解码：非法 UTF-8 用替换字符，避免读库整批失败。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _sqlite_cell_value(value: Any) -> Any:
    """行字段归一化：部分 SQLite/驱动对脏 TEXT 仍返回 bytes。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def connect_sqlite() -> sqlite3.Connection:
    return connect_sqlite_path(sqlite_db_path())


def connect_sqlite_path(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=120.0,
        isolation_level="DEFERRED",
        check_same_thread=False,
    )
    conn.text_factory = _sqlite_text_factory
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {str(r[1]) for r in cur.fetchall()}


def _table_column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [str(r[1]) for r in cur.fetchall()]


def _select_expr_from_old(col: str, old_names: set[str], *, table: str) -> str:
    """从旧表 SELECT 表达式（按列名映射到新表顺序）。"""
    if col in old_names:
        return col
    if col == "llm_attempt_count":
        if "llm_fail_count" in old_names:
            return "COALESCE(llm_fail_count, 0)"
        return "0"
    if col == "can_process":
        return "1"
    if col == "can_upload":
        return "0"
    if col == "rescrape_pending":
        return "0"
    if col in ("created_at", "updated_at"):
        return f"datetime('now', '+8 hours')"
    if col == "id" and "id" not in old_names:
        return "rowid"
    if col == "price_cny":
        parts: list[str] = []
        if "llm_cny_price" in old_names:
            parts.append(
                "CASE WHEN trim(COALESCE(llm_cny_price,''))<>'' THEN trim(llm_cny_price) END",
            )
        if "commodity_price_raw" in old_names:
            parts.append(
                "CASE WHEN trim(COALESCE(commodity_price_raw,''))<>'' "
                "THEN trim(commodity_price_raw) END",
            )
        if parts:
            return f"COALESCE({', '.join(parts)})"
        return "NULL"
    return "NULL"


def _rebuild_pf_store_item_layout(conn: sqlite3.Connection) -> None:
    names = _table_column_names(conn, "pf_store_item")
    if len(names) > 1 and names[1] == "tag_id":
        return
    old_names = set(names)
    conn.execute("ALTER TABLE pf_store_item RENAME TO pf_store_item__layout_old")
    conn.execute(_CREATE_PF_STORE_ITEM)
    insert_cols = ", ".join(_PF_STORE_ITEM_COLS)
    select_list = ", ".join(_select_expr_from_old(c, old_names, table="item") for c in _PF_STORE_ITEM_COLS)
    conn.execute(
        f"INSERT INTO pf_store_item ({insert_cols}) SELECT {select_list} FROM pf_store_item__layout_old",
    )
    conn.execute("DROP TABLE pf_store_item__layout_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_upload ON pf_store_item (album_id, uploaded_to_platform)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_goods ON pf_store_item (album_id, goods_id)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_album ON pf_store_item (album_id)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_img_hash ON pf_store_item (album_id, first_image_hash)",
    )


def _rebuild_pf_store_info_layout(conn: sqlite3.Connection) -> None:
    info_cols = (
        "id",
        "skip_detail",
        "detail_delay_sec",
        "last_saved_at",
        "updated_at",
        "album_id",
        "store_url",
        "trans_lang",
        "stats_json",
    )
    names = _table_column_names(conn, "pf_store_info")
    if len(names) > 1 and names[1] == "skip_detail":
        return
    old_names = set(names)
    conn.execute("ALTER TABLE pf_store_info RENAME TO pf_store_info__layout_old")
    conn.execute(_CREATE_PF_STORE_INFO)
    insert_cols = ", ".join(info_cols)
    select_list = ", ".join(_select_expr_from_old(c, old_names, table="info") for c in info_cols)
    conn.execute(
        f"INSERT INTO pf_store_info ({insert_cols}) SELECT {select_list} FROM pf_store_info__layout_old",
    )
    conn.execute("DROP TABLE pf_store_info__layout_old")


def _migrate_sqlite_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(pf_store_item)")
    cols = {str(r[1]) for r in cur.fetchall()}
    if "llm_attempt_count" not in cols:
        if "llm_fail_count" in cols:
            conn.execute(
                "ALTER TABLE pf_store_item ADD COLUMN llm_attempt_count INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                "UPDATE pf_store_item SET llm_attempt_count = COALESCE(llm_fail_count, 0)",
            )
        else:
            conn.execute(
                "ALTER TABLE pf_store_item ADD COLUMN llm_attempt_count INTEGER NOT NULL DEFAULT 0",
            )
    if "can_upload" not in cols:
        conn.execute(
            "ALTER TABLE pf_store_item ADD COLUMN can_upload INTEGER NOT NULL DEFAULT 0",
        )
    if "can_process" not in cols:
        conn.execute(
            "ALTER TABLE pf_store_item ADD COLUMN can_process INTEGER NOT NULL DEFAULT 1",
        )
    if "first_image_hash" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN first_image_hash TEXT")
    if "seven17_ca_id" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN seven17_ca_id TEXT")
    if "commodity_sizes_json" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN commodity_sizes_json TEXT")
    if "commodity_colors_json" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN commodity_colors_json TEXT")
    if "sizes_ko_json" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN sizes_ko_json TEXT")
    if "colors_ko_json" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN colors_ko_json TEXT")
    if "llm_source" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN llm_source TEXT")
    if "llm_reason" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN llm_reason TEXT")
    if "price_cny" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN price_cny TEXT")
    if "rescrape_pending" not in cols:
        conn.execute(
            "ALTER TABLE pf_store_item ADD COLUMN rescrape_pending INTEGER NOT NULL DEFAULT 0",
        )
    if "wecatalog_listed_at" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN wecatalog_listed_at TEXT")
    _backfill_llm_spec_from_legacy(conn)
    _backfill_flat_fields_from_listing_llm_json(conn)
    _migrate_drop_legacy_attr_map_columns(conn)
    _migrate_llm_prefixed_spec_columns(conn)
    _migrate_drop_response_json_columns(conn)
    _migrate_unify_price_columns(conn)
    _migrate_merge_supplement_into_commodity(conn)


def _backfill_llm_spec_from_legacy(conn: sqlite3.Connection) -> None:
    """旧 ``attr_map_*`` / ``listing_llm_json`` → ``commodity_*``（中文）+ ``sizes_ko_json``。"""
    import json as _json

    from product_feed_kr.db.llm_spec_fields import parse_json_str_list, spec_columns_from_listing_llm

    table_cols = set(_table_column_names(conn, "pf_store_item"))
    if "llm_sizes_json" in table_cols:
        return
    if "listing_llm_json" not in table_cols and "attr_map_json" not in table_cols:
        return

    select_cols = [
        "id",
        "commodity_sizes_json",
        "commodity_colors_json",
        "sizes_ko_json",
        "colors_ko_json",
    ]
    if "supplement_sizes_json" in table_cols:
        select_cols.extend(["supplement_sizes_json", "supplement_colors_json"])
    if "listing_llm_json" in table_cols:
        select_cols.append("listing_llm_json")
    if "attr_map_json" in table_cols:
        select_cols.append("attr_map_json")
    if "attr_map_ko_json" in table_cols:
        select_cols.append("attr_map_ko_json")
    cur = conn.execute(f"SELECT {', '.join(select_cols)} FROM pf_store_item")
    updates: list[tuple[Any, ...]] = []
    for row in cur:
        row_d = dict(row)
        if parse_json_str_list(row_d.get("sizes_ko_json")):
            if parse_json_str_list(row_d.get("commodity_sizes_json")):
                continue
        ll: dict[str, Any] | None = None
        raw = row_d.get("listing_llm_json")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = _json.loads(raw)
                ll = parsed if isinstance(parsed, dict) else None
            except _json.JSONDecodeError:
                ll = None
        if ll is None and "attr_map_json" in row_d:
            am = row_d.get("attr_map_json")
            amk = row_d.get("attr_map_ko_json")
            if isinstance(am, str) and am.strip():
                try:
                    ll = {"attr_map": _json.loads(am)}
                    if isinstance(amk, str) and amk.strip():
                        ll["attr_map_ko"] = _json.loads(amk)
                except _json.JSONDecodeError:
                    ll = None
        if not isinstance(ll, dict):
            continue
        spec = spec_columns_from_listing_llm(ll, row_d)
        if not any(spec.values()):
            continue
        updates.append(
            (
                spec["commodity_sizes_json"],
                spec["commodity_colors_json"],
                spec["sizes_ko_json"],
                spec["colors_ko_json"],
                int(row_d["id"]),
            ),
        )
    if not updates:
        return
    conn.executemany(
        """
        UPDATE pf_store_item SET
          commodity_sizes_json = COALESCE(?, commodity_sizes_json),
          commodity_colors_json = COALESCE(?, commodity_colors_json),
          sizes_ko_json = COALESCE(?, sizes_ko_json),
          colors_ko_json = COALESCE(?, colors_ko_json),
          updated_at = datetime('now', '+8 hours')
        WHERE id = ?
        """,
        updates,
    )


def _backfill_flat_fields_from_listing_llm_json(conn: sqlite3.Connection) -> None:
    """``listing_llm_json`` → 扁平行（文案 / 价 / source），迁移删列前执行。"""
    import json as _json

    from product_feed_kr.listing.listing_llm_enrich import _cny_price_field_usable

    table_cols = set(_table_column_names(conn, "pf_store_item"))
    if "listing_llm_json" not in table_cols:
        return

    select_cols = [
        c
        for c in (
            "id",
            "price_cny",
            "llm_name_zh",
            "llm_name_ko",
            "llm_desc_zh",
            "llm_desc_ko",
            "llm_processed_at",
            "llm_cny_price",
            "commodity_price_raw",
            "llm_source",
            "llm_reason",
            "listing_llm_json",
        )
        if c in table_cols
    ]
    cur = conn.execute(f"SELECT {', '.join(select_cols)} FROM pf_store_item")
    updates: list[tuple[Any, ...]] = []
    for row in cur:
        row_d = dict(row)
        raw = row_d.get("listing_llm_json")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            ll = _json.loads(raw)
        except _json.JSONDecodeError:
            continue
        if not isinstance(ll, dict):
            continue

        def _pick_str(key: str, existing: Any) -> str | None:
            if existing is not None and str(existing).strip():
                return None
            v = ll.get(key)
            if v is None or not str(v).strip():
                return None
            return str(v).strip()

        nzh = _pick_str("name_zh", row_d.get("llm_name_zh"))
        nko = _pick_str("name_ko", row_d.get("llm_name_ko"))
        dzh = _pick_str("desc_zh", row_d.get("llm_desc_zh"))
        dko = _pick_str("desc_ko", row_d.get("llm_desc_ko"))
        lpat = _pick_str("processed_at", row_d.get("llm_processed_at"))
        cny: str | None = None
        if not (row_d.get("price_cny") and str(row_d.get("price_cny")).strip()):
            if not (row_d.get("llm_cny_price") and str(row_d.get("llm_cny_price")).strip()):
                cp = ll.get("cny_price")
                if _cny_price_field_usable(cp):
                    cny = str(cp).strip()
            elif row_d.get("llm_cny_price"):
                cny = str(row_d.get("llm_cny_price")).strip() or None
        src = _pick_str("source", row_d.get("llm_source"))
        reason = _pick_str("reason", row_d.get("llm_reason"))
        price_cny_fill: str | None = cny
        if price_cny_fill is None and not (
            row_d.get("price_cny") and str(row_d.get("price_cny")).strip()
        ):
            raw = row_d.get("commodity_price_raw")
            if raw is not None and str(raw).strip():
                price_cny_fill = str(raw).strip()

        price_val = price_cny_fill if price_cny_fill is not None else cny
        if not any((nzh, nko, dzh, dko, lpat, price_val, src, reason)):
            continue
        row_vals: list[Any] = [nzh, nko, dzh, dko, lpat]
        if "price_cny" in table_cols:
            row_vals.append(price_val)
        if "llm_source" in table_cols:
            row_vals.append(src)
        if "llm_reason" in table_cols:
            row_vals.append(reason)
        row_vals.append(int(row_d["id"]))
        updates.append(tuple(row_vals))
    if not updates:
        return
    set_parts = [
        "llm_name_zh = COALESCE(?, llm_name_zh)",
        "llm_name_ko = COALESCE(?, llm_name_ko)",
        "llm_desc_zh = COALESCE(?, llm_desc_zh)",
        "llm_desc_ko = COALESCE(?, llm_desc_ko)",
        "llm_processed_at = COALESCE(?, llm_processed_at)",
    ]
    if "price_cny" in table_cols:
        set_parts.append("price_cny = COALESCE(?, price_cny)")
    if "llm_source" in table_cols:
        set_parts.append("llm_source = COALESCE(?, llm_source)")
    if "llm_reason" in table_cols:
        set_parts.append("llm_reason = COALESCE(?, llm_reason)")
    conn.executemany(
        f"UPDATE pf_store_item SET {', '.join(set_parts)} WHERE id = ?",
        updates,
    )


def _migrate_drop_response_json_columns(conn: sqlite3.Connection) -> None:
    """移除 ``detail_response_json`` / ``popups_response_json`` / ``listing_llm_json``（表重建）。"""
    old_names = set(_table_column_names(conn, "pf_store_item"))
    drop = {"detail_response_json", "popups_response_json", "listing_llm_json"}
    if not drop & old_names:
        return
    conn.execute("ALTER TABLE pf_store_item RENAME TO pf_store_item__json_old")
    conn.execute(_CREATE_PF_STORE_ITEM)
    insert_cols = ", ".join(_PF_STORE_ITEM_COLS)
    old_cols = set(_table_column_names(conn, "pf_store_item__json_old"))
    select_list = ", ".join(
        _select_expr_from_old(c, old_cols, table="item") for c in _PF_STORE_ITEM_COLS
    )
    conn.execute(
        f"INSERT INTO pf_store_item ({insert_cols}) SELECT {select_list} FROM pf_store_item__json_old",
    )
    conn.execute("DROP TABLE pf_store_item__json_old")
    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS idx_pf_item_upload ON pf_store_item (album_id, uploaded_to_platform)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_goods ON pf_store_item (album_id, goods_id)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_album ON pf_store_item (album_id)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_img_hash ON pf_store_item (album_id, first_image_hash)",
    ):
        conn.execute(idx_sql)


def _migrate_unify_price_columns(conn: sqlite3.Connection) -> None:
    """合并 ``commodity_price_raw`` / ``llm_cny_price`` → ``price_cny``；移除汇率/HTML 冗余列。"""
    old_names = set(_table_column_names(conn, "pf_store_item"))
    extra = {"commodity_price_raw", "llm_cny_price", "fx_krw_per_cny", "product_desc_html"}
    if "price_cny" in old_names and not (extra & old_names):
        return
    if "price_cny" not in old_names and not (
        "commodity_price_raw" in old_names or "llm_cny_price" in old_names
    ):
        return
    conn.execute("ALTER TABLE pf_store_item RENAME TO pf_store_item__price_old")
    conn.execute(_CREATE_PF_STORE_ITEM)
    insert_cols = ", ".join(_PF_STORE_ITEM_COLS)
    old_cols = set(_table_column_names(conn, "pf_store_item__price_old"))
    select_list = ", ".join(
        _select_expr_from_old(c, old_cols, table="item") for c in _PF_STORE_ITEM_COLS
    )
    conn.execute(
        f"INSERT INTO pf_store_item ({insert_cols}) SELECT {select_list} "
        "FROM pf_store_item__price_old",
    )
    conn.execute("DROP TABLE pf_store_item__price_old")
    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS idx_pf_item_upload ON pf_store_item (album_id, uploaded_to_platform)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_goods ON pf_store_item (album_id, goods_id)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_album ON pf_store_item (album_id)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_img_hash ON pf_store_item (album_id, first_image_hash)",
    ):
        conn.execute(idx_sql)


def _migrate_llm_prefixed_spec_columns(conn: sqlite3.Connection) -> None:
    """``llm_sizes_json`` 等 → ``commodity_*``（仅爬取为空时）+ ``sizes_ko_json`` / ``colors_ko_json``。"""
    from product_feed_kr.db.llm_spec_fields import dumps_json_list, parse_json_str_list

    table_cols = set(_table_column_names(conn, "pf_store_item"))
    if "llm_sizes_json" not in table_cols:
        return

    select = [
        "id",
        "commodity_sizes_json",
        "commodity_colors_json",
        "llm_sizes_json",
        "llm_colors_json",
        "llm_sizes_ko_json",
        "llm_colors_ko_json",
    ]
    cur = conn.execute(f"SELECT {', '.join(select)} FROM pf_store_item")
    updates: list[tuple[Any, ...]] = []
    for row in cur:
        row_d = dict(row)
        scrape_sz = parse_json_str_list(row_d.get("commodity_sizes_json"))
        scrape_cl = parse_json_str_list(row_d.get("commodity_colors_json"))
        ll_sz = parse_json_str_list(row_d.get("llm_sizes_json"))
        ll_cl = parse_json_str_list(row_d.get("llm_colors_json"))
        fill_sz = ll_sz if ll_sz and not scrape_sz else []
        fill_cl = ll_cl if ll_cl and not scrape_cl else []
        sz_ko = parse_json_str_list(row_d.get("llm_sizes_ko_json"))
        cl_ko = parse_json_str_list(row_d.get("llm_colors_ko_json"))
        if not (fill_sz or fill_cl or sz_ko or cl_ko):
            continue
        updates.append(
            (
                dumps_json_list(fill_sz),
                dumps_json_list(fill_cl),
                dumps_json_list(sz_ko),
                dumps_json_list(cl_ko),
                int(row_d["id"]),
            ),
        )
    if updates:
        conn.executemany(
            """
            UPDATE pf_store_item SET
              commodity_sizes_json = COALESCE(?, commodity_sizes_json),
              commodity_colors_json = COALESCE(?, commodity_colors_json),
              sizes_ko_json = COALESCE(?, sizes_ko_json),
              colors_ko_json = COALESCE(?, colors_ko_json)
            WHERE id = ?
            """,
            updates,
        )

    old_names = set(_table_column_names(conn, "pf_store_item"))
    if "llm_sizes_json" not in old_names:
        return
    conn.execute("ALTER TABLE pf_store_item RENAME TO pf_store_item__llm_spec_old")
    conn.execute(_CREATE_PF_STORE_ITEM)
    insert_cols = ", ".join(_PF_STORE_ITEM_COLS)
    old_cols = set(_table_column_names(conn, "pf_store_item__llm_spec_old"))
    select_list = ", ".join(
        _select_expr_from_old(c, old_cols, table="item") for c in _PF_STORE_ITEM_COLS
    )
    conn.execute(
        f"INSERT INTO pf_store_item ({insert_cols}) SELECT {select_list} FROM pf_store_item__llm_spec_old",
    )
    conn.execute("DROP TABLE pf_store_item__llm_spec_old")
    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS idx_pf_item_upload ON pf_store_item (album_id, uploaded_to_platform)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_goods ON pf_store_item (album_id, goods_id)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_album ON pf_store_item (album_id)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_img_hash ON pf_store_item (album_id, first_image_hash)",
    ):
        conn.execute(idx_sql)


def _migrate_merge_supplement_into_commodity(conn: sqlite3.Connection) -> None:
    """``supplement_*`` 合并进 ``commodity_sizes_json`` / ``commodity_colors_json`` 后删列。"""
    old_names = set(_table_column_names(conn, "pf_store_item"))
    if "supplement_sizes_json" not in old_names:
        return
    conn.execute(
        """
        UPDATE pf_store_item SET
          commodity_sizes_json = CASE
            WHEN trim(COALESCE(commodity_sizes_json, '')) <> '' THEN commodity_sizes_json
            ELSE supplement_sizes_json
          END,
          commodity_colors_json = CASE
            WHEN trim(COALESCE(commodity_colors_json, '')) <> '' THEN commodity_colors_json
            ELSE supplement_colors_json
          END
        """,
    )
    conn.execute("ALTER TABLE pf_store_item RENAME TO pf_store_item__supp_old")
    conn.execute(_CREATE_PF_STORE_ITEM)
    insert_cols = ", ".join(_PF_STORE_ITEM_COLS)
    old_cols = set(_table_column_names(conn, "pf_store_item__supp_old"))
    select_list = ", ".join(
        _select_expr_from_old(c, old_cols, table="item") for c in _PF_STORE_ITEM_COLS
    )
    conn.execute(
        f"INSERT INTO pf_store_item ({insert_cols}) SELECT {select_list} "
        "FROM pf_store_item__supp_old",
    )
    conn.execute("DROP TABLE pf_store_item__supp_old")
    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS idx_pf_item_upload ON pf_store_item (album_id, uploaded_to_platform)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_goods ON pf_store_item (album_id, goods_id)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_album ON pf_store_item (album_id)",
        "CREATE INDEX IF NOT EXISTS idx_pf_item_img_hash ON pf_store_item (album_id, first_image_hash)",
    ):
        conn.execute(idx_sql)


def _migrate_drop_legacy_attr_map_columns(conn: sqlite3.Connection) -> None:
    """移除 ``attr_map_json`` / ``attr_map_ko_json`` 列（表重建）。"""
    old_names = set(_table_column_names(conn, "pf_store_item"))
    if "attr_map_json" not in old_names and "attr_map_ko_json" not in old_names:
        return
    if "llm_sizes_json" in old_names:
        return
    if "commodity_sizes_json" not in old_names:
        return
    conn.execute("ALTER TABLE pf_store_item RENAME TO pf_store_item__attr_old")
    conn.execute(_CREATE_PF_STORE_ITEM)
    insert_cols = ", ".join(_PF_STORE_ITEM_COLS)
    old_cols = set(_table_column_names(conn, "pf_store_item__attr_old"))
    select_list = ", ".join(
        _select_expr_from_old(c, old_cols, table="item") for c in _PF_STORE_ITEM_COLS
    )
    conn.execute(
        f"INSERT INTO pf_store_item ({insert_cols}) SELECT {select_list} FROM pf_store_item__attr_old",
    )
    conn.execute("DROP TABLE pf_store_item__attr_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_upload ON pf_store_item (album_id, uploaded_to_platform)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_goods ON pf_store_item (album_id, goods_id)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_album ON pf_store_item (album_id)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_img_hash ON pf_store_item (album_id, first_image_hash)",
    )


def _migrate_store_info_to_int_pk(conn: sqlite3.Connection) -> None:
    if "id" in _table_columns(conn, "pf_store_info"):
        return
    conn.execute("ALTER TABLE pf_store_info RENAME TO pf_store_info__old")
    conn.execute(_CREATE_PF_STORE_INFO)
    conn.execute(
        """
        INSERT INTO pf_store_info (
          id, skip_detail, detail_delay_sec, last_saved_at, updated_at,
          album_id, store_url, trans_lang, stats_json
        )
        SELECT
          rowid, skip_detail, detail_delay_sec, last_saved_at, updated_at,
          album_id, store_url, trans_lang, stats_json
        FROM pf_store_info__old
        """,
    )
    conn.execute("DROP TABLE pf_store_info__old")


def _migrate_store_item_to_int_pk(conn: sqlite3.Connection) -> None:
    if "id" in _table_columns(conn, "pf_store_item"):
        return
    conn.execute("ALTER TABLE pf_store_item RENAME TO pf_store_item__old")
    conn.execute(_CREATE_PF_STORE_ITEM)
    old_names = set(_table_column_names(conn, "pf_store_item__old"))
    insert_cols = ", ".join(_PF_STORE_ITEM_COLS)
    select_list = ", ".join(_select_expr_from_old(c, old_names, table="item") for c in _PF_STORE_ITEM_COLS)
    conn.execute(
        f"INSERT INTO pf_store_item ({insert_cols}) SELECT {select_list} FROM pf_store_item__old",
    )
    conn.execute("DROP TABLE pf_store_item__old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_upload ON pf_store_item (album_id, uploaded_to_platform)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_goods ON pf_store_item (album_id, goods_id)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pf_item_album ON pf_store_item (album_id)",
    )


def _migrate_int_pk(conn: sqlite3.Connection) -> None:
    _migrate_store_info_to_int_pk(conn)
    _migrate_store_item_to_int_pk(conn)
    _rebuild_pf_store_info_layout(conn)
    _rebuild_pf_store_item_layout(conn)


def _migrate_pf_scrape_skip_columns(conn: sqlite3.Connection) -> None:
    """旧库 ``skip_reason`` / ``created_at`` → 新列 ``reason`` / ``first_seen_at`` 等。"""
    cols = set(_table_column_names(conn, "pf_scrape_skip"))
    if not cols:
        return
    if "reason" not in cols:
        conn.execute(
            "ALTER TABLE pf_scrape_skip ADD COLUMN reason TEXT NOT NULL DEFAULT 'popups_invalid'",
        )
        cols.add("reason")
    if "skip_reason" in cols:
        conn.execute(
            """
            UPDATE pf_scrape_skip
            SET reason = COALESCE(NULLIF(trim(skip_reason), ''), reason, 'popups_invalid')
            WHERE COALESCE(NULLIF(trim(reason), ''), '') IN ('', 'popups_invalid')
              AND COALESCE(NULLIF(trim(skip_reason), ''), '') != ''
            """,
        )
    if "first_seen_at" not in cols:
        conn.execute("ALTER TABLE pf_scrape_skip ADD COLUMN first_seen_at TEXT")
        if "created_at" in cols:
            conn.execute(
                "UPDATE pf_scrape_skip SET first_seen_at = created_at "
                "WHERE first_seen_at IS NULL OR trim(first_seen_at) = ''",
            )
        conn.execute(
            "UPDATE pf_scrape_skip SET first_seen_at = datetime('now', '+8 hours') "
            "WHERE first_seen_at IS NULL OR trim(first_seen_at) = ''",
        )
    if "last_seen_at" not in cols:
        conn.execute("ALTER TABLE pf_scrape_skip ADD COLUMN last_seen_at TEXT")
        if "updated_at" in cols:
            conn.execute(
                "UPDATE pf_scrape_skip SET last_seen_at = updated_at "
                "WHERE last_seen_at IS NULL OR trim(last_seen_at) = ''",
            )
        conn.execute(
            "UPDATE pf_scrape_skip SET last_seen_at = datetime('now', '+8 hours') "
            "WHERE last_seen_at IS NULL OR trim(last_seen_at) = ''",
        )
    if "hit_count" not in cols:
        conn.execute(
            "ALTER TABLE pf_scrape_skip ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 1",
        )


def _lookup_store_info_id(conn: sqlite3.Connection, album_id: str) -> int | None:
    cur = conn.execute("SELECT id FROM pf_store_info WHERE album_id = ? LIMIT 1", (album_id,))
    row = cur.fetchone()
    return int(row[0]) if row else None


def _lookup_item_id(
    conn: sqlite3.Connection,
    album_id: str,
    goods_id: str,
    tag_id: int,
) -> int | None:
    cur = conn.execute(
        "SELECT id FROM pf_store_item WHERE album_id = ? AND goods_id = ? AND tag_id = ? LIMIT 1",
        (album_id, goods_id, tag_id),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def ensure_sqlite_schema_at(conn: sqlite3.Connection, db_path: Path | str) -> None:
    """对指定路径的数据库执行建表与列迁移（锁文件绑定 db_path）。"""
    if not _SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"未找到建表脚本: {_SCHEMA_PATH}")
    path = Path(db_path).expanduser().resolve()
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    with _write_lock(path):
        conn.executescript(sql)
        _migrate_sqlite_columns(conn)
        _migrate_int_pk(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pf_item_img_hash ON pf_store_item (album_id, first_image_hash)",
        )
        conn.executescript(_CREATE_PF_SCRAPE_SKIP)
        _migrate_pf_scrape_skip_columns(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pf_scrape_skip_album ON pf_scrape_skip (album_id)",
        )
        conn.commit()


def ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
    ensure_sqlite_schema_at(conn, sqlite_db_path())


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: _sqlite_cell_value(row[k]) for k in row.keys()}


def _extract_enriched_fields(rec: dict[str, Any]) -> dict[str, Any]:
    """从记录提取 LLM/上架写库字段（价、规格拆列、中韩文文案）。"""
    from product_feed_kr.db.llm_spec_fields import spec_columns_from_listing_llm

    # 韩元仅来自抓取写库，不从 listing_llm 读取
    price_krw_raw = rec.get("price_krw")
    price_krw = str(price_krw_raw).strip() if price_krw_raw is not None else None
    if price_krw == "":
        price_krw = None

    ll = rec.get("listing_llm")
    spec_cols = spec_columns_from_listing_llm(
        ll if isinstance(ll, dict) else None,
        rec,
    )
    llm_name_zh: str | None = None
    llm_name_ko: str | None = None
    llm_desc_zh: str | None = None
    llm_desc_ko: str | None = None
    llm_processed_at: str | None = None
    if isinstance(ll, dict):
        nzh = ll.get("name_zh")
        nko = ll.get("name_ko")
        dzh = ll.get("desc_zh")
        dko = ll.get("desc_ko")
        llm_name_zh = str(nzh).strip() if nzh is not None and str(nzh).strip() else None
        llm_name_ko = str(nko).strip() if nko is not None and str(nko).strip() else None
        llm_desc_zh = str(dzh).strip() if dzh is not None and str(dzh).strip() else None
        llm_desc_ko = str(dko).strip() if dko is not None and str(dko).strip() else None
        lpat = ll.get("processed_at")
        llm_processed_at = str(lpat).strip() if lpat is not None and str(lpat).strip() else None
    if llm_processed_at is None:
        raw_lp = rec.get("llm_processed_at")
        llm_processed_at = str(raw_lp).strip() if raw_lp is not None and str(raw_lp).strip() else None

    price_cny: str | None = None
    llm_source: str | None = None
    llm_reason: str | None = None
    if isinstance(ll, dict):
        from product_feed_kr.listing.listing_llm_enrich import _cny_price_field_usable

        cp = ll.get("cny_price")
        if _cny_price_field_usable(cp):
            price_cny = str(cp).strip()
        src = ll.get("source")
        if src is not None and str(src).strip():
            llm_source = str(src).strip()
        rsn = ll.get("reason")
        if rsn is not None and str(rsn).strip():
            llm_reason = str(rsn).strip()

    return {
        "price_krw": price_krw,
        **spec_cols,
        "llm_name_zh": llm_name_zh,
        "llm_name_ko": llm_name_ko,
        "llm_desc_zh": llm_desc_zh,
        "llm_desc_ko": llm_desc_ko,
        "llm_processed_at": llm_processed_at,
        "price_cny": price_cny,
        "llm_source": llm_source,
        "llm_reason": llm_reason,
    }


def row_to_product_record(row: dict[str, Any]) -> dict[str, Any]:
    from product_feed_kr.db.llm_spec_fields import listing_llm_from_row

    rec: dict[str, Any] = {
        "wecatalog_group": row["wecatalog_group"],
        "wecatalog_tag": row["wecatalog_tag"],
        "tag_id": int(row["tag_id"]),
        "goods_url": row["goods_url"],
        "goods_id": row["goods_id"],
    }
    if row.get("id") is not None:
        try:
            rec["id"] = int(row["id"])
        except (TypeError, ValueError):
            pass
    ca_raw = row.get("seven17_ca_id")
    rec.update({
        "can_process": bool(row.get("can_process", 1)),
        "uploaded_to_platform": bool(row["uploaded_to_platform"]),
        "can_upload": bool(row.get("can_upload")),
        "seven17_uploaded_at": (str(row.get("seven17_uploaded_at")).strip() if row.get("seven17_uploaded_at") is not None else None),
        "seven17_ca_id": (str(ca_raw).strip() if ca_raw is not None and str(ca_raw).strip() else None),
        "first_image_hash": (str(row.get("first_image_hash")).strip() if row.get("first_image_hash") is not None and str(row.get("first_image_hash")).strip() else None),
    })
    rec["commodity_sizes_json"] = row.get("commodity_sizes_json")
    rec["commodity_colors_json"] = row.get("commodity_colors_json")
    scp = row.get("shop_category_path_json")
    if isinstance(scp, str) and scp.strip():
        try:
            rec["shop_category_path"] = json.loads(scp)
        except json.JSONDecodeError:
            rec["shop_category_path"] = None
    ll_mem = listing_llm_from_row(row)
    if ll_mem:
        rec["listing_llm"] = ll_mem
    # 最小必需字段（独立列）也回填到内存记录，供调试/导出查看。
    image_urls: list[str] = []
    if isinstance(row.get("commodity_image_urls_json"), str) and str(row.get("commodity_image_urls_json") or "").strip():
        try:
            parsed_urls = json.loads(row["commodity_image_urls_json"])
            if isinstance(parsed_urls, list):
                image_urls = [str(x).strip() for x in parsed_urls if str(x).strip()]
        except json.JSONDecodeError:
            image_urls = []

    tag_names: list[str] = []
    if isinstance(row.get("commodity_tag_names_json"), str) and str(row.get("commodity_tag_names_json") or "").strip():
        try:
            parsed_tags = json.loads(row["commodity_tag_names_json"])
            if isinstance(parsed_tags, list):
                tag_names = [str(x).strip() for x in parsed_tags if str(x).strip()]
        except json.JSONDecodeError:
            tag_names = []

    from product_feed_kr.db.llm_spec_fields import listing_llm_attr_maps_from_row
    from product_feed_kr.wecatalog.wecatalog_scrape_fields import parse_colors_json, parse_sizes_json

    scrape_sizes = parse_sizes_json(row.get("commodity_sizes_json"))
    scrape_colors = parse_colors_json(row.get("commodity_colors_json"))
    rec["commodity_sizes"] = scrape_sizes
    rec["commodity_colors"] = scrape_colors
    rec["sizes_ko_json"] = row.get("sizes_ko_json")
    rec["colors_ko_json"] = row.get("colors_ko_json")

    attr_zh, attr_ko = listing_llm_attr_maps_from_row(row)

    rec["commodity_min"] = {
        "title": str(row.get("commodity_title") or "").strip(),
        "price_raw": (str(row.get("price_cny")).strip() if row.get("price_cny") is not None else None),
        "goods_num": (str(row.get("commodity_goods_num") or "").strip() or None),
        "image_urls": image_urls,
        "tag_names": tag_names,
        "scrape_sizes": scrape_sizes,
        "scrape_colors": scrape_colors,
        "price_krw": (str(row.get("price_krw")).strip() if row.get("price_krw") is not None else None),
        "attr_map": attr_zh,
        "attr_map_ko": attr_ko,
        "llm_name_zh": (str(row.get("llm_name_zh")).strip() if row.get("llm_name_zh") is not None else None),
        "llm_name_ko": (str(row.get("llm_name_ko")).strip() if row.get("llm_name_ko") is not None else None),
        "llm_desc_zh": (str(row.get("llm_desc_zh")).strip() if row.get("llm_desc_zh") is not None else None),
        "llm_desc_ko": (str(row.get("llm_desc_ko")).strip() if row.get("llm_desc_ko") is not None else None),
        "llm_processed_at": (str(row.get("llm_processed_at")).strip() if row.get("llm_processed_at") is not None else None),
        "seven17_uploaded_at": (str(row.get("seven17_uploaded_at")).strip() if row.get("seven17_uploaded_at") is not None else None),
    }
    rec["price_cny"] = row.get("price_cny")
    pk = row.get("price_krw")
    rec["price_krw"] = (
        str(pk).strip().replace(",", "") if pk is not None and str(pk).strip() else None
    )
    wla = row.get("wecatalog_listed_at")
    rec["wecatalog_listed_at"] = (
        str(wla).strip() if wla is not None and str(wla).strip() else None
    )
    rec["llm_processed_at"] = rec["commodity_min"]["llm_processed_at"]
    try:
        raw_ac = row.get("llm_attempt_count")
        if raw_ac is None:
            raw_ac = row.get("llm_fail_count")
        rec["llm_attempt_count"] = int(raw_ac or 0)
    except (TypeError, ValueError):
        rec["llm_attempt_count"] = 0
    return rec


def _llm_attempt_count_for_db(rec: dict[str, Any]) -> int:
    try:
        raw = rec.get("llm_attempt_count")
        if raw is None:
            raw = rec.get("llm_fail_count")
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def scrape_detail_ready(rec: dict[str, Any]) -> bool:
    """抓取入库：至少有非空 ``commodity_title``（结构化字段）。"""
    return bool(str(rec.get("commodity_title") or "").strip())


def sqlite_count_store_items(conn: sqlite3.Connection, album_id: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM pf_store_item WHERE album_id = ?",
        (album_id,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


_RERUN_ACTIONS = frozenset({"rescrape", "reprocess", "reupload"})


def sqlite_request_item_rerun(
    conn: sqlite3.Connection,
    item_id: int,
    action: str,
) -> dict[str, Any] | None:
    """浏览器触发单条重跑：rescrape / reprocess / reupload（下游状态单向回退）。"""
    act = (action or "").strip().lower()
    if act not in _RERUN_ACTIONS:
        raise ValueError(f"无效 action: {action!r}")

    cur = conn.execute("SELECT * FROM pf_store_item WHERE id = ? LIMIT 1", (int(item_id),))
    row = cur.fetchone()
    if row is None:
        return None
    base = _row_to_dict(row)
    album_id = str(base.get("album_id") or "")
    goods_id = str(base.get("goods_id") or "")
    if not album_id or not goods_id:
        return None

    db_path = sqlite_db_path()
    upload_sql = "uploaded_to_platform = 0, seven17_uploaded_at = NULL"
    llm_sql = """
      llm_processed_at = NULL,
      llm_attempt_count = 0,
      llm_name_zh = NULL,
      llm_name_ko = NULL,
      llm_desc_zh = NULL,
      llm_desc_ko = NULL,
      llm_source = NULL,
      llm_reason = NULL,
      sizes_ko_json = NULL,
      colors_ko_json = NULL,
      price_krw = NULL,
      can_upload = 0
    """

    with _write_lock(db_path):
        if act == "reupload":
            conn.execute(
                f"""
                UPDATE pf_store_item SET
                  {upload_sql},
                  updated_at = {SQLITE_NOW_CST8}
                WHERE id = ?
                """,
                (int(item_id),),
            )
        elif act == "reprocess":
            conn.execute(
                f"""
                UPDATE pf_store_item SET
                  can_process = 1,
                  rescrape_pending = 0,
                  {llm_sql},
                  {upload_sql},
                  updated_at = {SQLITE_NOW_CST8}
                WHERE id = ?
                """,
                (int(item_id),),
            )
        else:  # rescrape
            conn.execute(
                f"""
                UPDATE pf_store_item SET
                  rescrape_pending = 1,
                  can_process = 1,
                  {llm_sql},
                  {upload_sql},
                  updated_at = {SQLITE_NOW_CST8}
                WHERE album_id = ? AND goods_id = ?
                """,
                (album_id, goods_id),
            )
            _sqlite_clear_scrape_skip_unlocked(conn, album_id, goods_id)
        conn.commit()

    cur2 = conn.execute("SELECT * FROM pf_store_item WHERE id = ? LIMIT 1", (int(item_id),))
    updated = cur2.fetchone()
    if updated is None:
        return None
    out = _row_to_dict(updated)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.write"),
                ("op", f"rerun_{act}"),
                *pf_db_row_id_kv(out, row_id=int(item_id)),
                ("goods_id", goods_id),
                ("scope", "goods_id" if act == "rescrape" else "row"),
            ],
            zh={
                "rescrape": "标记待重爬（同 goods_id 全部行，并重置 LLM/上架）",
                "reprocess": "重置 LLM/上架，待 02 重新处理",
                "reupload": "清除上架标记，待 03 重新上传",
            }.get(act, "重跑"),
        ),
    )
    return out


def sqlite_load_existing_goods_ids(conn: sqlite3.Connection, album_id: str) -> tuple[set[str], int]:
    """返回 (已有 goods_id 集合, 库内商品行数)；不加载整行，避免 checkpoint 用旧快照覆盖 LLM。

    ``rescrape_pending=1`` 的 ``goods_id`` 不进入跳过集合，下次采集会重新请求 popUps 并写库。
    """
    total = sqlite_count_store_items(conn, album_id)
    cur = conn.execute(
        """
        SELECT DISTINCT goods_id FROM pf_store_item
        WHERE album_id = ?
          AND goods_id NOT IN (
            SELECT goods_id FROM pf_store_item
            WHERE album_id = ? AND rescrape_pending = 1
          )
        """,
        (album_id, album_id),
    )
    skip_ids: set[str] = set()
    for r in cur.fetchall():
        gid = r[0]
        if isinstance(gid, str) and gid.strip():
            skip_ids.add(gid.strip())
    store_id = _lookup_store_info_id(conn, album_id)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.read"),
                ("op", "load_existing_goods_ids"),
                *pf_db_row_id_kv(row_id=store_id),
                ("distinct_goods_id", len(skip_ids)),
                ("rows", total),
            ],
            zh="读库：仅载入已有 goods_id（用于跳过，不载入整行）",
        ),
    )
    return skip_ids, total


def sqlite_load_scrape_skip_goods_ids(conn: sqlite3.Connection, album_id: str) -> set[str]:
    """``pf_scrape_skip`` 中本店永久跳过的 ``goods_id``（不请求 popUps）。"""
    cur = conn.execute(
        "SELECT goods_id FROM pf_scrape_skip WHERE album_id = ?",
        (album_id,),
    )
    out: set[str] = set()
    for r in cur.fetchall():
        gid = r[0]
        if isinstance(gid, str) and gid.strip():
            out.add(gid.strip())
    if out:
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "db.read"),
                    ("op", "load_scrape_skip"),
                    ("album_id", album_id),
                    ("skip_blacklist", len(out)),
                ],
                zh="读库：pf_scrape_skip 永久跳过 goods_id",
            ),
        )
    return out


def sqlite_record_scrape_skip(
    conn: sqlite3.Connection,
    album_id: str,
    goods_id: str,
    *,
    reason: str,
    errcode: int | None = None,
    errmsg: str | None = None,
    goods_url: str | None = None,
) -> None:
    """记入/更新 ``pf_scrape_skip``（同 goods_id 累加 hit_count）。"""
    gid = str(goods_id or "").strip()
    if not gid:
        return
    db_path = sqlite_db_path()
    with _write_lock(db_path):
        conn.execute(
            f"""
            INSERT INTO pf_scrape_skip (
              album_id, goods_id, reason, errcode, errmsg, goods_url
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(album_id, goods_id) DO UPDATE SET
              reason = excluded.reason,
              errcode = excluded.errcode,
              errmsg = excluded.errmsg,
              goods_url = COALESCE(excluded.goods_url, pf_scrape_skip.goods_url),
              last_seen_at = {SQLITE_NOW_CST8},
              hit_count = pf_scrape_skip.hit_count + 1
            """,
            (
                album_id,
                gid,
                str(reason or "popups_invalid").strip() or "popups_invalid",
                errcode,
                (str(errmsg).strip() if errmsg else None),
                (str(goods_url).strip() if goods_url else None),
            ),
        )
        conn.commit()
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.write"),
                ("op", "scrape_skip"),
                ("album_id", album_id),
                ("goods_id", gid),
                ("reason", reason),
                ("errcode", errcode),
            ],
            zh="写库：pf_scrape_skip 永久跳过",
        ),
    )


def _sqlite_clear_scrape_skip_unlocked(
    conn: sqlite3.Connection,
    album_id: str,
    goods_id: str,
) -> int:
    gid = str(goods_id or "").strip()
    if not gid:
        return 0
    cur = conn.execute(
        "DELETE FROM pf_scrape_skip WHERE album_id = ? AND goods_id = ?",
        (album_id, gid),
    )
    return int(cur.rowcount or 0)


def sqlite_clear_scrape_skip(
    conn: sqlite3.Connection,
    album_id: str,
    goods_id: str,
) -> int:
    """从跳过表移除（如标记重爬或抓取成功后可再请求 popUps）。返回删除行数。"""
    gid = str(goods_id or "").strip()
    if not gid:
        return 0
    db_path = sqlite_db_path()
    with _write_lock(db_path):
        n = _sqlite_clear_scrape_skip_unlocked(conn, album_id, gid)
        conn.commit()
    if n:
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "db.write"),
                    ("op", "scrape_skip_clear"),
                    ("album_id", album_id),
                    ("goods_id", gid),
                    ("deleted", n),
                ],
                zh="写库：清除 pf_scrape_skip",
            ),
        )
    return n


def sqlite_load_existing_products(conn: sqlite3.Connection, album_id: str) -> tuple[list[dict[str, Any]], set[str]]:
    records: list[dict[str, Any]] = []
    skip_ids: set[str] = set()
    cur = conn.execute("SELECT DISTINCT goods_id FROM pf_store_item WHERE album_id = ?", (album_id,))
    for r in cur.fetchall():
        d = _row_to_dict(r)
        gid = d.get("goods_id")
        if isinstance(gid, str) and gid:
            skip_ids.add(gid)
    cur = conn.execute(
        "SELECT * FROM pf_store_item WHERE album_id = ? ORDER BY goods_id, tag_id",
        (album_id,),
    )
    for row in cur.fetchall():
        records.append(row_to_product_record(_row_to_dict(row)))
    store_id = _lookup_store_info_id(conn, album_id)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.read"),
                ("op", "load_existing_products"),
                *pf_db_row_id_kv(row_id=store_id),
                ("rows", len(records)),
                ("distinct_goods_id", len(skip_ids)),
            ],
            zh="读库：载入本店已有商品",
        ),
    )
    return records, skip_ids


def _load_upload_priority_rank() -> dict[tuple[str, str], int]:
    try:
        from product_feed_kr.pf_browser.upload_priority import upload_priority_rank_map

        return upload_priority_rank_map()
    except Exception:
        return {}


def sqlite_load_products_for_upload(
    conn: sqlite3.Connection,
    album_id: str,
    *,
    skip_uploaded: bool = True,
) -> list[dict[str, Any]]:
    """载入商品行：上架优先级优先，次按 ``wecatalog_listed_at`` 早的优先（空上架时间排后），同时间按 ``id``。"""
    order_sql = """
        CASE WHEN trim(COALESCE(wecatalog_listed_at, '')) = '' THEN 1 ELSE 0 END,
        wecatalog_listed_at ASC,
        id ASC
    """
    if skip_uploaded:
        cur = conn.execute(
            f"""
            SELECT * FROM pf_store_item
            WHERE album_id = ? AND seven17_uploaded_at IS NULL
            ORDER BY {order_sql}
            """,
            (album_id,),
        )
    else:
        cur = conn.execute(
            f"""
            SELECT * FROM pf_store_item
            WHERE album_id = ?
            ORDER BY {order_sql}
            """,
            (album_id,),
        )
    rows = [row_to_product_record(_row_to_dict(r)) for r in cur.fetchall()]
    ranks = _load_upload_priority_rank()
    if ranks:
        max_rank = len(ranks)
        rows.sort(key=lambda rec: ranks.get(
            (str(rec.get("wecatalog_group") or "").strip(),
             str(rec.get("wecatalog_tag") or "").strip()),
            max_rank,
        ))
    store_id = _lookup_store_info_id(conn, album_id)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.read"),
                ("op", "load_products_for_upload"),
                *pf_db_row_id_kv(row_id=store_id),
                ("skip_uploaded", 1 if skip_uploaded else 0),
                ("rows", len(rows)),
                ("upload_priority_ranks", len(ranks) if ranks else 0),
            ],
            zh="读库：载入待上架/处理商品列表（上架优先，次按微猫上架时间）",
        ),
    )
    return rows


def sqlite_write_store_meta(
    conn: sqlite3.Connection,
    album_id: str,
    *,
    store_url: str,
    trans_lang: str,
    detail_delay_sec: float,
    skip_detail: bool,
    meta_extra: dict[str, Any],
) -> None:
    """仅更新 ``pf_store_info``（进度/统计），不写商品行。"""
    db_path = sqlite_db_path()
    saved_at = str(meta_extra.get("saved_at") or "")
    stats = meta_extra.get("stats")
    stats_json = json.dumps(stats, ensure_ascii=False) if isinstance(stats, dict) else None
    with _write_lock(db_path):
        conn.execute(
            f"""
            INSERT INTO pf_store_info (
              album_id, store_url, trans_lang, detail_delay_sec, skip_detail, last_saved_at, stats_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(album_id) DO UPDATE SET
              store_url = excluded.store_url,
              trans_lang = excluded.trans_lang,
              detail_delay_sec = excluded.detail_delay_sec,
              skip_detail = excluded.skip_detail,
              last_saved_at = excluded.last_saved_at,
              stats_json = excluded.stats_json,
              updated_at = {SQLITE_NOW_CST8}
            """,
            (
                album_id,
                store_url,
                trans_lang,
                float(detail_delay_sec),
                1 if skip_detail else 0,
                saved_at or None,
                stats_json,
            ),
        )
        conn.commit()
    store_id = _lookup_store_info_id(conn, album_id)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.write"),
                ("op", "store_meta"),
                *pf_db_row_id_kv(row_id=store_id),
            ],
            zh="写库：店铺元信息",
        ),
    )


def _resolve_can_process_for_image_hash(
    conn: sqlite3.Connection,
    album_id: str,
    first_image_hash: str,
    goods_id: str,
    tag_id: int,
    *,
    batch_seen_hash: set[str] | None = None,
) -> int:
    """同 ``album_id`` + 首图 hash 仅 **id 最小** 的一条为 1；本批内第二条起为 0。"""
    h = str(first_image_hash or "").strip()
    if not h:
        return 1
    if batch_seen_hash is not None:
        if h in batch_seen_hash:
            return 0
        batch_seen_hash.add(h)

    self_id = _lookup_item_id(conn, album_id, goods_id, tag_id)
    row = conn.execute(
        """
        SELECT MIN(id) AS mid
        FROM pf_store_item
        WHERE album_id = ?
          AND first_image_hash = ?
          AND NOT (goods_id = ? AND tag_id = ?)
        """,
        (album_id, h, goods_id, tag_id),
    ).fetchone()
    other_min = int(row["mid"]) if row and row["mid"] is not None else None
    if self_id is None:
        return 0 if other_min is not None else 1
    if other_min is None:
        return 1
    return 1 if self_id <= other_min else 0


def sqlite_reconcile_can_process_dup_hashes(
    conn: sqlite3.Connection,
    *,
    album_id: str | None = None,
) -> int:
    """按首图 hash 对账：每组仅 ``MIN(id)`` 为 ``can_process=1``（修复全部被标 0 的重复组）。"""
    parts = ["trim(COALESCE(first_image_hash, '')) <> ''"]
    params: list[Any] = []
    if album_id and str(album_id).strip():
        parts.append("album_id = ?")
        params.append(str(album_id).strip())
    where_sql = " AND ".join(parts)
    cur = conn.execute(
        f"""
        UPDATE pf_store_item
        SET can_process = (
              id = (
                SELECT MIN(i2.id)
                FROM pf_store_item i2
                WHERE i2.album_id = pf_store_item.album_id
                  AND i2.first_image_hash = pf_store_item.first_image_hash
              )
            ),
            updated_at = {SQLITE_NOW_CST8}
        WHERE {where_sql}
        """,
        params,
    )
    return int(cur.rowcount or 0)


def sqlite_load_goods_ids_missing_listed_at(
    conn: sqlite3.Connection,
    album_id: str,
) -> set[str]:
    """库内 ``wecatalog_listed_at`` 为空的 ``goods_id``（用于列表翻页补全上架时间）。"""
    cur = conn.execute(
        """
        SELECT DISTINCT goods_id FROM pf_store_item
        WHERE album_id = ?
          AND trim(COALESCE(wecatalog_listed_at, '')) = ''
          AND trim(COALESCE(goods_id, '')) <> ''
        """,
        (album_id,),
    )
    out: set[str] = set()
    for r in cur.fetchall():
        gid = r[0]
        if isinstance(gid, str) and gid.strip():
            out.add(gid.strip())
    if out:
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "db.read"),
                    ("op", "load_missing_listed_at"),
                    ("album_id", album_id),
                    ("goods_id", len(out)),
                ],
                zh="读库：待补全微猫上架时间的 goods_id",
            ),
        )
    return out


def sqlite_backfill_wecatalog_listed_at(
    conn: sqlite3.Connection,
    album_id: str,
    updates: dict[str, str],
) -> int:
    """按 ``goods_id`` 批量补 ``wecatalog_listed_at``（仅更新当前为空的行）。"""
    if not updates:
        return 0
    db_path = sqlite_db_path()
    n = 0
    with _write_lock(db_path):
        for gid, listed_at in updates.items():
            if not gid or not str(listed_at or "").strip():
                continue
            cur = conn.execute(
                f"""
                UPDATE pf_store_item
                SET wecatalog_listed_at = ?, updated_at = {SQLITE_NOW_CST8}
                WHERE album_id = ? AND goods_id = ?
                  AND trim(COALESCE(wecatalog_listed_at, '')) = ''
                """,
                (str(listed_at).strip(), album_id, gid),
            )
            n += int(cur.rowcount or 0)
        if n:
            conn.commit()
            _log.info(
                "%s",
                pf_kv(
                    [
                        ("event", "db.write"),
                        ("op", "backfill_listed_at"),
                        ("album_id", album_id),
                        ("goods_id", len(updates)),
                        ("rows", n),
                    ],
                    zh="写库：补全微猫上架时间",
                ),
            )
    return n


def sqlite_upsert_scrape_items(
    conn: sqlite3.Connection,
    album_id: str,
    records: list[dict[str, Any]],
) -> int:
    """仅 upsert 本批已抓到有效详情的商品行；冲突时更新爬虫字段与 price_cny/price_krw，不碰 LLM / 上架状态。"""
    to_write = [rec for rec in records if scrape_detail_ready(rec)]
    if not to_write:
        return 0
    db_path = sqlite_db_path()
    with _write_lock(db_path):
        sql_prod = f"""
            INSERT INTO pf_store_item (
              album_id, goods_id, tag_id, wecatalog_group, wecatalog_tag,
              shop_category_path_json, goods_url, can_process, uploaded_to_platform,
              commodity_title, wecatalog_listed_at, price_cny, price_krw, commodity_goods_num,
              commodity_image_urls_json, commodity_tag_names_json,
              commodity_sizes_json, commodity_colors_json,
              sizes_ko_json,
              first_image_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(album_id, goods_id, tag_id) DO UPDATE SET
              wecatalog_group = excluded.wecatalog_group,
              wecatalog_tag = excluded.wecatalog_tag,
              shop_category_path_json = excluded.shop_category_path_json,
              goods_url = excluded.goods_url,
              can_process = excluded.can_process,
              commodity_title = excluded.commodity_title,
              wecatalog_listed_at = CASE
                WHEN trim(COALESCE(excluded.wecatalog_listed_at, '')) <> ''
                THEN excluded.wecatalog_listed_at
                ELSE pf_store_item.wecatalog_listed_at
              END,
              price_cny = excluded.price_cny,
              price_krw = excluded.price_krw,
              commodity_goods_num = excluded.commodity_goods_num,
              commodity_image_urls_json = excluded.commodity_image_urls_json,
              commodity_tag_names_json = excluded.commodity_tag_names_json,
              commodity_sizes_json = CASE
                WHEN trim(COALESCE(excluded.commodity_sizes_json, '')) <> ''
                THEN excluded.commodity_sizes_json
                ELSE pf_store_item.commodity_sizes_json
              END,
              commodity_colors_json = CASE
                WHEN trim(COALESCE(excluded.commodity_colors_json, '')) <> ''
                THEN excluded.commodity_colors_json
                ELSE pf_store_item.commodity_colors_json
              END,
              sizes_ko_json = CASE
                WHEN trim(COALESCE(excluded.sizes_ko_json, '')) <> ''
                THEN excluded.sizes_ko_json
                ELSE pf_store_item.sizes_ko_json
              END,
              first_image_hash = excluded.first_image_hash,
              updated_at = {SQLITE_NOW_CST8}
            """
        batch_seen_hash: set[str] = set()
        for rec in to_write:
            from product_feed_kr.wecatalog.wecatalog_scrape_fields import scrape_fields_to_db_columns

            mins = scrape_fields_to_db_columns(rec)
            scp = rec.get("shop_category_path")
            scp_json = json.dumps(scp, ensure_ascii=False) if isinstance(scp, list) else None
            tid = rec.get("tag_id")
            try:
                tag_id = int(tid) if tid is not None else 0
            except (TypeError, ValueError):
                tag_id = 0
            gid = str(rec.get("goods_id") or "")
            first_image_hash = mins["first_image_hash"]
            can_process = 1
            if isinstance(first_image_hash, str) and first_image_hash.strip():
                can_process = _resolve_can_process_for_image_hash(
                    conn,
                    album_id,
                    first_image_hash,
                    gid,
                    tag_id,
                    batch_seen_hash=batch_seen_hash,
                )
            conn.execute(
                sql_prod,
                (
                    album_id,
                    gid,
                    tag_id,
                    str(rec.get("wecatalog_group") or ""),
                    str(rec.get("wecatalog_tag") or ""),
                    scp_json,
                    str(rec.get("goods_url") or ""),
                    can_process,
                    mins["commodity_title"],
                    mins.get("wecatalog_listed_at"),
                    mins["price_cny"],
                    mins.get("price_krw"),
                    mins["commodity_goods_num"],
                    mins["commodity_image_urls_json"],
                    mins["commodity_tag_names_json"],
                    mins.get("commodity_sizes_json"),
                    mins.get("commodity_colors_json"),
                    mins.get("sizes_ko_json"),
                    first_image_hash,
                ),
            )
            item_id = _lookup_item_id(conn, album_id, gid, tag_id)
            if item_id is not None:
                rec["id"] = item_id
            conn.execute(
                """
                UPDATE pf_store_item SET rescrape_pending = 0
                WHERE album_id = ? AND goods_id = ? AND rescrape_pending = 1
                """,
                (album_id, gid),
            )
            _sqlite_clear_scrape_skip_unlocked(conn, album_id, gid)
        reconciled = sqlite_reconcile_can_process_dup_hashes(conn, album_id=album_id)
        if reconciled:
            _log.info(
                "%s",
                pf_kv(
                    [
                        ("event", "db.reconcile_can_process"),
                        ("album_id", album_id),
                        ("rows", reconciled),
                    ],
                    zh="首图 hash 重复组对账 can_process",
                ),
            )
        conn.commit()
    store_id = _lookup_store_info_id(conn, album_id)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.write"),
                ("op", "upsert_scrape_items_done"),
                *pf_db_row_id_kv(row_id=store_id),
                ("rows", len(to_write)),
            ],
            zh="写库：本批详情入库完成",
        ),
    )
    return len(to_write)


def sqlite_checkpoint(
    conn: sqlite3.Connection,
    album_id: str,
    *,
    store_url: str,
    trans_lang: str,
    detail_delay_sec: float,
    skip_detail: bool,
    meta_extra: dict[str, Any],
    records: list[dict[str, Any]],
) -> int:
    """店铺元信息 + 仅写入 ``records`` 中已含有效详情的行。返回本批写入商品行数。"""
    sqlite_write_store_meta(
        conn,
        album_id,
        store_url=store_url,
        trans_lang=trans_lang,
        detail_delay_sec=detail_delay_sec,
        skip_detail=skip_detail,
        meta_extra=meta_extra,
    )
    return sqlite_upsert_scrape_items(conn, album_id, records)


def sqlite_mark_uploaded(conn: sqlite3.Connection, album_id: str, rec: dict[str, Any]) -> None:
    tid = rec.get("tag_id")
    try:
        tag_id = int(tid) if tid is not None else 0
    except (TypeError, ValueError):
        tag_id = 0
    db_path = sqlite_db_path()
    with _write_lock(db_path):
        conn.execute(
            f"""
            UPDATE pf_store_item SET
              uploaded_to_platform = 1,
              seven17_uploaded_at = {SQLITE_NOW_CST8},
              updated_at = {SQLITE_NOW_CST8}
            WHERE album_id = ? AND goods_id = ? AND tag_id = ?
            """,
            (album_id, str(rec.get("goods_id") or ""), tag_id),
        )
        conn.commit()
    item_id = rec.get("id")
    if item_id is None:
        item_id = _lookup_item_id(conn, album_id, str(rec.get("goods_id") or ""), tag_id)
        if item_id is not None:
            rec["id"] = item_id
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.write"),
                ("op", "mark_uploaded"),
                *pf_db_row_id_kv(rec, row_id=item_id if isinstance(item_id, int) else None),
            ],
            zh="写库：标记已上架",
        ),
    )


def sqlite_update_llm_result(conn: sqlite3.Connection, album_id: str, rec: dict[str, Any]) -> None:
    """LLM 专用写回：只更新 LLM 相关字段，不碰上架状态（不落库原始 JSON）。"""
    enrich = _extract_enriched_fields(rec)
    tid = rec.get("tag_id")
    try:
        tag_id = int(tid) if tid is not None else 0
    except (TypeError, ValueError):
        tag_id = 0
    gid = str(rec.get("goods_id") or "")
    attempt_count = _llm_attempt_count_for_db(rec)
    can_upload_raw = rec.get("can_upload")
    can_upload_db: int | None
    if can_upload_raw is None:
        can_upload_db = None
    else:
        can_upload_db = 1 if bool(can_upload_raw) else 0
    db_path = sqlite_db_path()
    with _write_lock(db_path):
        conn.execute(
            f"""
            UPDATE pf_store_item SET
              commodity_sizes_json = COALESCE(?, commodity_sizes_json),
              commodity_colors_json = COALESCE(?, commodity_colors_json),
              sizes_ko_json = COALESCE(?, sizes_ko_json),
              colors_ko_json = COALESCE(?, colors_ko_json),
              llm_name_zh = COALESCE(?, llm_name_zh),
              llm_name_ko = COALESCE(?, llm_name_ko),
              llm_desc_zh = COALESCE(?, llm_desc_zh),
              llm_desc_ko = COALESCE(?, llm_desc_ko),
              llm_processed_at = COALESCE(?, llm_processed_at),
              price_cny = COALESCE(?, price_cny),
              llm_source = COALESCE(?, llm_source),
              llm_reason = COALESCE(?, llm_reason),
              llm_attempt_count = ?,
              can_upload = COALESCE(?, can_upload),
              updated_at = {SQLITE_NOW_CST8}
            WHERE album_id = ? AND goods_id = ? AND tag_id = ?
            """,
            (
                enrich["commodity_sizes_json"],
                enrich["commodity_colors_json"],
                enrich["sizes_ko_json"],
                enrich["colors_ko_json"],
                enrich["llm_name_zh"],
                enrich["llm_name_ko"],
                enrich["llm_desc_zh"],
                enrich["llm_desc_ko"],
                enrich["llm_processed_at"],
                enrich["price_cny"],
                enrich["llm_source"],
                enrich["llm_reason"],
                attempt_count,
                can_upload_db,
                album_id,
                gid,
                tag_id,
            ),
        )
        conn.commit()
    item_id = rec.get("id")
    if item_id is None:
        item_id = _lookup_item_id(conn, album_id, gid, tag_id)
        if item_id is not None:
            rec["id"] = item_id
    from product_feed_kr.wecatalog.wecatalog_scrape_fields import parse_colors_json, parse_sizes_json

    spec_log: list[tuple[str, Any]] = []
    for key, label in (
        ("commodity_sizes_json", "中文尺码"),
        ("commodity_colors_json", "中文颜色"),
        ("sizes_ko_json", "韩文尺码"),
        ("colors_ko_json", "韩文颜色"),
    ):
        new_raw = enrich.get(key)
        if not new_raw:
            continue
        old_raw = rec.get(key)
        if new_raw != old_raw:
            spec_log.append((f"spec_{key}", f"{pf_trunc(old_raw or '—', 80)}→{pf_trunc(new_raw, 80)}"))
        rec[key] = new_raw

    rec["commodity_sizes"] = parse_sizes_json(rec.get("commodity_sizes_json"))
    rec["commodity_colors"] = parse_colors_json(rec.get("commodity_colors_json"))
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.write"),
                ("op", "update_llm_result"),
                *pf_db_row_id_kv(rec, row_id=item_id if isinstance(item_id, int) else None),
                *spec_log,
            ],
            zh="写库：LLM 结果（含规格纠错回写）",
            val_max=500,
        ),
    )


def sqlite_update_upload_fields(conn: sqlite3.Connection, album_id: str, rec: dict[str, Any]) -> None:
    """Upload 专用写回：只更新上架相关衍生字段（汇率/韩元价/描述HTML/ca_id），不碰 LLM 结果。"""
    enrich = _extract_enriched_fields(rec)
    tid = rec.get("tag_id")
    try:
        tag_id = int(tid) if tid is not None else 0
    except (TypeError, ValueError):
        tag_id = 0
    gid = str(rec.get("goods_id") or "")
    ca_id_store = (str(rec.get("seven17_ca_id")).strip() if rec.get("seven17_ca_id") else None)
    db_path = sqlite_db_path()
    with _write_lock(db_path):
        conn.execute(
            f"""
            UPDATE pf_store_item SET
              price_krw = COALESCE(?, price_krw),
              seven17_ca_id = COALESCE(?, seven17_ca_id),
              updated_at = {SQLITE_NOW_CST8}
            WHERE album_id = ? AND goods_id = ? AND tag_id = ?
            """,
            (
                enrich["price_krw"],
                ca_id_store,
                album_id,
                gid,
                tag_id,
            ),
        )
        conn.commit()
    item_id = rec.get("id")
    if item_id is None:
        item_id = _lookup_item_id(conn, album_id, gid, tag_id)
        if item_id is not None:
            rec["id"] = item_id
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.write"),
                ("op", "update_upload_fields"),
                *pf_db_row_id_kv(rec, row_id=item_id if isinstance(item_id, int) else None),
            ],
            zh="写库：上架衍生字段（不碰 LLM 和上架状态）",
        ),
    )


def sqlite_load_store_snapshot(conn: sqlite3.Connection, album_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM pf_store_info WHERE album_id = ? LIMIT 1", (album_id,))
    row = cur.fetchone()
    if not row:
        store_id = _lookup_store_info_id(conn, album_id)
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "db.read"),
                    ("op", "load_store_snapshot"),
                    *pf_db_row_id_kv(row_id=store_id),
                    ("found", 0),
                ],
                zh="读库：店铺快照不存在",
            ),
        )
        return None
    d = _row_to_dict(row)
    store_id = int(d["id"]) if d.get("id") is not None else None
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "db.read"),
                ("op", "load_store_snapshot"),
                *pf_db_row_id_kv(row_id=store_id),
                ("found", 1),
            ],
            zh="读库：店铺快照",
        ),
    )
    stats: dict[str, Any] | None = None
    if isinstance(d.get("stats_json"), str) and str(d.get("stats_json") or "").strip():
        try:
            parsed = json.loads(d["stats_json"])
            if isinstance(parsed, dict):
                stats = parsed
        except json.JSONDecodeError:
            stats = None
    return {
        "album_id": d["album_id"],
        "store_url": d["store_url"],
        "trans_lang": d["trans_lang"],
        "detail_delay_sec": float(d["detail_delay_sec"]),
        "skip_detail": bool(d["skip_detail"]),
        "saved_at": d.get("last_saved_at"),
        "stats": stats,
    }
