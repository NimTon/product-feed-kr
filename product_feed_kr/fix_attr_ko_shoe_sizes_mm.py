"""扫描 SQLite：从 **中文** ``attr_map`` 的 ``尺码`` 读取欧码，换算后写入 ``attr_map_ko`` 的 ``사이즈``（毫米脚长）。

**不以韩文 ``사이즈`` 为换算来源**；仅对比韩文列是否已与「中文尺码换算结果」一致。

默认 **dry-run**（只列出待修复行）；加 ``--apply`` 才写库（同步 ``attr_map_ko_json`` 与 ``listing_llm_json.attr_map_ko``）。

用法::

  python -m product_feed_kr.fix_attr_ko_shoe_sizes_mm
  python -m product_feed_kr.fix_attr_ko_shoe_sizes_mm --album-id YOUR_ALBUM_ID --limit 20
  python -m product_feed_kr.fix_attr_ko_shoe_sizes_mm --apply
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from product_feed_kr.listing_llm_enrich import (
    _shoe_size_token_to_kr_mm,
    _shoe_sizes_to_kr_mm,
    _text_suggests_footwear,
)
from product_feed_kr.store_sqlite import (
    connect_sqlite,
    ensure_sqlite_schema,
    sqlite_db_path,
    sqlite_update_llm_result,
)

_SIZE_ZH = "尺码"
_SIZE_KO = "사이즈"


def _parse_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _as_size_list(vals: Any) -> list[str]:
    if not isinstance(vals, list):
        return []
    out: list[str] = []
    for v in vals:
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def _token_is_convertible_eu(tok: str) -> bool:
    """欧码数字（含 EU42、40.5）且可映射到毫米表时为 True。"""
    t = str(tok).strip()
    if not t:
        return False
    return _shoe_size_token_to_kr_mm(t) != t


def _attr_map_has_eu_numeric_sizes(attr_map: dict[str, Any]) -> bool:
    return any(_token_is_convertible_eu(s) for s in _as_size_list(attr_map.get(_SIZE_ZH)))


def _mm_sizes_from_zh(attr_map: dict[str, Any]) -> list[str] | None:
    """仅依据中文 ``尺码`` 列表换算韩版毫米脚长。"""
    zh_sizes = _as_size_list(attr_map.get(_SIZE_ZH))
    if not zh_sizes or not _attr_map_has_eu_numeric_sizes(attr_map):
        return None
    mm = _shoe_sizes_to_kr_mm(zh_sizes)
    if not mm or mm == zh_sizes:
        return None
    return mm


def _attr_map_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """优先 ``attr_map_json``；缺 ``尺码`` 时回退 ``listing_llm_json.attr_map``（仍不用韩文）。"""
    attr_map = _parse_json_obj(row.get("attr_map_json"))
    if _as_size_list(attr_map.get(_SIZE_ZH)):
        return attr_map
    ll = _parse_json_obj(row.get("listing_llm_json"))
    ll_am = ll.get("attr_map") if isinstance(ll.get("attr_map"), dict) else None
    if isinstance(ll_am, dict) and _as_size_list(ll_am.get(_SIZE_ZH)):
        merged = dict(attr_map)
        merged[_SIZE_ZH] = ll_am[_SIZE_ZH]
        if "颜色" in ll_am and "颜色" not in merged:
            merged["颜色"] = ll_am["颜色"]
        return merged
    return attr_map


def _needs_fix(
    attr_map: dict[str, Any],
    attr_map_ko: dict[str, Any],
    *,
    footwear_only: bool,
    context_text: str,
) -> tuple[bool, list[str] | None]:
    if footwear_only and not _text_suggests_footwear(context_text):
        return False, None
    mm = _mm_sizes_from_zh(attr_map)
    if not mm:
        return False, None
    ko_sizes = _as_size_list(attr_map_ko.get(_SIZE_KO))
    if ko_sizes == mm:
        return False, None
    return True, mm


def _context_text(row: dict[str, Any], attr_map: dict[str, Any]) -> str:
    parts = [
        str(row.get("commodity_title") or ""),
        str(row.get("llm_name_zh") or ""),
    ]
    ll = _parse_json_obj(row.get("listing_llm_json"))
    if isinstance(ll, dict):
        for k in ("name_zh", "name_ko", "desc_zh"):
            v = ll.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip()[:300])
    return "\n".join(p for p in parts if p)


def _scan_rows(
    conn: Any,
    *,
    album_id: str | None,
    limit: int | None,
    footwear_only: bool,
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, album_id, goods_id, tag_id, commodity_title,
               llm_name_zh, attr_map_json, attr_map_ko_json, listing_llm_json
        FROM pf_store_item
        WHERE attr_map_json IS NOT NULL AND trim(attr_map_json) != ''
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
        attr_map_ko = _parse_json_obj(row_d.get("attr_map_ko_json"))
        ctx = _context_text(row_d, attr_map)
        ok, mm = _needs_fix(
            attr_map, attr_map_ko, footwear_only=footwear_only, context_text=ctx
        )
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
                "listing_llm_json": row_d.get("listing_llm_json"),
            }
        )
    return hits


def _apply_one(conn: Any, hit: dict[str, Any]) -> None:
    attr_map_ko = dict(hit["attr_map_ko"])
    attr_map_ko[_SIZE_KO] = hit["ko_sizes_after"]
    ll = _parse_json_obj(hit.get("listing_llm_json"))
    if not ll:
        ll = {}
    ll["attr_map_ko"] = attr_map_ko
    if _SIZE_ZH not in ll and hit.get("attr_map"):
        ll["attr_map"] = hit["attr_map"]
    rec: dict[str, Any] = {
        "id": hit["id"],
        "album_id": hit["album_id"],
        "goods_id": hit["goods_id"],
        "tag_id": hit["tag_id"],
        "listing_llm": ll,
    }
    sqlite_update_llm_result(conn, str(hit["album_id"]), rec)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="attr_map 欧码数字 → attr_map_ko 사이즈 韩版毫米脚长")
    ap.add_argument("--album-id", default="", help="仅处理指定相册 album_id")
    ap.add_argument("--limit", type=int, default=0, help="最多扫描行数（0=不限制）")
    ap.add_argument("--apply", action="store_true", help="写回数据库（默认仅预览）")
    ap.add_argument(
        "--all-numeric",
        action="store_true",
        help="不校验鞋类标题/名称（默认仅处理鞋靴类语境，避免裤装 30–38 误换算）",
    )
    args = ap.parse_args(argv)

    album_filter = str(args.album_id or "").strip() or None
    scan_limit = int(args.limit) if args.limit and args.limit > 0 else None

    conn = connect_sqlite()
    try:
        ensure_sqlite_schema(conn)
        footwear_only = not bool(args.all_numeric)
        hits = _scan_rows(
            conn,
            album_id=album_filter,
            limit=scan_limit,
            footwear_only=footwear_only,
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
            print("提示: 加 --apply 写回 attr_map_ko_json / listing_llm_json")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
