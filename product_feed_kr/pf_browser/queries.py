"""pf_store_item 只读查询（分页 / 排序 / 筛选）。"""

from __future__ import annotations

import json
from typing import Any

from product_feed_kr.db.llm_spec_fields import (
    effective_sizes_colors_ko,
    effective_sizes_colors_zh,
    parse_json_str_list,
)
from product_feed_kr.pf_browser.status_reasons import enrich_status_reasons
from product_feed_kr.db.store_sqlite import connect_sqlite, sqlite_db_path

_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50

_SORT_SQL: dict[str, str] = {
    "llm": """
        CASE WHEN llm_processed_at IS NULL OR trim(llm_processed_at) = '' THEN 1 ELSE 0 END,
        llm_processed_at DESC,
        CASE WHEN seven17_uploaded_at IS NULL OR trim(seven17_uploaded_at) = '' THEN 1 ELSE 0 END,
        seven17_uploaded_at DESC,
        id DESC
    """,
    "upload": """
        CASE WHEN seven17_uploaded_at IS NULL OR trim(seven17_uploaded_at) = '' THEN 1 ELSE 0 END,
        seven17_uploaded_at DESC,
        CASE WHEN llm_processed_at IS NULL OR trim(llm_processed_at) = '' THEN 1 ELSE 0 END,
        llm_processed_at DESC,
        id DESC
    """,
    "updated": "updated_at DESC, id DESC",
    "created": "created_at DESC, id DESC",
}

# 列表 SELECT 列顺序（与表头分组一致）
_LIST_COLS: tuple[str, ...] = (
    # 状态与时间
    "id",
    "can_process",
    "can_upload",
    "rescrape_pending",
    "uploaded_to_platform",
    "llm_attempt_count",
    "llm_processed_at",
    "seven17_uploaded_at",
    # 标识与微猫
    "album_id",
    "goods_id",
    "tag_id",
    "wecatalog_group",
    "wecatalog_tag",
    "commodity_title",
    "wecatalog_listed_at",
    "commodity_goods_num",
    "goods_url",
    "first_image_hash",
    "commodity_image_urls_json",
    # 价格
    "price_cny",
    "price_krw",
    # 规格 JSON（后端拼 preview）
    "commodity_sizes_json",
    "commodity_colors_json",
    "sizes_ko_json",
    "colors_ko_json",
    # LLM 文案
    "llm_name_zh",
    "llm_name_ko",
    "llm_desc_zh",
    "llm_desc_ko",
    "llm_source",
    "llm_reason",
    # 上架
    "seven17_ca_id",
    "created_at",
    "updated_at",
)

_DETAIL_EXTRA: tuple[str, ...] = (
    "shop_category_path_json",
    "commodity_image_urls_json",
    "commodity_tag_names_json",
    "first_image_hash",
)


def _table_cols(conn) -> set[str]:
    cur = conn.execute("PRAGMA table_info(pf_store_item)")
    return {str(r[1]) for r in cur.fetchall()}


def _select_cols(conn, *, detail: bool = False) -> str:
    names = set(_table_cols(conn))
    cols = [c for c in _LIST_COLS if c in names]
    if detail:
        for c in _DETAIL_EXTRA:
            if c in names and c not in cols:
                cols.append(c)
    return ", ".join(cols)


def _clamp_page(page: int) -> int:
    return max(1, int(page))


def _clamp_page_size(page_size: int) -> int:
    n = int(page_size)
    if n < 1:
        n = _DEFAULT_PAGE_SIZE
    return min(n, _MAX_PAGE_SIZE)


def _sort_key(sort: str | None) -> str:
    k = (sort or "llm").strip().lower()
    return k if k in _SORT_SQL else "llm"


def _looks_like_image_hash(qs: str) -> bool:
    """首图 hash 为 SHA1 hex（40 位）；允许粘贴完整或较长前缀。"""
    s = qs.strip()
    return 8 <= len(s) <= 64 and all(c in "0123456789abcdefABCDEF" for c in s)


def _where_clause(
    *,
    album_id: str | None,
    q: str | None,
) -> tuple[str, list[Any]]:
    parts: list[str] = []
    params: list[Any] = []
    if album_id and str(album_id).strip():
        parts.append("album_id = ?")
        params.append(str(album_id).strip())
    if q and str(q).strip():
        qs = str(q).strip()
        like = f"%{qs}%"
        text_conds = (
            "commodity_title LIKE ? OR goods_id LIKE ? OR commodity_goods_num LIKE ?"
            " OR llm_name_zh LIKE ? OR llm_name_ko LIKE ?"
            " OR llm_desc_zh LIKE ? OR llm_desc_ko LIKE ?"
            " OR price_cny LIKE ? OR CAST(id AS TEXT) LIKE ?"
            " OR first_image_hash LIKE ?"
        )
        like_params = [like] * 10
        exact_conds: list[str] = []
        exact_params: list[Any] = []
        if qs.isdigit():
            exact_conds.extend(["id = ?", "TRIM(commodity_goods_num) = ?"])
            exact_params.extend([int(qs), qs])
        elif _looks_like_image_hash(qs):
            exact_conds.append("LOWER(TRIM(first_image_hash)) = LOWER(?)")
            exact_params.append(qs)
        if exact_conds:
            parts.append(f"({' OR '.join(exact_conds)} OR ({text_conds}))")
            params.extend(exact_params)
            params.extend(like_params)
        else:
            parts.append(f"({text_conds})")
            params.extend(like_params)
    if not parts:
        return "", params
    return " WHERE " + " AND ".join(parts), params


