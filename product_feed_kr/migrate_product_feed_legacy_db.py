"""将历史 product_feed 数据库迁移为当前 schema（扁平行 + 规格拆列）。

默认从 ``data/product_feed 原始.db`` 复制到 ``data/product_feed.db`` 后执行
``store_sqlite.ensure_sqlite_schema_at``（与运行时 01/02/03 共用同一套迁移逻辑）。

用法::

  python -m product_feed_kr.migrate_product_feed_legacy_db
  python -m product_feed_kr.migrate_product_feed_legacy_db --dry-run
  python -m product_feed_kr.migrate_product_feed_legacy_db --in-place
  python -m product_feed_kr.migrate_product_feed_legacy_db --source data/product_feed\\ 原始.db --output data/product_feed_new.db
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import stat
import sys
from datetime import datetime
from pathlib import Path

from product_feed_kr.migrate_llm_spec_db import _LEGACY_COLS, _TARGET_COLS
from product_feed_kr.pf_time import now_cst8_iso
from product_feed_kr.store_sqlite import (
    _PF_STORE_ITEM_COLS,
    connect_sqlite_path,
    ensure_sqlite_schema_at,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SOURCE = _PROJECT_ROOT / "data" / "product_feed 原始.db"
_DEFAULT_OUTPUT = _PROJECT_ROOT / "data" / "product_feed.db"


def _resolve_db_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (_PROJECT_ROOT / p).resolve()


def _find_default_source() -> Path | None:
    if _DEFAULT_SOURCE.is_file():
        return _DEFAULT_SOURCE
    data = _PROJECT_ROOT / "data"
    if not data.is_dir():
        return None
    for candidate in sorted(data.glob("product_feed*.db")):
        name = candidate.name
        if "原始" in name or "legacy" in name.lower():
            return candidate.resolve()
    return None


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

    items = int(conn.execute("SELECT COUNT(*) FROM pf_store_item").fetchone()[0])
    stores = int(conn.execute("SELECT COUNT(*) FROM pf_store_info").fetchone()[0])
    return {
        "stores": stores,
        "items": items,
        "zh_sizes": _cnt_col("commodity_sizes_json"),
        "zh_colors": _cnt_col("commodity_colors_json"),
        "sizes_ko": _cnt_col("sizes_ko_json"),
        "colors_ko": _cnt_col("colors_ko_json"),
        "price_cny": _cnt_col("price_cny"),
        "llm_name_ko": _cnt_col("llm_name_ko"),
    }


def _print_report(*, title: str, db_path: Path, cols: list[str], stats: dict[str, int]) -> None:
    legacy = [c for c in cols if c in _LEGACY_COLS]
    target = [c for c in _TARGET_COLS if c in cols]
    missing = [c for c in _TARGET_COLS if c not in cols]
    schema_ok = cols == list(_PF_STORE_ITEM_COLS)
    print(f"\n=== {title} ===")
    print(f"数据库: {db_path}")
    print(f"店铺 / 商品: {stats['stores']} / {stats['items']}")

    def _fmt(n: int) -> str:
        return "—" if n < 0 else str(n)

    print(f"price_cny / llm_name_ko: {_fmt(stats['price_cny'])} / {_fmt(stats['llm_name_ko'])}")
    print(f"中文尺码/颜色: {_fmt(stats['zh_sizes'])} / {_fmt(stats['zh_colors'])}")
    print(f"韩文 sizes_ko/colors_ko: {_fmt(stats['sizes_ko'])} / {_fmt(stats['colors_ko'])}")
    if schema_ok:
        print("表结构: 与当前 schema 一致")
    else:
        print(f"表结构: 列数 {len(cols)}（目标 {len(_PF_STORE_ITEM_COLS)}）")
    if legacy:
        print(f"仍存在的旧列: {', '.join(legacy)}")
    if missing:
        print(f"缺少目标列: {', '.join(missing)}")


def _needs_migrate(cols: list[str]) -> bool:
    if any(c in cols for c in _LEGACY_COLS):
        return True
    if cols != list(_PF_STORE_ITEM_COLS):
        return True
    return any(c not in cols for c in _TARGET_COLS)


def _ensure_writable(db_path: Path) -> None:
    """复制自只读源库后，解除 Windows 只读属性以便 SQLite 写入。"""
    mode = db_path.stat().st_mode
    db_path.chmod(mode | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    if os.name == "nt" and not os.access(db_path, os.W_OK):
        os.chmod(db_path, stat.S_IWRITE)


def _backup_db(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = db_path.with_name(f"{db_path.stem}.bak-{stamp}{db_path.suffix}")
    shutil.copy2(db_path, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.is_file():
            shutil.copy2(side, Path(str(dest) + suffix))
    return dest


def _prepare_target(
    *,
    source: Path,
    output: Path,
    in_place: bool,
    force: bool,
) -> Path:
    if in_place:
        return source
    if output.resolve() == source.resolve():
        raise SystemExit("输出路径不能与源库相同；请改用 --in-place 或指定其它 --output。")
    if output.is_file():
        if not force:
            raise SystemExit(
                f"输出已存在: {output}\n"
                "加 --force 覆盖，或换 --output；也可用 --dry-run 仅检查源库。",
            )
        for path in (output, Path(str(output) + "-wal"), Path(str(output) + "-shm")):
            if path.is_file():
                _ensure_writable(path)
                path.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)
    _ensure_writable(output)
    for suffix in ("-wal", "-shm"):
        side = Path(str(source) + suffix)
        if side.is_file():
            shutil.copy2(side, Path(str(output) + suffix))
            _ensure_writable(Path(str(output) + suffix))
        out_side = Path(str(output) + suffix)
        if out_side.is_file():
            out_side.unlink()
    return output


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="将 product_feed 原始.db 迁移为当前 SQLite 结构",
    )
    ap.add_argument(
        "--source",
        default="",
        help=f"源库路径（默认 data/product_feed 原始.db 或 data 下含「原始」的 db）",
    )
    ap.add_argument(
        "--output",
        default=str(_DEFAULT_OUTPUT.relative_to(_PROJECT_ROOT)),
        help=f"输出库路径（默认 {_DEFAULT_OUTPUT.relative_to(_PROJECT_ROOT)}）",
    )
    ap.add_argument(
        "--in-place",
        action="store_true",
        help="直接迁移源库，不复制到新文件",
    )
    ap.add_argument("--dry-run", action="store_true", help="只检查，不改库")
    ap.add_argument("--no-backup", action="store_true", help="迁移前不备份目标库")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的 --output 文件")
    args = ap.parse_args(argv)

    source = _resolve_db_path(args.source) if args.source.strip() else _find_default_source()
    if source is None or not source.is_file():
        print("未找到源数据库。", file=sys.stderr)
        print(f"请指定 --source，或将备份放在: {_DEFAULT_SOURCE}", file=sys.stderr)
        return 1

    output = source if args.in_place else _resolve_db_path(args.output)

    print("product-feed-kr 历史库 → 新结构迁移")
    print(f"时间: {now_cst8_iso()}")
    print(f"源库: {source}")
    if args.in_place:
        print("模式: 原地迁移（--in-place）")
    else:
        print(f"输出: {output}")

    conn = connect_sqlite_path(source)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            )
        }
        if "pf_store_item" not in tables:
            print("源库缺少 pf_store_item 表，不是 product_feed 数据库。", file=sys.stderr)
            return 1
        cols_before = _table_columns(conn)
        stats_before = _stats(conn)
    finally:
        conn.close()

    _print_report(title="迁移前（源库）", db_path=source, cols=cols_before, stats=stats_before)

    if not _needs_migrate(cols_before):
        print("\n源库已是新结构。")
        if not args.in_place and output.resolve() != source.resolve():
            print("未复制到输出路径（源库无需迁移）。")
        return 0

    if args.dry_run:
        print("\n[dry-run] 未修改任何文件。去掉 --dry-run 执行迁移。")
        return 0

    work_path = source
    if not args.in_place:
        print("\n正在复制源库到输出路径…")
        work_path = _prepare_target(
            source=source,
            output=output,
            in_place=False,
            force=args.force,
        )
        print(f"已复制: {work_path}")
    elif not args.no_backup:
        bak = _backup_db(source)
        print(f"\n已备份源库: {bak}")

    _ensure_writable(work_path)

    if not args.in_place and not args.no_backup and work_path.is_file():
        bak = _backup_db(work_path)
        print(f"已备份输出库: {bak}")

    print("\n正在迁移（请关闭 01/02/03 批处理，避免写锁冲突）…")
    conn = connect_sqlite_path(work_path)
    try:
        ensure_sqlite_schema_at(conn, work_path)
    finally:
        conn.close()

    conn = connect_sqlite_path(work_path)
    try:
        cols_after = _table_columns(conn)
        stats_after = _stats(conn)
    finally:
        conn.close()

    _print_report(title="迁移后", db_path=work_path, cols=cols_after, stats=stats_after)

    if any(c in cols_after for c in _LEGACY_COLS):
        print("\n警告: 仍有旧列未删除。", file=sys.stderr)
        return 2
    if cols_after != list(_PF_STORE_ITEM_COLS):
        print("\n警告: 列顺序/集合与当前 schema 不一致。", file=sys.stderr)
        return 2

    print("\n迁移成功。")
    if stats_after["items"] != stats_before["items"]:
        print(
            f"警告: 商品行数变化 {stats_before['items']} → {stats_after['items']}",
            file=sys.stderr,
        )
    if stats_after.get("zh_sizes", -1) == 0:
        print(
            "提示: 中文尺码列为空时，可重跑 01_采集微猫店铺.bat 填充 commodity_sizes_json。",
        )
    if not args.in_place:
        print(f"\n可将 PRODUCT_FEED_SQLITE 指向: {work_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
