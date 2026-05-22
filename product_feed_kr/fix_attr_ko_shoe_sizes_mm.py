"""扫描 SQLite：从 **中文** ``commodity_sizes_json`` 的欧码换算韩文 ``sizes_ko_json``。

默认 **dry-run**（只列出待修复行）；加 ``--apply`` 才写库（同步 ``sizes_ko_json``）。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from product_feed_kr.wecatalog_size_fix import shoe_sizes_to_kr_mm
from product_feed_kr.llm_spec_fields import (
    COLOR_ZH,
    SIZE_KO,
    SIZE_ZH,
    effective_sizes_colors_zh,
    parse_json_str_list,
)
from product_feed_kr.store_sqlite import connect_sqlite, ensure_sqlite_schema, sqlite_db_path, sqlite_update_llm_result

_SIZE_ZH = SIZE_ZH
_SIZE_KO = SIZE_KO


def _parse_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _as_size_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if raw is not None and str(raw).strip():
        return [str(raw).strip()]
    return []


def _token_is_convertible_eu(token: str) -> bool:
    s = str(token).strip()
    if not s or not s.replace(".", "", 1).isdigit():
        return False
    try:
        v = float(s)
    except ValueError:
        return False
    return 10 <= v <= 60


def _attr_map_from_row(row: dict[str, Any]) -> dict[str, Any]:
    sizes, colors = effective_sizes_colors_zh(row)
    out: dict[str, Any] = {}
    if sizes:
        out[SIZE_ZH] = sizes
    if colors:
        out[COLOR_ZH] = colors
    return out


def _attr_map_ko_from_row(row: dict[str, Any]) -> dict[str, Any]:
    sizes_ko = parse_json_str_list(row.get("sizes_ko_json"))
    colors_ko = parse_json_str_list(row.get("colors_ko_json"))
    out: dict[str, Any] = {}
    if sizes_ko:
        out[SIZE_KO] = sizes_ko
    if colors_ko:
        from product_feed_kr.llm_spec_fields import COLOR_KO

        out[COLOR_KO] = colors_ko
    return out


def _mm_sizes_from_zh(attr_map: dict[str, Any]) -> list[str] | None:
    zh_sizes = _as_size_list(attr_map.get(_SIZE_ZH))
    if not zh_sizes or not any(_token_is_convertible_eu(s) for s in zh_sizes):
        return None
    mm = shoe_sizes_to_kr_mm(zh_sizes)
    if not mm or mm == zh_sizes:
        return None
    return mm


def _needs_fix(
    attr_map: dict[str, Any],
    attr_map_ko: dict[str, Any],
) -> tuple[bool, list[str] | None]:
    mm = _mm_sizes_from_zh(attr_map)
    if not mm:
        return False, None
    ko_sizes = _as_size_list(attr_map_ko.get(_SIZE_KO))
    if ko_sizes == mm:
        return False, None
    return True, mm


def _scan_rows(
    conn: Any,
    *,
    album_id: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, album_id, goods_id, tag_id, commodity_title,
               llm_name_zh, commodity_sizes_json,
               sizes_ko_json, colors_ko_json
        FROM pf_store_item
        WHERE trim(COALESCE(commodity_sizes_json,'')) <> ''
    """
    params: list[Any] = []
    if album_id:
        sql += " AND album_id = ?"
        params.append(album_id.strip())
    sql += " ORDER BY id"
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(sql, params)
    hits: list[dict[str, Any]] = []
    for row in cur.fetchall():
        row_d = dict(row)
        attr_map = _attr_map_from_row(row_d)
        attr_map_ko = _attr_map_ko_from_row(row_d)
        ok, mm = _needs_fix(attr_map, attr_map_ko)
        if not ok or not mm:
            continue
        zh_sizes = _as_size_list(attr_map.get(_SIZE_ZH))
        ko_sizes = _as_size_list(attr_map_ko.get(_SIZE_KO))
        hits.append(
            {
                "id": row_d["id"],
                "album_id": row_d["album_id"],
                "goods_id": row_d["goods_id"],
                "tag_id": int(row_d.get("tag_id") or 0),
                "title": str(row_d.get("commodity_title") or "")[:60],
                "zh_sizes": zh_sizes,
                "ko_sizes_before": ko_sizes,
                "ko_sizes_after": mm,
                "attr_map": attr_map,
                "attr_map_ko": attr_map_ko,
                "commodity_sizes_json": row_d.get("commodity_sizes_json"),
            }
        )
    return hits


def _apply_one(conn: Any, hit: dict[str, Any]) -> None:
    attr_map_ko = dict(hit["attr_map_ko"])
    attr_map_ko[_SIZE_KO] = hit["ko_sizes_after"]
    ll: dict[str, Any] = {"attr_map_ko": attr_map_ko}
    if hit.get("attr_map"):
        ll["attr_map"] = hit["attr_map"]
    rec: dict[str, Any] = {
        "id": hit["id"],
        "album_id": hit["album_id"],
        "goods_id": hit["goods_id"],
        "tag_id": hit["tag_id"],
        "listing_llm": ll,
        "commodity_sizes_json": hit.get("commodity_sizes_json"),
    }
    sqlite_update_llm_result(conn, str(hit["album_id"]), rec)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="中文欧码 → sizes_ko_json 韩版毫米脚长")
    ap.add_argument("--album-id", default="", help="仅处理指定相册 album_id")
    ap.add_argument("--limit", type=int, default=0, help="最多扫描行数（0=不限制）")
    ap.add_argument("--apply", action="store_true", help="写回数据库（默认仅预览）")
    ap.add_argument(
        "--all-numeric",
        action="store_true",
        help="凡中文尺码为可换算欧码数字即转毫米（不依赖 LLM；慎用，可能误伤服装）",
    )
    args = ap.parse_args(argv)

    album_filter = str(args.album_id or "").strip() or None
    scan_limit = int(args.limit) if args.limit and args.limit > 0 else None

    conn = connect_sqlite()
    try:
        ensure_sqlite_schema(conn)
        if not args.all_numeric:
            print("未加 --all-numeric：本脚本不猜鞋类，请用 02 LLM 标注 size_spec_kind 后写库。")
            hits = []
        else:
            hits = _scan_rows(
                conn,
                album_id=album_filter,
                limit=scan_limit,
            )
        print(f"db: {sqlite_db_path()}")
        print(f"待修复（中文尺码已换算，韩文 사이즈 未对齐）: {len(hits)} 条")
        for h in hits:
            print(
                f"  id={h['id']} goods_id={h['goods_id']} tag_id={h['tag_id']}\n"
                f"    尺码(zh): {h['zh_sizes']}\n"
                f"    사이즈(ko 前): {h['ko_sizes_before'] or '(空)'}\n"
                f"    사이즈(ko 后): {h['ko_sizes_after']}"
            )
        if args.apply and hits:
            for h in hits:
                _apply_one(conn, h)
            print(f"已写库: {len(hits)} 条")
        elif args.apply:
            print("无待修复记录，未写库")
        elif hits:
            print("提示: 加 --apply 写回 sizes_ko_json")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
