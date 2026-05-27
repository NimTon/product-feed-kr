"""一键迁移本地 SQLite：规格拆列 + 清理旧列。

将历史 ``attr_map_json`` / ``supplement_*`` / ``llm_sizes_json`` 等迁移为：

- ``commodity_sizes_json`` / ``commodity_colors_json`` — 中文尺码/颜色（爬取 + 缺时 LLM 写入同列）
- ``sizes_ko_json`` / ``colors_ko_json`` — 韩文翻译

用法::

  python -m product_feed_kr.tools.migrate_llm_spec_db
  python -m product_feed_kr.tools.migrate_llm_spec_db --dry-run
  python -m product_feed_kr.tools.migrate_llm_spec_db --no-backup
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from product_feed_kr.common.pf_time import now_cst8_iso
from product_feed_kr.db.store_sqlite import connect_sqlite, ensure_sqlite_schema, sqlite_db_path

_LEGACY_COLS = frozenset(
    {
        "attr_map_json",
        "attr_map_ko_json",
        "llm_sizes_json",
        "llm_colors_json",
        "llm_sizes_ko_json",
        "llm_colors_ko_json",
        "supplement_sizes_json",
        "supplement_colors_json",
        "detail_response_json",
        "popups_response_json",
        "listing_llm_json",
        "commodity_price_raw",
        "llm_cny_price",
        "fx_krw_per_cny",
        "product_desc_html",
    },
)
_TARGET_COLS = (
    "commodity_sizes_json",
    "commodity_colors_json",
    "sizes_ko_json",
    "colors_ko_json",
)


def _table_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(r[1]) for r in conn.execute("PRAGMA table_info(pf_store_item)")]


def _stats(conn: sqlite3.Connection) -> dict[str, int]:
    cols = set(_table_columns(conn))

    def _cnt_col(col: str) -> int:
        if col not in cols:
            return -1
        row = conn.execute(
            f"SELECT COUNT(*) FROM pf_store_item WHERE trim(COALESCE({col},'')) <> ''",
        ).fetchone()
        return int(row[0]) if row else 0

    return {
        "items": int(conn.execute("SELECT COUNT(*) FROM pf_store_item").fetchone()[0]),
        "zh_sizes": _cnt_col("commodity_sizes_json"),
        "zh_colors": _cnt_col("commodity_colors_json"),
        "sizes_ko": _cnt_col("sizes_ko_json"),
        "colors_ko": _cnt_col("colors_ko_json"),
    }


def _print_report(*, title: str, db_path: Path, cols: list[str], stats: dict[str, int]) -> None:
    legacy = [c for c in cols if c in _LEGACY_COLS]
    target = [c for c in _TARGET_COLS if c in cols]
    missing = [c for c in _TARGET_COLS if c not in cols]
    print(f"\n=== {title} ===")
    print(f"数据库: {db_path}")
    print(f"商品行数: {stats['items']}")
    def _fmt(n: int) -> str:
        return "—" if n < 0 else str(n)

    print(f"中文尺码/颜色: {_fmt(stats['zh_sizes'])} / {_fmt(stats['zh_colors'])}")
    print(f"韩文 sizes_ko/colors_ko: {_fmt(stats['sizes_ko'])} / {_fmt(stats['colors_ko'])}")
    if legacy:
        print(f"仍存在的旧列: {', '.join(legacy)}")
    else:
        print("旧列已清理: attr_map_json、listing_llm_json、detail_response_json 等")
    if missing:
        print(f"缺少目标列: {', '.join(missing)}")
    if target:
        print(f"目标列就绪: {', '.join(target)}")


def _backup_db(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = db_path.with_name(f"{db_path.stem}.bak-{stamp}{db_path.suffix}")
    shutil.copy2(db_path, dest)
    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    if wal.is_file():
        shutil.copy2(wal, Path(str(dest) + "-wal"))
    if shm.is_file():
        shutil.copy2(shm, Path(str(dest) + "-shm"))
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="一键迁移 product_feed.db 规格列结构")
    ap.add_argument("--dry-run", action="store_true", help="只检查，不改库")
    ap.add_argument("--no-backup", action="store_true", help="迁移前不备份 .db")
    args = ap.parse_args(argv)

    db_path = sqlite_db_path()
    print(f"product-feed-kr 数据库迁移")
    print(f"时间: {now_cst8_iso()}")

    if not db_path.is_file():
        print(f"\n数据库不存在: {db_path}")
        print("将首次运行 01 采集 时自动建表；也可先执行本脚本创建空库结构。")
        if args.dry_run:
            return 0
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect_sqlite()
        try:
            ensure_sqlite_schema(conn)
        finally:
            conn.close()
        print(f"已创建空库结构: {db_path}")
        return 0

    conn = connect_sqlite()
    try:
        cols_before = _table_columns(conn)
        stats_before = _stats(conn)
    finally:
        conn.close()

    _print_report(title="迁移前", db_path=db_path, cols=cols_before, stats=stats_before)

    legacy_before = [c for c in cols_before if c in _LEGACY_COLS]
    need_migrate = bool(legacy_before) or any(c not in cols_before for c in _TARGET_COLS)
    if not need_migrate and any(
        c in cols_before
        for c in (
            "detail_response_json",
            "popups_response_json",
            "listing_llm_json",
            "commodity_price_raw",
            "llm_cny_price",
            "fx_krw_per_cny",
            "product_desc_html",
            "supplement_sizes_json",
            "supplement_colors_json",
        )
    ):
        need_migrate = True
    if not need_migrate:
        print("\n已是新结构，无需迁移。")
        return 0

    if args.dry_run:
        print("\n[dry-run] 未修改数据库。去掉 --dry-run 执行迁移。")
        return 0

    if not args.no_backup:
        bak = _backup_db(db_path)
        print(f"\n已备份: {bak}")

    print("\n正在迁移（请确保 01/02/03 批处理已关闭，避免写锁冲突）…")
    conn = connect_sqlite()
    try:
        ensure_sqlite_schema(conn)
        conn.commit()
    finally:
        conn.close()

    conn = connect_sqlite()
    try:
        cols_after = _table_columns(conn)
        stats_after = _stats(conn)
    finally:
        conn.close()

    _print_report(title="迁移后", db_path=db_path, cols=cols_after, stats=stats_after)

    if any(c in cols_after for c in _LEGACY_COLS):
        print("\n警告: 仍有旧列未删除，请把完整输出发给维护者。", file=sys.stderr)
        return 2

    print("\n迁移成功。")
    if stats_after.get("zh_sizes", -1) == 0:
        print("提示: 中文尺码列为空，请重跑 01_采集微猫店铺.bat（popUpsInfoV2）填充 commodity_sizes_json。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
