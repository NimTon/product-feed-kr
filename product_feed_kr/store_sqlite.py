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

from product_feed_kr.pf_log import pf_db_row_id_kv, pf_kv
from product_feed_kr.pf_time import SQLITE_NOW_CST8
from product_feed_kr.seven17_config import getenv as _cfg_get

_log = logging.getLogger("product_feed_kr.store_sqlite")

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema_pf_sqlite.sql"

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
  commodity_price_raw TEXT,
  commodity_goods_num TEXT,
  commodity_image_urls_json TEXT,
  commodity_tag_names_json TEXT,
  fx_krw_per_cny REAL,
  price_krw TEXT,
  attr_map_json TEXT,
  attr_map_ko_json TEXT,
  llm_name_zh TEXT,
  llm_name_ko TEXT,
  llm_desc_zh TEXT,
  llm_desc_ko TEXT,
  product_desc_html TEXT,
  detail_response_json TEXT,
  listing_llm_json TEXT,
  UNIQUE (album_id, goods_id, tag_id)
)
"""

_PF_STORE_ITEM_COLS: tuple[str, ...] = (
    "id",
    "tag_id",
    "llm_attempt_count",
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
    "commodity_price_raw",
    "commodity_goods_num",
    "commodity_image_urls_json",
    "commodity_tag_names_json",
    "fx_krw_per_cny",
    "price_krw",
    "attr_map_json",
    "attr_map_ko_json",
    "llm_name_zh",
    "llm_name_ko",
    "llm_desc_zh",
    "llm_desc_ko",
    "product_desc_html",
    "detail_response_json",
    "listing_llm_json",
)


def sqlite_db_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    raw = (_cfg_get("PRODUCT_FEED_SQLITE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p.resolve() if p.is_absolute() else (root / p).resolve()
    return (root / "data" / "product_feed.db").resolve()


def _write_lock(db_path: Path) -> FileLock:
    return FileLock(str(db_path) + ".lock", timeout=-1)


def connect_sqlite() -> sqlite3.Connection:
    path = sqlite_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=120.0, isolation_level="DEFERRED")
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
    if col in ("created_at", "updated_at"):
        return f"datetime('now', '+8 hours')"
    if col == "id" and "id" not in old_names:
        return "rowid"
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
    if "seven17_ca_id" not in cols:
        conn.execute("ALTER TABLE pf_store_item ADD COLUMN seven17_ca_id TEXT")


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


def ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
    if not _SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"未找到建表脚本: {_SCHEMA_PATH}")
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    with _write_lock(sqlite_db_path()):
        conn.executescript(sql)
        _migrate_sqlite_columns(conn)
        _migrate_int_pk(conn)
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _extract_min_fields_from_detail(detail_response: dict[str, Any] | None) -> dict[str, Any]:
    """从 detail_response.result.commodity 提取上架最小必需字段。"""
    empty = {
        "commodity_title": "",
        "commodity_price_raw": None,
        "commodity_goods_num": None,
        "commodity_image_urls_json": None,
        "commodity_tag_names_json": None,
    }
    if not isinstance(detail_response, dict):
        return empty
    result = detail_response.get("result")
    if not isinstance(result, dict):
        return empty
    commodity = result.get("commodity")
    if not isinstance(commodity, dict):
        return empty

    title = str(commodity.get("title") or "").strip()
    raw_price = commodity.get("optimaPrice")
    if raw_price is not None and str(raw_price).strip() in ("", "-1"):
        raw_price = None
    if raw_price is None:
        arr = commodity.get("priceArr") or []
        if isinstance(arr, list) and arr:
            first = arr[0]
            if isinstance(first, dict):
                v = first.get("value")
                if v is not None and str(v).strip() not in ("", "-1"):
                    raw_price = v
    goods_num = str(commodity.get("goodsNum") or "").strip() or None

    urls_raw = commodity.get("imgsSrc") or commodity.get("imgs") or []
    image_urls: list[str] = []
    if isinstance(urls_raw, list):
        for u in urls_raw:
            if not isinstance(u, str):
                continue
            s = u.strip().split("|")[0].strip()
            if s.startswith(("http://", "https://")):
                image_urls.append(s)

    tags_raw = commodity.get("tags") or []
    tag_names: list[str] = []
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, dict) and t.get("tagName"):
                tag_names.append(str(t["tagName"]).strip())

    return {
        "commodity_title": title,
        "commodity_price_raw": (str(raw_price).strip() if raw_price is not None else None),
        "commodity_goods_num": goods_num,
        "commodity_image_urls_json": json.dumps(image_urls, ensure_ascii=False) if image_urls else None,
        "commodity_tag_names_json": json.dumps(tag_names, ensure_ascii=False) if tag_names else None,
    }


def _extract_enriched_fields(rec: dict[str, Any]) -> dict[str, Any]:
    """从记录中提取上架 enrich 衍生字段（汇率/韩元价/attr_map/中韩文名称与描述/商品描述HTML）。"""
    fx_raw = rec.get("fx_krw_per_cny")
    fx_val: float | None = None
    try:
        if fx_raw is not None and str(fx_raw).strip() != "":
            fx_val = float(str(fx_raw).strip())
    except (TypeError, ValueError):
        fx_val = None

    price_krw_raw = rec.get("price_krw")
    price_krw = str(price_krw_raw).strip() if price_krw_raw is not None else None
    if price_krw == "":
        price_krw = None

    ll = rec.get("listing_llm")
    attr_map_json: str | None = None
    attr_map_ko_json: str | None = None
    llm_name_zh: str | None = None
    llm_name_ko: str | None = None
    llm_desc_zh: str | None = None
    llm_desc_ko: str | None = None
    llm_processed_at: str | None = None
    if isinstance(ll, dict) and isinstance(ll.get("attr_map"), dict):
        attr_map_json = json.dumps(ll["attr_map"], ensure_ascii=False)
    if isinstance(ll, dict) and isinstance(ll.get("attr_map_ko"), dict):
        attr_map_ko_json = json.dumps(ll["attr_map_ko"], ensure_ascii=False)
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

    desc_raw = rec.get("product_desc_html")
    desc_html = str(desc_raw) if isinstance(desc_raw, str) and desc_raw.strip() else None

    return {
        "fx_krw_per_cny": fx_val,
        "price_krw": price_krw,
        "attr_map_json": attr_map_json,
        "attr_map_ko_json": attr_map_ko_json,
        "llm_name_zh": llm_name_zh,
        "llm_name_ko": llm_name_ko,
        "llm_desc_zh": llm_desc_zh,
        "llm_desc_ko": llm_desc_ko,
        "llm_processed_at": llm_processed_at,
        "product_desc_html": desc_html,
    }


def row_to_product_record(row: dict[str, Any]) -> dict[str, Any]:
    drj = row.get("detail_response_json")
    if isinstance(drj, str) and drj.strip():
        try:
            detail_response = json.loads(drj)
        except json.JSONDecodeError:
            detail_response = None
    else:
        detail_response = None
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
        "uploaded_to_platform": bool(row["uploaded_to_platform"]),
        "seven17_uploaded_at": (str(row.get("seven17_uploaded_at")).strip() if row.get("seven17_uploaded_at") is not None else None),
        "seven17_ca_id": (str(ca_raw).strip() if ca_raw is not None and str(ca_raw).strip() else None),
        "detail_response": detail_response,
    })
    scp = row.get("shop_category_path_json")
    if isinstance(scp, str) and scp.strip():
        try:
            rec["shop_category_path"] = json.loads(scp)
        except json.JSONDecodeError:
            rec["shop_category_path"] = None
    ll = row.get("listing_llm_json")
    if isinstance(ll, str) and ll.strip():
        try:
            rec["listing_llm"] = json.loads(ll)
        except json.JSONDecodeError:
            pass
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

    rec["commodity_min"] = {
        "title": str(row.get("commodity_title") or "").strip(),
        "price_raw": (str(row.get("commodity_price_raw")).strip() if row.get("commodity_price_raw") is not None else None),
        "goods_num": (str(row.get("commodity_goods_num") or "").strip() or None),
        "image_urls": image_urls,
        "tag_names": tag_names,
        "fx_krw_per_cny": row.get("fx_krw_per_cny"),
        "price_krw": (str(row.get("price_krw")).strip() if row.get("price_krw") is not None else None),
        "attr_map": json.loads(row["attr_map_json"])
        if isinstance(row.get("attr_map_json"), str) and str(row.get("attr_map_json") or "").strip()
        else {},
        "attr_map_ko": json.loads(row["attr_map_ko_json"])
        if isinstance(row.get("attr_map_ko_json"), str) and str(row.get("attr_map_ko_json") or "").strip()
        else {},
        "llm_name_zh": (str(row.get("llm_name_zh")).strip() if row.get("llm_name_zh") is not None else None),
        "llm_name_ko": (str(row.get("llm_name_ko")).strip() if row.get("llm_name_ko") is not None else None),
        "llm_desc_zh": (str(row.get("llm_desc_zh")).strip() if row.get("llm_desc_zh") is not None else None),
        "llm_desc_ko": (str(row.get("llm_desc_ko")).strip() if row.get("llm_desc_ko") is not None else None),
        "llm_processed_at": (str(row.get("llm_processed_at")).strip() if row.get("llm_processed_at") is not None else None),
        "seven17_uploaded_at": (str(row.get("seven17_uploaded_at")).strip() if row.get("seven17_uploaded_at") is not None else None),
        "product_desc_html": (str(row.get("product_desc_html")) if row.get("product_desc_html") is not None else None),
    }
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
    """抓取入库：仅当 ``detail_response`` 含有效 ``result.commodity`` 时写 ``pf_store_item``。"""
    dr = rec.get("detail_response")
    if not isinstance(dr, dict):
        return False
    if dr.get("errcode") not in (0, None):
        return False
    res = dr.get("result")
    if not isinstance(res, dict):
        return False
    return isinstance(res.get("commodity"), dict)


def sqlite_count_store_items(conn: sqlite3.Connection, album_id: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM pf_store_item WHERE album_id = ?",
        (album_id,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def sqlite_load_existing_goods_ids(conn: sqlite3.Connection, album_id: str) -> tuple[set[str], int]:
    """返回 (已有 goods_id 集合, 库内商品行数)；不加载整行，避免 checkpoint 用旧快照覆盖 LLM。"""
    total = sqlite_count_store_items(conn, album_id)
    cur = conn.execute(
        "SELECT DISTINCT goods_id FROM pf_store_item WHERE album_id = ?",
        (album_id,),
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


def sqlite_load_products_for_upload(
    conn: sqlite3.Connection,
    album_id: str,
    *,
    skip_uploaded: bool = True,
) -> list[dict[str, Any]]:
    """载入商品行，按表主键 ``id`` 正序（先入先处理）。"""
    if skip_uploaded:
        cur = conn.execute(
            "SELECT * FROM pf_store_item WHERE album_id = ? AND seven17_uploaded_at IS NULL ORDER BY id ASC",
            (album_id,),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM pf_store_item WHERE album_id = ? ORDER BY id ASC",
            (album_id,),
        )
    rows = [row_to_product_record(_row_to_dict(r)) for r in cur.fetchall()]
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
            ],
            zh="读库：载入待上架/处理商品列表",
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


def sqlite_upsert_scrape_items(
    conn: sqlite3.Connection,
    album_id: str,
    records: list[dict[str, Any]],
) -> int:
    """仅 upsert 本批已抓到有效详情的商品行；不触碰仅有 goods_id、无详情的记录。"""
    to_write = [rec for rec in records if scrape_detail_ready(rec)]
    if not to_write:
        return 0
    db_path = sqlite_db_path()
    with _write_lock(db_path):
        sql_prod = f"""
            INSERT INTO pf_store_item (
              album_id, goods_id, tag_id, wecatalog_group, wecatalog_tag,
              shop_category_path_json, goods_url, uploaded_to_platform, seven17_uploaded_at,
              commodity_title, commodity_price_raw, commodity_goods_num,
              commodity_image_urls_json, commodity_tag_names_json,
              fx_krw_per_cny, price_krw, attr_map_json, attr_map_ko_json,
              llm_name_zh, llm_name_ko, llm_desc_zh, llm_desc_ko, llm_processed_at, llm_attempt_count,
              product_desc_html, detail_response_json, listing_llm_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(album_id, goods_id, tag_id) DO UPDATE SET
              wecatalog_group = excluded.wecatalog_group,
              wecatalog_tag = excluded.wecatalog_tag,
              shop_category_path_json = excluded.shop_category_path_json,
              goods_url = excluded.goods_url,
              uploaded_to_platform = excluded.uploaded_to_platform,
              seven17_uploaded_at = excluded.seven17_uploaded_at,
              commodity_title = excluded.commodity_title,
              commodity_price_raw = excluded.commodity_price_raw,
              commodity_goods_num = excluded.commodity_goods_num,
              commodity_image_urls_json = excluded.commodity_image_urls_json,
              commodity_tag_names_json = excluded.commodity_tag_names_json,
              fx_krw_per_cny = COALESCE(excluded.fx_krw_per_cny, pf_store_item.fx_krw_per_cny),
              price_krw = COALESCE(excluded.price_krw, pf_store_item.price_krw),
              attr_map_json = COALESCE(excluded.attr_map_json, pf_store_item.attr_map_json),
              attr_map_ko_json = COALESCE(excluded.attr_map_ko_json, pf_store_item.attr_map_ko_json),
              llm_name_zh = COALESCE(excluded.llm_name_zh, pf_store_item.llm_name_zh),
              llm_name_ko = COALESCE(excluded.llm_name_ko, pf_store_item.llm_name_ko),
              llm_desc_zh = COALESCE(excluded.llm_desc_zh, pf_store_item.llm_desc_zh),
              llm_desc_ko = COALESCE(excluded.llm_desc_ko, pf_store_item.llm_desc_ko),
              llm_processed_at = COALESCE(excluded.llm_processed_at, pf_store_item.llm_processed_at),
              llm_attempt_count = pf_store_item.llm_attempt_count,
              product_desc_html = COALESCE(excluded.product_desc_html, pf_store_item.product_desc_html),
              detail_response_json = excluded.detail_response_json,
              listing_llm_json = COALESCE(excluded.listing_llm_json, pf_store_item.listing_llm_json),
              updated_at = {SQLITE_NOW_CST8}
            """
        for rec in to_write:
            dr = rec.get("detail_response")
            assert isinstance(dr, dict)
            dr_json = json.dumps(dr, ensure_ascii=False)
            mins = _extract_min_fields_from_detail(dr)
            ll = rec.get("listing_llm")
            ll_json = json.dumps(ll, ensure_ascii=False) if isinstance(ll, dict) else None
            enrich = _extract_enriched_fields(rec)
            scp = rec.get("shop_category_path")
            scp_json = json.dumps(scp, ensure_ascii=False) if isinstance(scp, list) else None
            tid = rec.get("tag_id")
            try:
                tag_id = int(tid) if tid is not None else 0
            except (TypeError, ValueError):
                tag_id = 0
            gid = str(rec.get("goods_id") or "")
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
                    1 if rec.get("uploaded_to_platform") is True else 0,
                    (str(rec.get("seven17_uploaded_at")).strip() if rec.get("seven17_uploaded_at") else None),
                    mins["commodity_title"],
                    mins["commodity_price_raw"],
                    mins["commodity_goods_num"],
                    mins["commodity_image_urls_json"],
                    mins["commodity_tag_names_json"],
                    enrich["fx_krw_per_cny"],
                    enrich["price_krw"],
                    enrich["attr_map_json"],
                    enrich["attr_map_ko_json"],
                    enrich["llm_name_zh"],
                    enrich["llm_name_ko"],
                    enrich["llm_desc_zh"],
                    enrich["llm_desc_ko"],
                    enrich["llm_processed_at"],
                    _llm_attempt_count_for_db(rec),
                    enrich["product_desc_html"],
                    dr_json,
                    ll_json,
                ),
            )
            item_id = _lookup_item_id(conn, album_id, gid, tag_id)
            if item_id is not None:
                rec["id"] = item_id
            _log.info(
                "%s",
                pf_kv(
                    [
                        ("event", "db.write"),
                        ("op", "upsert_item"),
                        *pf_db_row_id_kv(rec, row_id=item_id),
                    ],
                    zh="写库：upsert 商品行",
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


def sqlite_update_product_row(conn: sqlite3.Connection, album_id: str, rec: dict[str, Any]) -> None:
    dr = rec.get("detail_response")
    ll = rec.get("listing_llm")
    ll_json = json.dumps(ll, ensure_ascii=False) if isinstance(ll, dict) else None
    enrich = _extract_enriched_fields(rec)
    tid = rec.get("tag_id")
    try:
        tag_id = int(tid) if tid is not None else 0
    except (TypeError, ValueError):
        tag_id = 0
    uploaded = 1 if rec.get("uploaded_to_platform") is True else 0
    uploaded_at = (str(rec.get("seven17_uploaded_at")).strip() if rec.get("seven17_uploaded_at") else None)
    gid = str(rec.get("goods_id") or "")
    attempt_count = _llm_attempt_count_for_db(rec)
    ca_id_store = (str(rec.get("seven17_ca_id")).strip() if rec.get("seven17_ca_id") else None)
    db_path = sqlite_db_path()
    with _write_lock(db_path):
        if isinstance(dr, dict):
            mins = _extract_min_fields_from_detail(dr)
            conn.execute(
                f"""
                UPDATE pf_store_item SET
                  commodity_title = ?,
                  commodity_price_raw = ?,
                  commodity_goods_num = ?,
                  commodity_image_urls_json = ?,
                  commodity_tag_names_json = ?,
                  fx_krw_per_cny = COALESCE(?, fx_krw_per_cny),
                  price_krw = COALESCE(?, price_krw),
                  attr_map_json = COALESCE(?, attr_map_json),
                  attr_map_ko_json = COALESCE(?, attr_map_ko_json),
                  llm_name_zh = COALESCE(?, llm_name_zh),
                  llm_name_ko = COALESCE(?, llm_name_ko),
                  llm_desc_zh = COALESCE(?, llm_desc_zh),
                  llm_desc_ko = COALESCE(?, llm_desc_ko),
                  llm_processed_at = COALESCE(?, llm_processed_at),
                  llm_attempt_count = ?,
                  product_desc_html = COALESCE(?, product_desc_html),
                  detail_response_json = ?,
                  listing_llm_json = COALESCE(?, listing_llm_json),
                  uploaded_to_platform = ?,
                  seven17_uploaded_at = ?,
                  seven17_ca_id = COALESCE(?, seven17_ca_id),
                  updated_at = {SQLITE_NOW_CST8}
                WHERE album_id = ? AND goods_id = ? AND tag_id = ?
                """,
                (
                    mins["commodity_title"],
                    mins["commodity_price_raw"],
                    mins["commodity_goods_num"],
                    mins["commodity_image_urls_json"],
                    mins["commodity_tag_names_json"],
                    enrich["fx_krw_per_cny"],
                    enrich["price_krw"],
                    enrich["attr_map_json"],
                    enrich["attr_map_ko_json"],
                    enrich["llm_name_zh"],
                    enrich["llm_name_ko"],
                    enrich["llm_desc_zh"],
                    enrich["llm_desc_ko"],
                    enrich["llm_processed_at"],
                    attempt_count,
                    enrich["product_desc_html"],
                    json.dumps(dr, ensure_ascii=False),
                    ll_json,
                    uploaded,
                    uploaded_at,
                    ca_id_store,
                    album_id,
                    gid,
                    tag_id,
                ),
            )
        elif ll_json is not None:
            conn.execute(
                f"""
                UPDATE pf_store_item SET
                  listing_llm_json = ?,
                  fx_krw_per_cny = COALESCE(?, fx_krw_per_cny),
                  price_krw = COALESCE(?, price_krw),
                  attr_map_json = COALESCE(?, attr_map_json),
                  attr_map_ko_json = COALESCE(?, attr_map_ko_json),
                  llm_name_zh = COALESCE(?, llm_name_zh),
                  llm_name_ko = COALESCE(?, llm_name_ko),
                  llm_desc_zh = COALESCE(?, llm_desc_zh),
                  llm_desc_ko = COALESCE(?, llm_desc_ko),
                  llm_processed_at = COALESCE(?, llm_processed_at),
                  llm_attempt_count = ?,
                  product_desc_html = COALESCE(?, product_desc_html),
                  uploaded_to_platform = ?,
                  seven17_uploaded_at = ?,
                  seven17_ca_id = COALESCE(?, seven17_ca_id),
                  updated_at = {SQLITE_NOW_CST8}
                WHERE album_id = ? AND goods_id = ? AND tag_id = ?
                """,
                (
                    ll_json,
                    enrich["fx_krw_per_cny"],
                    enrich["price_krw"],
                    enrich["attr_map_json"],
                    enrich["attr_map_ko_json"],
                    enrich["llm_name_zh"],
                    enrich["llm_name_ko"],
                    enrich["llm_desc_zh"],
                    enrich["llm_desc_ko"],
                    enrich["llm_processed_at"],
                    attempt_count,
                    enrich["product_desc_html"],
                    uploaded,
                    uploaded_at,
                    ca_id_store,
                    album_id,
                    gid,
                    tag_id,
                ),
            )
        else:
            conn.execute(
                f"""
                UPDATE pf_store_item SET
                  fx_krw_per_cny = COALESCE(?, fx_krw_per_cny),
                  price_krw = COALESCE(?, price_krw),
                  llm_name_zh = COALESCE(?, llm_name_zh),
                  llm_name_ko = COALESCE(?, llm_name_ko),
                  llm_desc_zh = COALESCE(?, llm_desc_zh),
                  llm_desc_ko = COALESCE(?, llm_desc_ko),
                  llm_processed_at = COALESCE(?, llm_processed_at),
                  llm_attempt_count = ?,
                  product_desc_html = COALESCE(?, product_desc_html),
                  uploaded_to_platform = ?,
                  seven17_uploaded_at = ?,
                  seven17_ca_id = COALESCE(?, seven17_ca_id),
                  updated_at = {SQLITE_NOW_CST8}
                WHERE album_id = ? AND goods_id = ? AND tag_id = ?
                """,
                (
                    enrich["fx_krw_per_cny"],
                    enrich["price_krw"],
                    enrich["llm_name_zh"],
                    enrich["llm_name_ko"],
                    enrich["llm_desc_zh"],
                    enrich["llm_desc_ko"],
                    enrich["llm_processed_at"],
                    attempt_count,
                    enrich["product_desc_html"],
                    uploaded,
                    uploaded_at,
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
                ("op", "update_product_row"),
                *pf_db_row_id_kv(rec, row_id=item_id if isinstance(item_id, int) else None),
            ],
            zh="写库：更新商品行",
        ),
    )


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