def _text_short(text: Any, *, max_len: int = 80) -> str:
    s = str(text or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    if not s:
        return ""
    one_line = " ".join(s.split())
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 1] + "…"


def _list_preview(items: list[str], *, max_show: int = 6) -> str:
    if not items:
        return ""
    if len(items) <= max_show:
        return "、".join(items)
    return "、".join(items[:max_show]) + f"…(+{len(items) - max_show})"


def _apply_display_fields(d: dict[str, Any]) -> None:
    """列表/详情展示用派生字段（中文规格=爬取优先否则兜底；韩文=翻译列）。"""
    sizes_zh, colors_zh = effective_sizes_colors_zh(d)
    sizes_ko, colors_ko = effective_sizes_colors_ko(d)

    d["sizes"] = sizes_zh
    d["colors"] = colors_zh
    d["sizes_ko"] = sizes_ko
    d["colors_ko"] = colors_ko

    d["sizes_preview"] = _list_preview(sizes_zh)
    d["colors_preview"] = _list_preview(colors_zh)
    d["sizes_ko_preview"] = _list_preview(sizes_ko)
    d["colors_ko_preview"] = _list_preview(colors_ko)

    urls = parse_json_str_list(d.get("commodity_image_urls_json"))
    tags = parse_json_str_list(d.get("commodity_tag_names_json"))
    d["image_count"] = len(urls)
    d["image_urls"] = urls
    d["tag_names"] = tags
    d["tag_names_preview"] = _list_preview(tags, max_show=4)

    title = str(d.get("commodity_title") or "")
    d["commodity_title_short"] = _text_short(title, max_len=100) or title
    d["llm_name_zh_short"] = _text_short(d.get("llm_name_zh"), max_len=40)
    d["llm_name_ko_short"] = _text_short(d.get("llm_name_ko"), max_len=40)
    d["llm_desc_zh_preview"] = _text_short(d.get("llm_desc_zh"), max_len=80)
    d["llm_desc_ko_preview"] = _text_short(d.get("llm_desc_ko"), max_len=80)

    cny = str(d.get("price_cny") or "").strip()
    krw = str(d.get("price_krw") or "").strip()
    d["price_cny_display"] = cny or ""
    d["price_krw_display"] = krw or ""

    grp = str(d.get("wecatalog_group") or "").strip()
    tag = str(d.get("wecatalog_tag") or "").strip()
    d["wecatalog_label"] = f"{grp} / {tag}" if grp or tag else "—"


def _row_to_item(row: Any) -> dict[str, Any]:
    d = dict(row)
    for flag in ("can_process", "can_upload", "uploaded_to_platform", "rescrape_pending"):
        d[flag] = bool(d.get(flag))
    try:
        d["llm_attempt_count"] = int(d.get("llm_attempt_count") or 0)
    except (TypeError, ValueError):
        d["llm_attempt_count"] = 0
    d["llm_processed"] = bool(str(d.get("llm_processed_at") or "").strip())
    _apply_display_fields(d)
    return d


def _finalize_items(conn, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enrich_status_reasons(conn, items)
    return items


def list_albums() -> list[dict[str, Any]]:
    conn = connect_sqlite()
    try:
        cur = conn.execute(
            """
            SELECT album_id, COUNT(*) AS item_count,
                   SUM(CASE WHEN trim(COALESCE(llm_processed_at,'')) <> '' THEN 1 ELSE 0 END) AS llm_count,
                   SUM(CASE WHEN uploaded_to_platform = 1 THEN 1 ELSE 0 END) AS uploaded_count
            FROM pf_store_item
            GROUP BY album_id
            ORDER BY item_count DESC, album_id
            """,
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_items(
    *,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    album_id: str | None = None,
    q: str | None = None,
    sort: str | None = "llm",
) -> dict[str, Any]:
    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size)
    sort_key = _sort_key(sort)
    where_sql, params = _where_clause(album_id=album_id, q=q)
    offset = (page - 1) * page_size

    conn = connect_sqlite()
    try:
        count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM pf_store_item{where_sql}",
            params,
        ).fetchone()
        total = int(count_row["n"]) if count_row else 0

        order_sql = _SORT_SQL[sort_key]
        select_cols = _select_cols(conn)
        cur = conn.execute(
            f"""
            SELECT {select_cols}
            FROM pf_store_item
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        )
        items = [_row_to_item(r) for r in cur.fetchall()]
        _finalize_items(conn, items)
    finally:
        conn.close()

    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    return {
        "db_path": str(sqlite_db_path()),
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
        "sort": sort_key,
        "items": items,
    }


def get_item(item_id: int) -> dict[str, Any] | None:
    conn = connect_sqlite()
    try:
        select_cols = _select_cols(conn, detail=True)
        cur = conn.execute(
            f"""
            SELECT {select_cols}
            FROM pf_store_item
            WHERE id = ?
            LIMIT 1
            """,
            (int(item_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        item = _row_to_item(row)
        _finalize_items(conn, [item])
        raw_path = row["shop_category_path_json"] if "shop_category_path_json" in row.keys() else None
        if isinstance(raw_path, str) and raw_path.strip():
            try:
                item["shop_category_path"] = json.loads(raw_path)
            except json.JSONDecodeError:
                item["shop_category_path"] = raw_path
        return item
    finally:
        conn.close()
