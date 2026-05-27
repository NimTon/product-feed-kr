"""对 SQLite 店铺商品执行 LLM enrich 并写回（不登录 seven17、不上架）。

``OPENAI_PROFILES`` 总线程数 > 1（多 api_key 或单 key ``"threads": 1~3``）时同进程多线程：
主线程（dispatcher）读库分配任务，worker 线程调用 LLM；每完成一次 API 响应后释放 claim 并继续调度。

运行示例::

  python -m product_feed_kr.seven17.seven17_llm --include-uploaded
  python -m product_feed_kr.seven17.seven17_llm --once --limit 10

默认进程内循环直至 Ctrl+C；``--once`` 只跑一轮。轮次间隔见 ``LISTING_LLM_ROUND_DELAY_SEC``（默认 5 秒）。
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from product_feed_kr.common.pf_cli_loop import default_log_path, run_forever
from product_feed_kr.common.pf_log import configure_llm_logging, configure_pf_stderr, pf_kv
from product_feed_kr.common.pf_log import LLM_LOGGER_NAMES, SEVEN17_LLM_LOGGER_NAME
from product_feed_kr.common.process_singleton import EXIT_SINGLETON_CONFLICT, single_instance_lock
from product_feed_kr.common.seven17_config import getenv as _cfg_get
from product_feed_kr.common.seven17_config import (
    EXIT_RESTART_FRESH_DATA,
    reload_seven17_config,
    restart_after_n,
)
from product_feed_kr.wego.wecatalog_store_record import commodity_from_wecatalog_record

_log = logging.getLogger(SEVEN17_LLM_LOGGER_NAME)
_llm_logging_configured = False


def _llm_round_delay_sec() -> float:
    raw = (_cfg_get("LISTING_LLM_ROUND_DELAY_SEC") or "5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


def _configure_llm_stderr_logging() -> None:
    global _llm_logging_configured
    if _llm_logging_configured:
        return
    configure_pf_stderr(*LLM_LOGGER_NAMES)
    _llm_logging_configured = True


def _init_llm_logging(*, log_file: Any = None, verbose: bool = False) -> None:
    global _llm_logging_configured
    configure_llm_logging(log_file=log_file, verbose=verbose)
    _llm_logging_configured = True


def _stdout_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass


def _record_needs_llm_api(rec: dict[str, Any]) -> bool:
    from product_feed_kr.listing.listing_llm_enrich import listing_llm_needs_api

    return listing_llm_needs_api(rec)


def _load_llm_work_rows(
    conn: Any,
    album_id: str,
    *,
    include_uploaded: bool,
    pending_only: bool,
    limit: int | None,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], int, int]:
    from product_feed_kr.db.store_sqlite import sqlite_load_products_for_upload

    items = sqlite_load_products_for_upload(conn, album_id, skip_uploaded=not include_uploaded)
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    skipped_no_detail = 0
    for rec in items:
        if not isinstance(rec, dict):
            continue
        com = commodity_from_wecatalog_record(rec)
        if not isinstance(com, dict):
            skipped_no_detail += 1
            continue
        if pending_only and not _record_needs_llm_api(rec):
            continue
        rows.append((rec, com))
        if isinstance(limit, int) and limit > 0 and len(rows) >= limit:
            break
    return rows, len(items), skipped_no_detail


def _llm_row_key(rec: dict[str, Any]) -> tuple[str, int]:
    gid = str(rec.get("goods_id") or "")
    try:
        tag_id = int(rec.get("tag_id") or 0)
    except (TypeError, ValueError):
        tag_id = 0
    return (gid, tag_id)


def _claim_next_llm_work_item(
    conn: Any,
    album_id: str,
    *,
    include_uploaded: bool,
    claim_lock: threading.Lock,
    in_flight: set[tuple[str, int]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """按 ``id`` 正序取第一条待 LLM 且未占用的记录。"""
    from product_feed_kr.db.store_sqlite import sqlite_load_products_for_upload

    with claim_lock:
        items = sqlite_load_products_for_upload(conn, album_id, skip_uploaded=not include_uploaded)
        for rec in items:
            if not isinstance(rec, dict):
                continue
            key = _llm_row_key(rec)
            if key in in_flight:
                continue
            if not _record_needs_llm_api(rec):
                continue
            com = commodity_from_wecatalog_record(rec)
            if not isinstance(com, dict):
                continue
            in_flight.add(key)
            return rec, com
    return None


def _run_llm_multithread_claim_loop(
    album_id: str,
    *,
    include_uploaded: bool,
    limit: int | None,
    restart_after: int,
    active_profiles: list | None = None,
) -> tuple[int, bool]:
    from product_feed_kr.listing.listing_llm_enrich import (
        enrich_record_listing_llm,
        listing_llm_api_profiles,
        llm_token_usage_kv_pairs,
    )
    from product_feed_kr.db.store_sqlite import connect_sqlite, sqlite_update_llm_result

    profiles = active_profiles if active_profiles is not None else listing_llm_api_profiles()
    total_threads = sum(p.get("threads", 1) for p in profiles)
    if total_threads <= 1:
        raise ValueError(
            "multithread claim loop 需要总线程数 > 1（通过多 api_key 或 threads 配置）",
        )

    aid = album_id.strip()
    claim_lock = threading.Lock()
    in_flight: set[tuple[str, int]] = set()
    updated_lock = threading.Lock()
    updated_total = 0
    restart_fresh = False
    stop_event = threading.Event()
    work_q: queue.Queue[tuple[dict[str, Any], dict[str, Any]] | None] = queue.Queue(
        maxsize=max(2, total_threads * 2)
    )

    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.threads.start"),
                ("mode", "dispatcher_queue"),
                ("threads", total_threads),
                ("labels", ",".join(f"{p['label']}x{p.get('threads',1)}" for p in profiles)),
                (
                    "endpoints",
                    ",".join(
                        urlparse((p.get("base_url") or "").strip()).netloc or "default"
                        for p in profiles
                    ),
                ),
            ],
            zh="多线程 LLM：主线程分配任务，worker 调用 API",
        ),
    )

    max_consecutive_fail = 3
    fused_profiles: set[str] = set()
    fuse_lock = threading.Lock()

    def _worker(profile: Any) -> None:
        nonlocal updated_total, restart_fresh
        label = profile.get("label", "?")
        consecutive_fail = 0
        conn = connect_sqlite()
        try:
            while True:
                if stop_event.is_set() and work_q.empty():
                    break
                try:
                    item = work_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    work_q.task_done()
                    break
                rec, com = item
                key = _llm_row_key(rec)
                from product_feed_kr.listing.listing_llm_enrich import record_after_llm_attempt

                success = False
                try:
                    ok = enrich_record_listing_llm(
                        rec,
                        com,
                        api_profile=profile,
                        register_attempt=False,
                    )
                    success = ok
                    record_after_llm_attempt(
                        rec,
                        com,
                        ok=ok,
                        error=None if ok else "enrich_returned_false",
                    )
                except Exception as e:
                    record_after_llm_attempt(rec, com, ok=False, error=str(e))
                finally:
                    sqlite_update_llm_result(conn, aid, rec)
                    with claim_lock:
                        in_flight.discard(key)
                    work_q.task_done()

                if success:
                    consecutive_fail = 0
                    with updated_lock:
                        updated_total += 1
                        if restart_after > 0 and updated_total >= restart_after:
                            restart_fresh = True
                            stop_event.set()
                            _log.info(
                                "%s",
                                pf_kv(
                                    [
                                        ("event", "llm.restart_after"),
                                        ("rows_updated", updated_total),
                                        ("restart_after", restart_after),
                                        ("thread", label),
                                    ],
                                    zh="已达 LLM 写回条数阈值，结束各线程",
                                ),
                            )
                else:
                    consecutive_fail += 1
                    if consecutive_fail >= max_consecutive_fail:
                        with fuse_lock:
                            fused_profiles.add(label)
                        _log.warning(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "llm.fuse"),
                                    ("label", label),
                                    ("consecutive_fail", consecutive_fail),
                                ],
                                zh=f"连续失败 {consecutive_fail} 次，熔断线程：{label}（可能 API 过期/余额不足）",
                            ),
                        )
                        break
        finally:
            conn.close()

    def _dispatch() -> None:
        """仅调度线程读取 SQLite 并分配任务，worker 不直接读库。"""
        conn = connect_sqlite()
        try:
            while not stop_event.is_set():
                with updated_lock:
                    if isinstance(limit, int) and limit > 0 and updated_total >= limit:
                        stop_event.set()
                        break
                item = _claim_next_llm_work_item(
                    conn,
                    aid,
                    include_uploaded=include_uploaded,
                    claim_lock=claim_lock,
                    in_flight=in_flight,
                )
                if item is None:
                    if in_flight:
                        continue
                    break
                while not stop_event.is_set():
                    try:
                        work_q.put(item, timeout=0.5)
                        break
                    except queue.Full:
                        continue
        finally:
            conn.close()
            for _ in range(total_threads):
                while True:
                    try:
                        work_q.put(None, timeout=0.5)
                        break
                    except queue.Full:
                        continue

    threads: list[threading.Thread] = []
    for profile in profiles:
        n = max(1, profile.get("threads", 1))
        for ti in range(n):
            suffix = f"-{ti}" if n > 1 else ""
            threads.append(
                threading.Thread(
                    target=_worker,
                    args=(profile,),
                    name=f"llm-{profile['label']}{suffix}",
                    daemon=True,
                )
            )
    dispatcher = threading.Thread(target=_dispatch, name="llm-dispatcher", daemon=True)
    dispatcher.start()
    for t in threads:
        t.start()
    dispatcher.join()
    for t in threads:
        t.join()

    if fused_profiles:
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "llm.fuse.summary"),
                    ("fused", len(fused_profiles)),
                    ("total_threads", len(threads)),
                    ("fused_labels", ",".join(sorted(fused_profiles))),
                ],
                zh=f"本轮有 {len(fused_profiles)} 个线程被熔断：{','.join(sorted(fused_profiles))}",
            ),
        )
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.threads.done"),
                ("rows_updated", updated_total),
                ("restart_fresh", restart_fresh),
                ("fused", len(fused_profiles)),
                *llm_token_usage_kv_pairs(),
            ],
            zh="多线程 LLM 本 run 结束",
        ),
    )
    return updated_total, restart_fresh


def enrich_llm_for_sqlite_records(
    album_id: str,
    *,
    limit: int | None = None,
    include_uploaded: bool = False,
) -> dict[str, Any]:
    """对 SQLite 现有记录执行 LLM enrich 并写回。"""
    from product_feed_kr.listing.listing_llm_enrich import (
        enrich_record_listing_llm,
        listing_llm_api_profiles,
        listing_llm_color_vision_enabled,
        listing_llm_enabled,
        llm_token_usage_kv_pairs,
        llm_token_usage_run_snapshot,
        probe_all_profiles,
        record_after_llm_attempt,
        reset_llm_token_usage_run,
    )

    reset_llm_token_usage_run()

    aid = album_id.strip()
    if not aid:
        raise ValueError("album_id 不能为空")
    _configure_llm_stderr_logging()

    if not listing_llm_enabled():
        return {
            "ok": False,
            "error": "LLM 未启用：请配置 OPENAI_PROFILES（并确保 OPENAI_ENRICH_LISTING=true）",
        }

    from product_feed_kr.db.store_sqlite import (
        connect_sqlite,
        ensure_sqlite_schema,
        sqlite_update_llm_result,
    )

    conn = connect_sqlite()
    try:
        ensure_sqlite_schema(conn)
        restart_after_llm = restart_after_n("LISTING_LLM_RESTART_AFTER_ITEMS", 1000)
        api_profiles = probe_all_profiles(
            listing_llm_api_profiles(),
            test_vision=listing_llm_color_vision_enabled(),
        )
        if not api_profiles:
            return {
                "ok": False,
                "error": "所有 API 探测均未通过，本轮跳过 LLM 处理",
                "all_probes_failed": True,
            }
        updated_total = 0
        restart_fresh = False
        rows_total = 0
        rows_eligible = 0
        skipped_no_detail = 0

        total_threads = sum(p.get("threads", 1) for p in api_profiles)
        if total_threads > 1:
            _, rows_total, skipped_no_detail = _load_llm_work_rows(
                conn,
                aid,
                include_uploaded=include_uploaded,
                pending_only=False,
                limit=None,
            )
            pending_rows, _, _ = _load_llm_work_rows(
                conn,
                aid,
                include_uploaded=include_uploaded,
                pending_only=True,
                limit=None,
            )
            rows_eligible = len(pending_rows)
            if not pending_rows:
                _log.info(
                    "%s",
                    pf_kv([("event", "llm.empty")], zh="无待 LLM 处理的记录"),
                )
            else:
                updated_total, restart_fresh = _run_llm_multithread_claim_loop(
                    aid,
                    include_uploaded=include_uploaded,
                    limit=limit,
                    restart_after=restart_after_llm,
                    active_profiles=api_profiles,
                )
        else:
            pending_rows, rows_total, skipped_no_detail = _load_llm_work_rows(
                conn,
                aid,
                include_uploaded=include_uploaded,
                pending_only=True,
                limit=limit if isinstance(limit, int) and limit > 0 else None,
            )
            rows_eligible = len(pending_rows)
            if not pending_rows:
                _log.info(
                    "%s",
                    pf_kv([("event", "llm.empty")], zh="无待 LLM 处理的记录"),
                )
            else:
                profile = api_profiles[0] if api_profiles else None
                _log.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "llm.run.start"),
                            ("pending", rows_eligible),
                            ("profile", profile.get("label") if profile else ""),
                        ],
                        zh="逐条 LLM 处理",
                    ),
                )
                for rec, com in pending_rows:
                    try:
                        ok = enrich_record_listing_llm(
                            rec,
                            com,
                            api_profile=profile,
                            register_attempt=False,
                        )
                    except Exception as e:
                        record_after_llm_attempt(rec, com, ok=False, error=str(e))
                        sqlite_update_llm_result(conn, aid, rec)
                        continue
                    record_after_llm_attempt(
                        rec,
                        com,
                        ok=ok,
                        error=None if ok else "enrich_returned_false",
                    )
                    sqlite_update_llm_result(conn, aid, rec)
                    ll = rec.get("listing_llm")
                    if (
                        isinstance(ll, dict)
                        and str(ll.get("source") or "") != "llm_skipped"
                        and rec.get("llm_processed_at")
                        and ok
                    ):
                        updated_total += 1
                    if restart_after_llm > 0 and updated_total >= restart_after_llm:
                        restart_fresh = True
                        _log.info(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "llm.restart_after"),
                                    ("rows_updated", updated_total),
                                    ("restart_after", restart_after_llm),
                                    ("exit", EXIT_RESTART_FRESH_DATA),
                                ],
                                zh="已达配置的 LLM 写回条数阈值，结束本进程以便外层重跑",
                            ),
                        )
                        break

        usage = llm_token_usage_run_snapshot()
        if usage.get("requests"):
            _log.info(
                "%s",
                pf_kv(
                    [("event", "llm.usage.run"), *llm_token_usage_kv_pairs()],
                    zh="本轮 LLM API 累计 token 用量",
                ),
            )

        return {
            "ok": True,
            "album_id": aid,
            "rows_total": rows_total,
            "rows_eligible": rows_eligible,
            "rows_updated": updated_total,
            "rows_skipped_no_detail": skipped_no_detail,
            "restart_fresh": restart_fresh,
            "token_usage": usage,
        }
    finally:
        conn.close()


_EXIT_ALL_PROBES_FAILED = 3


def _run_once(args: argparse.Namespace) -> int:
    aid = (args.album_id or _cfg_get("WECATALOG_ALBUM_ID") or "").strip()
    try:
        out = enrich_llm_for_sqlite_records(
            aid,
            limit=args.limit,
            include_uploaded=args.include_uploaded,
        )
        print(json.dumps(out, ensure_ascii=False))
        if out.get("restart_fresh"):
            reload_seven17_config()
            return EXIT_RESTART_FRESH_DATA
        if out.get("all_probes_failed"):
            return _EXIT_ALL_PROBES_FAILED
        return 0 if out.get("ok") else 2
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite 店铺商品 → LLM enrich 写回")
    parser.add_argument(
        "--album-id",
        default=None,
        metavar="ID",
        help="微猫相册 albumId；省略时使用 seven17.json / 环境变量 WECATALOG_ALBUM_ID",
    )
    parser.add_argument(
        "--include-uploaded",
        action="store_true",
        help="仍处理 seven17_uploaded_at 非空（已上传平台）的记录",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="本 run 最多成功写回多少条 LLM 结果（默认不限制）",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="日志文件（UTF-8）；常驻模式下未指定则 data/logs/seven17_llm_enrich_{时间}.log",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只执行一轮后退出（默认循环直至 Ctrl+C）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 级别")
    args = parser.parse_args()

    _stdout_utf8()
    repeat = not args.once
    log_file = args.log_file
    if repeat and log_file is None:
        log_file = default_log_path("seven17_llm_enrich")
    _init_llm_logging(log_file=log_file, verbose=args.verbose)

    aid = (args.album_id or _cfg_get("WECATALOG_ALBUM_ID") or "").strip()
    if not aid:
        parser.error(
            "缺少相册 ID：请传入 --album-id，或在 config/seven17.json / 环境变量中设置 WECATALOG_ALBUM_ID",
        )

    lock_name = "seven17_llm_enrich"
    try:
        with single_instance_lock(lock_name):
            if repeat:
                return run_forever(
                    lambda: _run_once(args),
                    task_label=lock_name,
                    logger=_log,
                    on_restart_fresh=reload_seven17_config,
                    round_delay_sec=_llm_round_delay_sec(),
                    fatal_codes={_EXIT_ALL_PROBES_FAILED: 60},
                )
            return _run_once(args)
    except SystemExit as e:
        if e.code == EXIT_SINGLETON_CONFLICT:
            return EXIT_SINGLETON_CONFLICT
        raise


if __name__ == "__main__":
    raise SystemExit(main())
