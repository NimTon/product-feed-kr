"""按当前 CNY→KRW 汇率，为库内所有有 ``price_cny`` 的商品重算 ``price_krw``。

默认仅预览；加 ``--apply`` 写库。汇率与抓取一致：``resolve_krw_per_cny()``（配置 manual / 实时 / fallback）。

用法::

    python -m product_feed_kr.recalc_price_krw_db
    python -m product_feed_kr.recalc_price_krw_db --apply
    python -m product_feed_kr.recalc_price_krw_db --apply --force
"""

from __future__ import annotations

import argparse
from typing import Any

from product_feed_kr.cny_krw_rate import cny_amount_to_krw_won_str, resolve_krw_per_cny
from product_feed_kr.listing_llm_enrich import _cny_price_field_usable, _krw_price_field_usable
from product_feed_kr.pf_log import pf_trunc
from product_feed_kr.pf_time import SQLITE_NOW_CST8
from product_feed_kr.store_sqlite import connect_sqlite, ensure_sqlite_schema, sqlite_db_path


def _safe_console(s: str, *, max_len: int = 40) -> str:
    t = pf_trunc(str(s or ""), max_len)
    return t.encode("gbk", errors="replace").decode("gbk")


def _norm_krw(raw: Any) -> str | None:
    if not _krw_price_field_usable(raw):
        return None
    return str(raw).strip().replace(",", "")


def _scan_rows(
    conn: Any,
    *,
    album_id: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, album_id, goods_id, tag_id, commodity_title,
               price_cny, price_krw
        FROM pf_store_item
    """
    params: list[Any] = []
    if album_id:
        sql += " WHERE album_id = ?"
        params.append(album_id.strip())
    sql += " ORDER BY id"
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _plan_updates(
    rows: list[dict[str, Any]],
    *,
    rate: float,
    force: bool,
    clear_no_cny: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats = {
        "total": len(rows),
        "no_cny": 0,
        "no_krw_computed": 0,
        "unchanged": 0,
        "to_update": 0,
        "to_clear": 0,
    }
    updates: list[dict[str, Any]] = []
    for row in rows:
        cny = row.get("price_cny")
        if not _cny_price_field_usable(cny):
            stats["no_cny"] += 1
            if clear_no_cny and _norm_krw(row.get("price_krw")) is not None:
                updates.append({**row, "price_krw_new": None})
                stats["to_clear"] += 1
            continue
        krw_new = cny_amount_to_krw_won_str(str(cny).strip(), rate)
        if not krw_new:
            stats["no_krw_computed"] += 1
            continue
        krw_old = _norm_krw(row.get("price_krw"))
        if not force and krw_old == krw_new:
            stats["unchanged"] += 1
            continue
        updates.append({**row, "price_krw_new": krw_new})
        stats["to_update"] += 1
    return updates, stats


def _apply_updates(conn: Any, updates: list[dict[str, Any]]) -> int:
    if not updates:
        return 0
    conn.executemany(
        f"""
        UPDATE pf_store_item
        SET price_krw = ?, updated_at = {SQLITE_NOW_CST8}
        WHERE id = ?
        """,
        [(u.get("price_krw_new"), u["id"]) for u in updates],
    )
    conn.commit()
    return len(updates)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按 CNY 价为库内商品重算 price_krw（千韩元取整）")
    ap.add_argument("--album-id", default="", help="仅处理指定 album_id")
    ap.add_argument("--limit", type=int, default=0, help="最多扫描行数（0=不限制）")
    ap.add_argument("--apply", action="store_true", help="写回数据库（默认仅预览）")
    ap.add_argument(
        "--force",
        action="store_true",
        help="即使 price_krw 与换算结果相同也写库（刷新 updated_at）",
    )
    ap.add_argument(
        "--clear-no-cny",
        action="store_true",
        help="无有效 price_cny 时清空 price_krw",
    )
    ap.add_argument(
        "--rate",
        type=float,
        default=0.0,
        help="手动指定 1 CNY 兑 KRW（>0 时覆盖配置与实时汇率）",
    )
    args = ap.parse_args(argv)

    if args.rate and args.rate > 0:
        rate = float(args.rate)
        fx_src = "cli"
    else:
        rate, fx_src = resolve_krw_per_cny()

    album_filter = str(args.album_id or "").strip() or None
    scan_limit = int(args.limit) if args.limit and args.limit > 0 else None

    conn = connect_sqlite()
    try:
        ensure_sqlite_schema(conn)
        rows = _scan_rows(conn, album_id=album_filter, limit=scan_limit)
        updates, stats = _plan_updates(
            rows,
            rate=rate,
            force=bool(args.force),
            clear_no_cny=bool(args.clear_no_cny),
        )

        print(f"db: {sqlite_db_path()}")
        print(f"汇率: 1 CNY = {rate} KRW（来源 {fx_src}）")
        print(
            f"扫描 {stats['total']} 行 | "
            f"将更新 {stats['to_update']} | "
            f"将清空 {stats['to_clear']} | "
            f"无人民币 {stats['no_cny']} | "
            f"无法换算 {stats['no_krw_computed']} | "
            f"已一致跳过 {stats['unchanged']}"
        )

        show_n = 30
        for u in updates[:show_n]:
            title = _safe_console(u.get("commodity_title") or "")
            old = _norm_krw(u.get("price_krw")) or "(空)"
            new = u.get("price_krw_new")
            new_s = _norm_krw(new) if new is not None else "(清空)"
            print(
                f"  id={u['id']} cny={u.get('price_cny')} "
                f"krw {old} → {new_s}  {title}"
            )
        if len(updates) > show_n:
            print(f"  … 另有 {len(updates) - show_n} 条")

        if args.apply:
            n = _apply_updates(conn, updates)
            print(f"已写库: {n} 条")
        elif updates:
            print("提示: 加 --apply 写回 price_krw")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
