"""微猫 wecatalog 店铺：``commodity/tags`` + 按**抓取白名单** ``album/personal/all``（POST ``tagList=[tagId]``）列表 → SQLite。

仅爬取 ``data/wecatalog_scrape_whitelist.json`` 中勾选的标签（05 商品库「分类白名单」维护，**与分类配对无关**）。
白名单顺序即抓取优先级；单进程按标签顺序逐个抓完再换下一类（``01_采集微猫店铺.bat``）。
多进程分片见 ``01_采集微猫店铺_并行.bat`` + ``--worker-index`` / ``--worker-count``（读取白名单 ``max_parallel``）。
每个标签独立翻页，断点按 ``tag_id`` 保存在 ``stats.tag_progress``。

列表项缺 title/图 → ``pf_scrape_skip``（``list_incomplete``）；无价且分类不在白名单 → ``list_no_price``；库内已有 ``goods_id`` 跳过。
列表项已含 title / optimaPrice / formats / colors / imgsSrc，**不**请求 ``popUpsInfoV2``。
``--detail-delay`` 节流作用于 **相邻两次微猫 API**（tags、各标签列表翻页）。

示例::

  python -m product_feed_kr.wecatalog.wecatalog_scrape_store \\
    --store-url \"https://www.wecatalog.cn/weshop/store/{albumId}\" \\
    --detail-delay 5

可选配置 ``PRODUCT_FEED_SQLITE``：SQLite 文件路径（默认 ``data/product_feed.db``）。

``WECATALOG_SCRAPE_RESTART_AFTER_ITEMS``（默认 1000）：本 run 新增写入 SQLite 达 N 条后退出码 **75**。

``WECATALOG_DETAIL_DELAY``（默认 ``5``）：未传 ``--detail-delay`` 时的节流默认值。

日志：``event=`` + ``scrape:``；``--log-file`` UTF-8；``-v`` DEBUG；``--headed`` 有界面浏览器。
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from product_feed_kr.common.pf_time import now_cst8_iso
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from product_feed_kr.wecatalog.wecatalog_fetch_tags import (
    TAGS_PATH,
    _launch_browser,
    build_group_tree,
    tags_api_url,
)
from product_feed_kr.common.seven17_config import (
    EXIT_RESTART_FRESH_DATA,
    bool_env,
    getenv,
    reload_seven17_config,
    restart_after_n,
)
from product_feed_kr.wecatalog.wecatalog_scrape_fields import (
    attach_scrape_fields_to_record,
    fields_from_list_item,
    list_item_scrape_ready,
    scrape_no_price_skip_needed,
)
from product_feed_kr.pf_browser.category_whitelists import (
    build_scrape_tag_targets_from_whitelist,
    load_scrape_max_parallel,
    scrape_whitelist_empty_diagnostic,
)
from product_feed_kr.wecatalog.wecatalog_tag_mapping import ScrapeTagTarget, lookup_group_tag_by_id
from product_feed_kr.common.pf_cli_loop import run_forever
from product_feed_kr.common.pf_log import configure_scrape_logging, pf_kv, pf_store_row_id_kv
from product_feed_kr.common.process_singleton import EXIT_SINGLETON_CONFLICT, single_instance_lock

# 与 `configure_scrape_logging` 默认 logger_name 一致；`-m` 运行时 __name__ 为 __main__，勿用 getLogger(__name__)。
logger = logging.getLogger("product_feed_kr.wecatalog.wecatalog_scrape_store")


def parse_detail_delay_range(raw: str) -> tuple[float, float]:
    """解析节流间隔：``5`` → (5,5)；``3,8`` / ``3:8`` / ``3~8`` → (3,8) 闭区间随机；JSON 数组 ``[3, 8]``（如 seven17.json 里写数组经 ``getenv`` 转成字符串后）同上。非负；若 A>B 则自动交换。"""
    t = str(raw).strip()
    if not t:
        return (5.0, 5.0)
    if t.startswith("["):
        try:
            arr = json.loads(t)
        except json.JSONDecodeError:
            arr = None
        if isinstance(arr, list) and arr:
            lo = max(0.0, float(arr[0]))
            if len(arr) >= 2:
                hi = max(0.0, float(arr[1]))
                if lo > hi:
                    lo, hi = hi, lo
                return (lo, hi)
            return (lo, lo)
    for sep in (",", ":", "~"):
        if sep in t:
            a, b = t.split(sep, 1)
            lo = max(0.0, float(a.strip()))
            hi = max(0.0, float(b.strip()))
            if lo > hi:
                lo, hi = hi, lo
            return (lo, hi)
    v = max(0.0, float(t))
    return (v, v)


class InterRequestGap:
    """相邻两次微猫 API（浏览器内 fetch/evaluate）之间休眠；首次 before() 不休眠。delay 为 [lo,hi] 闭区间时每次随机 uniform。"""

    __slots__ = ("_lo", "_hi", "_n")

    def __init__(self, delay_lo: float, delay_hi: float) -> None:
        self._lo = max(0.0, float(delay_lo))
        self._hi = max(self._lo, float(delay_hi))
        self._n = 0

    def before(self, label: str) -> None:
        if self._n > 0:
            if self._hi <= 0.0:
                pass
            else:
                sec = random.uniform(self._lo, self._hi) if self._hi > self._lo else self._lo
                if self._n <= 3 or self._n % 50 == 0:
                    logger.info(
                        "%s",
                        pf_kv(
                            [
                                ("event", "scrape.throttle"),
                                ("delay_sec", round(sec, 3)),
                                ("count", self._n),
                                ("next", label),
                            ],
                            zh="请求节流（采样）",
                        ),
                    )
                time.sleep(sec)
        self._n += 1


FETCH_TAG_LIST_JS = """
async ({ albumId, tagId, pageTimestamp }) => {
  let url = `https://www.wecatalog.cn/album/personal/all?albumId=${encodeURIComponent(albumId)}`
    + `&searchValue=&searchImg=&startDate=&endDate=&requestDataType=&tagUnion=false`;
  if (pageTimestamp != null) {
    url += `&slipType=1&timestamp=${pageTimestamp}`;
  } else {
    url += `&slipType=0&timestamp=`;
  }
  const r = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "Accept": "application/json",
    },
    body: `tagList=[${tagId}]`,
  });
  if (!r.ok) {
    return { errcode: -1, errmsg: `HTTP ${r.status} ${r.statusText} for ${url}` };
  }
  return await r.json().catch(() => ({ errcode: -2, errmsg: "响应非 JSON: " + url }));
}
"""

FETCH_TAGS_JS = """
async (apiUrl) => {
  const r = await fetch(apiUrl, { credentials: "include" });
  if (!r.ok) {
    return { errcode: -1, errmsg: `HTTP ${r.status} ${r.statusText} for ${apiUrl}` };
  }
  return await r.json().catch(() => ({ errcode: -2, errmsg: "响应非 JSON: " + apiUrl }));
}
"""


def parse_album_id(store_url: str) -> str:
    u = store_url.strip().rstrip("/")
    if "/store/" in u:
        return u.split("/store/", 1)[-1].split("?")[0].split("#")[0]
    if "/weshop/" in u:
        seg = u.split("/weshop/", 1)[-1].split("/")[0]
        return seg.split("?")[0].split("#")[0]
    m = re.search(r"/([-_A-Za-z0-9]{20,})", u)
    if m:
        return m.group(1)
    raise ValueError(f"无法从 URL 解析 albumId: {store_url!r}")


def goods_page_url(album_id: str, goods_id: str) -> str:
    return f"https://www.wecatalog.cn/weshop/goods/{album_id}/{goods_id}"


def normalize_max_list_pages(raw: int | None) -> int:
    """``<= 0``（含 ``-1``）不限页数；正数为最大翻页次数。"""
    if raw is None or raw <= 0:
        return -1
    return int(raw)


TAG_PROGRESS_KEY = "tag_progress"
TAG_PAGE_NUM_KEY = "page_num"
TAG_PAGE_NEXT_TS_KEY = "page_next_ts"
TAG_DONE_KEY = "done"


def _tag_progress_map(stats: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(stats, dict):
        return {}
    raw = stats.get(TAG_PROGRESS_KEY)
    return raw if isinstance(raw, dict) else {}


def tag_progress_entry(stats: dict[str, Any] | None, tag_id: int) -> dict[str, Any]:
    entry = _tag_progress_map(stats).get(str(tag_id))
    return entry if isinstance(entry, dict) else {}


def tag_progress_from_stats(stats: dict[str, Any] | None, tag_id: int) -> tuple[int, int | None]:
    entry = tag_progress_entry(stats, tag_id)
    page_num = 0
    raw_num = entry.get(TAG_PAGE_NUM_KEY)
    if raw_num is not None:
        try:
            page_num = max(0, int(raw_num))
        except (TypeError, ValueError):
            page_num = 0
    raw_ts = entry.get(TAG_PAGE_NEXT_TS_KEY)
    if raw_ts is None:
        return page_num, None
    try:
        return page_num, int(raw_ts)
    except (TypeError, ValueError):
        return page_num, None


def tag_is_done(stats: dict[str, Any] | None, tag_id: int) -> bool:
    return bool(tag_progress_entry(stats, tag_id).get(TAG_DONE_KEY))


def update_tag_progress_in_stats(
    stats: dict[str, Any],
    *,
    tag_id: int,
    page_num: int,
    raw_page: dict[str, Any],
) -> None:
    tp = stats.setdefault(TAG_PROGRESS_KEY, {})
    entry: dict[str, Any] = dict(tp.get(str(tag_id)) or {})
    entry[TAG_PAGE_NUM_KEY] = int(page_num)
    result = raw_page.get("result") if isinstance(raw_page.get("result"), dict) else {}
    pag = result.get("pagination") if isinstance(result.get("pagination"), dict) else {}
    next_ts = pag.get("pageTimestamp")
    if pag.get("isLoadMore") and next_ts is not None:
        try:
            entry[TAG_PAGE_NEXT_TS_KEY] = int(next_ts)
        except (TypeError, ValueError):
            entry.pop(TAG_PAGE_NEXT_TS_KEY, None)
        entry.pop(TAG_DONE_KEY, None)
    else:
        entry[TAG_DONE_KEY] = True
        entry.pop(TAG_PAGE_NEXT_TS_KEY, None)
    tp[str(tag_id)] = entry


def clear_all_tag_progress(stats: dict[str, Any]) -> None:
    stats.pop(TAG_PROGRESS_KEY, None)


_CHECKPOINT_ADDITIVE_KEYS = (
    "tag_list_pages",
    "list_items_unique",
    "records_new",
    "skipped_existing",
    "skipped_blacklist",
    "list_ok",
    "list_incomplete",
    "list_no_price",
    "listed_at_backfilled",
)


def filter_targets_for_worker(
    targets: tuple[ScrapeTagTarget, ...] | list[ScrapeTagTarget],
    *,
    worker_index: int,
    worker_count: int,
) -> list[ScrapeTagTarget]:
    if worker_count <= 1:
        return list(targets)
    if worker_index < 0 or worker_index >= worker_count:
        raise ValueError(f"worker_index 须在 [0, {worker_count}) 内，收到 {worker_index}")
    return [t for i, t in enumerate(targets) if i % worker_count == worker_index]


def merge_checkpoint_stats(
    local_stats: dict[str, Any],
    *,
    remote_stats: dict[str, Any] | None,
    pending_records: int,
) -> dict[str, Any]:
    """多 worker 写 checkpoint 时合并 tag_progress 与累计计数，避免互相覆盖。"""
    remote = remote_stats if isinstance(remote_stats, dict) else {}
    out = dict(local_stats)
    merged_tp = {**_tag_progress_map(remote), **_tag_progress_map(local_stats)}
    out[TAG_PROGRESS_KEY] = merged_tp
    out["tags_total"] = max(int(remote.get("tags_total") or 0), int(local_stats.get("tags_total") or 0))
    out["tags_done"] = sum(
        1 for e in merged_tp.values() if isinstance(e, dict) and e.get(TAG_DONE_KEY)
    )
    for key in _CHECKPOINT_ADDITIVE_KEYS:
        out[key] = int(remote.get(key) or 0) + int(local_stats.get(key) or 0)
    if remote.get("records_prior") is not None:
        out["records_prior"] = remote.get("records_prior")
    out["records"] = int(remote.get("records") or 0) + pending_records
    if remote.get("max_parallel") is not None and out.get("max_parallel") is None:
        out["max_parallel"] = remote.get("max_parallel")
    return out


def iter_tag_list_pages(
    page,
    album_id: str,
    tag_id: int,
    *,
    max_pages: int,
    gap: InterRequestGap,
    resume_page_ts: int | None = None,
    resume_pages: int = 0,
):
    """逐页请求 ``album/personal/all``（POST ``tagList=[tagId]``，与 goods_list 页一致）；每页 yield (items, 原始响应, 页码 1-based)。"""
    seen: set[str] = set()
    ts: int | None = resume_page_ts
    pages = max(0, int(resume_pages))
    unlimited = max_pages <= 0
    if resume_page_ts is not None:
        logger.info(
            "%s",
            pf_kv(
                [
                    ("event", "scrape.tag_list.resume"),
                    ("tag_id", tag_id),
                    ("page_num", pages),
                    ("page_next_ts", resume_page_ts),
                ],
                zh=f"标签 {tag_id} 从翻页断点继续（已完成 {pages} 页）",
            ),
        )
    while True:
        if not unlimited and pages >= max_pages:
            logger.info(
                "%s",
                pf_kv(
                    [
                        ("event", "scrape.tag_list.page_limit"),
                        ("tag_id", tag_id),
                        ("page", pages),
                        ("max_pages", max_pages),
                    ],
                    zh=f"标签 {tag_id} 已达翻页上限 {max_pages} 页",
                ),
            )
            break
        gap.before(f"tagId={tag_id} 列表第 {pages + 1} 页")
        raw = page.evaluate(
            FETCH_TAG_LIST_JS,
            {"albumId": album_id, "tagId": tag_id, "pageTimestamp": ts},
        )
        pages += 1
        if not isinstance(raw, dict) or raw.get("errcode") not in (0, None):
            logger.warning(
                "%s",
                pf_kv(
                    [
                        ("event", "scrape.tag_list.bad_response"),
                        ("tag_id", tag_id),
                        ("page", pages),
                        ("errcode", raw.get("errcode") if isinstance(raw, dict) else None),
                    ],
                    zh=f"标签 {tag_id} 列表接口异常，停止翻页",
                ),
            )
            break
        result = raw.get("result")
        if not isinstance(result, dict):
            logger.warning(
                "%s",
                pf_kv(
                    [("event", "scrape.tag_list.no_result"), ("tag_id", tag_id), ("page", pages)],
                    zh=f"标签 {tag_id} 响应缺少 result",
                ),
            )
            break
        items = result.get("items")
        if not isinstance(items, list) or len(items) == 0:
            logger.info(
                "%s",
                pf_kv(
                    [("event", "scrape.tag_list.empty"), ("tag_id", tag_id), ("page", pages)],
                    zh=f"标签 {tag_id} 本页无商品，列表结束",
                ),
            )
            break

        page_items: list[dict[str, Any]] = []
        page_new = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            gid = (
                it.get("goods_id")
                or it.get("selfGoodsId")
                or it.get("parent_goods_id")
                or it.get("id")
            )
            if isinstance(gid, str) and gid and gid not in seen:
                seen.add(gid)
                page_items.append(it)
                page_new += 1

        pag = result.get("pagination") if isinstance(result.get("pagination"), dict) else {}
        logger.info(
            "%s",
            pf_kv(
                [
                    ("event", "scrape.tag_list.page"),
                    ("tag_id", tag_id),
                    ("page", pages),
                    ("max_pages", max_pages if not unlimited else -1),
                    ("items", len(items)),
                    ("page_new", page_new),
                    ("seen", len(seen)),
                    ("isLoadMore", pag.get("isLoadMore")),
                ],
                zh=f"标签 {tag_id} 已拉取第 {pages} 页",
            ),
        )

        yield page_items, raw, pages

        if not pag.get("isLoadMore"):
            logger.info(
                "%s",
                pf_kv(
                    [("event", "scrape.tag_list.no_more"), ("tag_id", tag_id)],
                    zh=f"标签 {tag_id} 无更多页",
                ),
            )
            break
        next_ts = pag.get("pageTimestamp")
        if next_ts is None:
            break
        try:
            ts = int(next_ts)
        except (TypeError, ValueError):
            break


def scrape_store(
    store_url: str,
    *,
    trans_lang: str = "zh",
    detail_delay_range: tuple[float, float] = (5.0, 5.0),
    max_list_pages: int = -1,
    skip_detail: bool = False,
    tag_resume: bool = True,
    tags_from_start: bool = False,
    checkpoint_every: int = 0,
    max_records: int = 0,
    headed: bool = False,
    worker_index: int = 0,
    worker_count: int = 1,
) -> dict[str, Any]:
    from product_feed_kr.db.store_sqlite import (
        connect_sqlite,
        ensure_sqlite_schema,
        scrape_detail_ready,
        sqlite_checkpoint,
        sqlite_load_existing_goods_ids,
        sqlite_load_goods_ids_missing_listed_at,
        sqlite_load_scrape_skip_goods_ids,
        sqlite_backfill_wecatalog_listed_at,
        sqlite_load_store_snapshot,
        sqlite_record_scrape_skip,
        sqlite_write_store_meta,
        sqlite_db_path,
    )

    album_id = parse_album_id(store_url)
    seed = store_url if store_url.startswith("http") else f"https://www.wecatalog.cn/weshop/store/{album_id}"

    delay_lo, delay_hi = (
        float(detail_delay_range[0]),
        float(detail_delay_range[1]),
    )
    if delay_lo > delay_hi:
        delay_lo, delay_hi = delay_hi, delay_lo
    delay_lo = max(0.0, delay_lo)
    delay_hi = max(delay_lo, delay_hi)

    if worker_count < 1:
        raise ValueError(f"worker_count 须 >= 1，收到 {worker_count}")
    if worker_count > 1 and (worker_index < 0 or worker_index >= worker_count):
        raise ValueError(f"worker_index 须在 [0, {worker_count}) 内，收到 {worker_index}")

    logger.info(
        "%s",
        pf_kv(
            [
                ("event", "scrape.start"),
                ("album_id", album_id),
                ("seed", seed),
                ("sqlite", str(sqlite_db_path())),
                ("throttle_delay_sec_range", [delay_lo, delay_hi]),
                ("worker_index", worker_index if worker_count > 1 else None),
                ("worker_count", worker_count if worker_count > 1 else None),
            ],
            zh="开始抓取微猫店铺：按抓取白名单拉列表",
        ),
    )

    stats = {
        "album_id": album_id,
        "tags_total": 0,
        "tags_done": 0,
        "tag_list_pages": 0,
        "list_items_unique": 0,
        "list_ok": 0,
        "list_incomplete": 0,
        "list_no_price": 0,
        "records": 0,
        "records_prior": 0,
        "records_new": 0,
        "skipped_existing": 0,
        "skipped_blacklist": 0,
        "listed_at_backfilled": 0,
        "restart_fresh": False,
        "throttle_delay_sec_range": [delay_lo, delay_hi],
    }
    restart_after_new = restart_after_n("WECATALOG_SCRAPE_RESTART_AFTER_ITEMS", 1000)

    conn_db = None
    records: list[dict[str, Any]] = []
    skip_ids_prior: set[str] = set()
    skip_ids_blacklist: set[str] = set()
    need_backfill_listed_at: set[str] = set()
    try:
        conn_db = connect_sqlite()
        ensure_sqlite_schema(conn_db)
        skip_ids_prior, rows_prior = sqlite_load_existing_goods_ids(conn_db, album_id)
        skip_ids_blacklist = sqlite_load_scrape_skip_goods_ids(conn_db, album_id)
        need_backfill_listed_at = sqlite_load_goods_ids_missing_listed_at(conn_db, album_id)
        logger.info(
            "%s",
            pf_kv(
                [
                    ("event", "scrape.sqlite_load"),
                    ("rows_prior", rows_prior),
                    ("skip_ids", len(skip_ids_prior)),
                    ("skip_blacklist", len(skip_ids_blacklist)),
                    ("missing_listed_at", len(need_backfill_listed_at)),
                    ("album_id", album_id),
                ],
                zh="已读库内 goods_id 与 pf_scrape_skip 跳过表（不载入整行）",
            ),
        )

        stats["records_prior"] = rows_prior

        if tag_resume and not tags_from_start:
            snap = sqlite_load_store_snapshot(conn_db, album_id)
            raw_stats = snap.get("stats") if isinstance(snap, dict) else None
            if isinstance(raw_stats, dict):
                stats[TAG_PROGRESS_KEY] = dict(_tag_progress_map(raw_stats))

        if tags_from_start:
            clear_all_tag_progress(stats)

        p = sync_playwright().start()
        browser = _launch_browser(p, headless=not headed)
        if headed:
            logger.info(
                "%s",
                pf_kv([("event", "scrape.browser"), ("headed", 1)], zh="使用有界面浏览器（非无头）"),
            )
        try:
            page = browser.new_page()
            page.goto(seed, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(2_000)

            gap = InterRequestGap(delay_lo, delay_hi)
            api_tags = tags_api_url(album_id=album_id, trans_lang=trans_lang)
            gap.before("commodity/tags 分类树")
            tags_raw = page.evaluate(FETCH_TAGS_JS, api_tags)
            if not isinstance(tags_raw, dict) or tags_raw.get("errcode") not in (0, None):
                raise RuntimeError(f"commodity/tags 失败: {tags_raw!r}"[:500])
            result = tags_raw.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("tags 响应缺少 result")
            groups = build_group_tree(result)
            all_targets = build_scrape_tag_targets_from_whitelist(groups)
            if not all_targets:
                raise RuntimeError(scrape_whitelist_empty_diagnostic(groups))
            max_parallel = load_scrape_max_parallel()
            targets = filter_targets_for_worker(
                all_targets,
                worker_index=worker_index,
                worker_count=worker_count,
            )
            stats["tags_total"] = len(all_targets)
            stats["tags_assigned"] = len(targets)
            stats["max_parallel"] = max_parallel
            if worker_count > 1:
                stats["worker_index"] = worker_index
                stats["worker_count"] = worker_count
            logger.info(
                "%s",
                pf_kv(
                    [
                        ("event", "scrape.tags_ok"),
                        ("groups", len(groups)),
                        ("targets", len(all_targets)),
                        ("assigned", len(targets)),
                        ("max_parallel", max_parallel),
                        ("worker_index", worker_index if worker_count > 1 else None),
                        ("worker_count", worker_count if worker_count > 1 else None),
                    ],
                    zh=(
                        f"分类树已拉取，白名单 {len(all_targets)} 个标签"
                        + (
                            f"；本 worker 负责 {len(targets)} 个"
                            if worker_count > 1
                            else ""
                        )
                    ),
                ),
            )

            if not targets:
                logger.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "scrape.worker_idle"),
                            ("worker_index", worker_index),
                            ("worker_count", worker_count),
                        ],
                        zh="本 worker 无分配标签，直接结束",
                    ),
                )

            logger.info(
                "%s",
                pf_kv(
                    [
                        ("event", "scrape.tag_list.begin"),
                        ("targets", len(targets)),
                        ("max_list_pages", max_list_pages),
                        ("tag_resume", tag_resume and not tags_from_start),
                    ],
                    zh="开始按标签遍历商品列表",
                ),
            )
            stop_flag = False

            def _write_checkpoint_locked(*, meta_only: bool = False) -> None:
                assert conn_db is not None
                stats_to_write = stats
                if worker_count > 1:
                    snap = sqlite_load_store_snapshot(conn_db, album_id)
                    remote_stats = snap.get("stats") if isinstance(snap, dict) else None
                    stats_to_write = merge_checkpoint_stats(
                        stats,
                        remote_stats=remote_stats if isinstance(remote_stats, dict) else None,
                        pending_records=len(records),
                    )
                meta_extra = {
                    "saved_at": now_cst8_iso(),
                    "stats": stats_to_write,
                }
                if meta_only:
                    sqlite_write_store_meta(
                        conn_db,
                        album_id,
                        store_url=seed,
                        trans_lang=trans_lang,
                        detail_delay_sec=delay_lo,
                        skip_detail=skip_detail,
                        meta_extra=meta_extra,
                    )
                    written = 0
                else:
                    written = sqlite_checkpoint(
                        conn_db,
                        album_id,
                        store_url=seed,
                        trans_lang=trans_lang,
                        detail_delay_sec=delay_lo,
                        skip_detail=skip_detail,
                        meta_extra=meta_extra,
                        records=records,
                    )
                ck_kv: list[tuple[str, Any]] = [
                    ("event", "scrape.checkpoint"),
                    ("pending_in_memory", len(records)),
                    ("written_with_detail", written),
                    ("album_id", album_id),
                ]
                ready = [r for r in records if scrape_detail_ready(r)]
                not_ready = len(records) - len(ready)
                if not_ready:
                    ck_kv.append(("not_ready_in_memory", not_ready))
                if ready:
                    ck_kv.extend(pf_store_row_id_kv(ready[-1], album_id=album_id))
                zh_ck = (
                    "写库：店铺元信息（尚无新商品）"
                    if meta_only
                    else (
                        "写库：店铺进度 + 本 run 新商品"
                        if written > 0 and not_ready == 0
                        else (
                            f"写库：内存 {len(records)} 条，"
                            f"其中 {written} 条已入库"
                            + (
                                f"（{not_ready} 条无 commodity_title，未写入 pf_store_item）"
                                if not_ready
                                else ""
                            )
                        )
                    )
                )
                logger.info("%s", pf_kv(ck_kv, zh=zh_ck))

            def write_checkpoint(*, meta_only: bool = False) -> None:
                _write_checkpoint_locked(meta_only=meta_only)

            # 进入详情循环前只写店铺元信息，不把旧商品行载入内存写回
            write_checkpoint(meta_only=True)
            logger.info(
                "%s",
                pf_kv(
                    [("event", "scrape.checkpoint.initial"), ("album_id", album_id)],
                    zh="进入详情循环前已写店铺元信息",
                ),
            )

            new_appended = 0
            log_every = 50
            krw_per_cny_page: float | None = None

            def append_product_row(
                it: dict[str, Any],
                gname: str,
                tname: str,
                tid_int: int,
                shop_path: tuple[str, ...] | None,
            ) -> bool:
                """写入一条分组×标签×商品；返回 False 表示已达 max_records 应停止整次抓取。"""
                return _append_product_row_locked(it, gname, tname, tid_int, shop_path)

            def _append_product_row_locked(
                it: dict[str, Any],
                gname: str,
                tname: str,
                tid_int: int,
                shop_path: tuple[str, ...] | None,
            ) -> bool:
                nonlocal new_appended
                gid = (
                    it.get("goods_id")
                    or it.get("selfGoodsId")
                    or it.get("parent_goods_id")
                    or ""
                )
                if not isinstance(gid, str) or not gid:
                    return True

                if gid in skip_ids_blacklist:
                    stats["skipped_blacklist"] += 1
                    if stats["skipped_blacklist"] in (1, 100, 500) or stats["skipped_blacklist"] % 2000 == 0:
                        logger.info(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.skip_blacklist"),
                                    ("total", stats["skipped_blacklist"]),
                                    ("goods_id", gid),
                                ],
                                zh="跳过 pf_scrape_skip 中已失效 goods_id",
                            ),
                        )
                    return True

                if gid in skip_ids_prior:
                    stats["skipped_existing"] += 1
                    if stats["skipped_existing"] in (1, 100, 500) or stats["skipped_existing"] % 2000 == 0:
                        logger.info(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.skip_existing"),
                                    ("total", stats["skipped_existing"]),
                                ],
                                zh="跳过库内已有 goods_id",
                            ),
                        )
                    return True

                if skip_detail:
                    return True

                scrape_fields = fields_from_list_item(it)

                if not list_item_scrape_ready(it, scrape_fields):
                    stats["list_incomplete"] += 1
                    skip_ids_prior.add(gid)
                    skip_ids_blacklist.add(gid)
                    g_url = goods_page_url(album_id, gid)
                    if conn_db is not None:
                        sqlite_record_scrape_skip(
                            conn_db,
                            album_id,
                            gid,
                            reason="list_incomplete",
                            goods_url=g_url,
                        )
                    if stats["list_incomplete"] in (1, 50, 200) or stats["list_incomplete"] % 1000 == 0:
                        logger.info(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.list.skip_incomplete"),
                                    ("total", stats["list_incomplete"]),
                                    ("goods_id", gid),
                                ],
                                zh="列表项缺 title/图，已记入 pf_scrape_skip",
                            ),
                        )
                    return True

                if scrape_no_price_skip_needed(
                    wecatalog_group=gname,
                    wecatalog_tag=tname,
                    scrape_fields=scrape_fields,
                ):
                    stats["list_no_price"] += 1
                    skip_ids_prior.add(gid)
                    skip_ids_blacklist.add(gid)
                    g_url = goods_page_url(album_id, gid)
                    if conn_db is not None:
                        sqlite_record_scrape_skip(
                            conn_db,
                            album_id,
                            gid,
                            reason="list_no_price",
                            goods_url=g_url,
                        )
                    if stats["list_no_price"] in (1, 50, 200) or stats["list_no_price"] % 1000 == 0:
                        logger.info(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.list.skip_no_price"),
                                    ("total", stats["list_no_price"]),
                                    ("goods_id", gid),
                                    ("group", gname),
                                    ("tag", tname),
                                ],
                                zh="列表项无价且分类不在白名单，已记入 pf_scrape_skip",
                            ),
                        )
                    return True

                stats["list_ok"] += 1

                rec = {
                    "wecatalog_group": gname,
                    "wecatalog_tag": tname,
                    "tag_id": tid_int,
                    "shop_category_path": list(shop_path) if shop_path is not None else None,
                    "goods_id": gid,
                    "goods_url": goods_page_url(album_id, gid),
                    "uploaded_to_platform": False,
                }
                attach_scrape_fields_to_record(
                    rec,
                    scrape_fields,
                    krw_per_cny=krw_per_cny_page,
                )
                records.append(rec)
                skip_ids_prior.add(gid)
                new_appended += 1
                stats["records"] = len(records)
                stats["records_new"] = new_appended

                if new_appended == 1 or new_appended % log_every == 0:
                    logger.info(
                        "%s",
                        pf_kv(
                            [
                                ("event", "scrape.append"),
                                ("n", new_appended),
                                ("group", gname),
                                ("tag", tname),
                                ("goods_id", gid),
                            ],
                            zh="新增一条「分组×标签×商品」到内存列表",
                        ),
                    )

                if checkpoint_every > 0 and new_appended > 0 and new_appended % checkpoint_every == 0:
                    write_checkpoint()

                if (
                    restart_after_new > 0
                    and new_appended > 0
                    and new_appended % restart_after_new == 0
                ):
                    write_checkpoint()
                    stats["restart_fresh"] = True
                    logger.info(
                        "%s",
                        pf_kv(
                            [
                                ("event", "scrape.restart_after"),
                                ("records_new", new_appended),
                                ("restart_after", restart_after_new),
                                ("exit", EXIT_RESTART_FRESH_DATA),
                            ],
                            zh="已达配置的新增条数阈值，结束本进程以便外层重跑刷新数据",
                        ),
                    )
                    return False

                if max_records > 0 and new_appended >= max_records:
                    logger.info(
                        "%s",
                        pf_kv(
                            [("event", "scrape.max_records"), ("max_records", max_records)],
                            zh="已达本 run 最大新增条数上限，停止抓取",
                        ),
                    )
                    return False
                return True

            def process_target(target: ScrapeTagTarget, tag_page) -> None:
                nonlocal stop_flag
                if stop_flag:
                    return
                if tag_resume and not tags_from_start and tag_is_done(stats, target.tag_id):
                    stats["tags_done"] += 1
                    return

                resume_pages, resume_page_ts = (0, None)
                if tag_resume and not tags_from_start:
                    resume_pages, resume_page_ts = tag_progress_from_stats(stats, target.tag_id)

                tag_begin_kv: list[tuple[str, Any]] = [
                    ("event", "scrape.tag.begin"),
                    ("tag_id", target.tag_id),
                    ("group", target.group_name),
                    ("tag", target.tag_name),
                    ("page_num", resume_pages or None),
                    ("page_next_ts", resume_page_ts),
                ]
                api_gt = lookup_group_tag_by_id(groups, target.tag_id)
                if api_gt is not None:
                    api_g, api_t = api_gt
                    tag_begin_kv.extend([("api_group", api_g), ("api_tag", api_t)])
                logger.info(
                    "%s",
                    pf_kv(
                        tag_begin_kv,
                        zh=(
                            f"开始爬取 tagId={target.tag_id}「{target.group_name} / {target.tag_name}」"
                            + (
                                f"（commodity/tags 对应「{api_gt[0]} / {api_gt[1]}」）"
                                if api_gt is not None
                                else "（commodity/tags 未找到该 tagId）"
                            )
                        ),
                    ),
                )
                if api_gt is None:
                    logger.warning(
                        "%s",
                        pf_kv(
                            [
                                ("event", "scrape.tag.id_unknown"),
                                ("tag_id", target.tag_id),
                                ("group", target.group_name),
                                ("tag", target.tag_name),
                            ],
                            zh=f"tagId {target.tag_id} 不在本次 commodity/tags 树中，列表可能不对",
                        ),
                    )

                tag_had_pages = False
                for new_items, _raw_pg, page_num in iter_tag_list_pages(
                    tag_page,
                    album_id,
                    target.tag_id,
                    max_pages=max_list_pages,
                    gap=gap,
                    resume_page_ts=resume_page_ts,
                    resume_pages=resume_pages,
                ):
                    if stop_flag:
                        break
                    tag_had_pages = True
                    resume_page_ts = None
                    stats["tag_list_pages"] += 1
                    stats["list_items_unique"] += len(new_items)
                    if isinstance(_raw_pg, dict):
                        update_tag_progress_in_stats(
                            stats,
                            tag_id=target.tag_id,
                            page_num=page_num,
                            raw_page=_raw_pg,
                        )
                    from product_feed_kr.common.cny_krw_rate import resolve_krw_per_cny

                    stats["fx_source"] = None
                    try:
                        krw_per_cny_page, fx_src = resolve_krw_per_cny()
                        stats["krw_per_cny"] = krw_per_cny_page
                        stats["fx_source"] = fx_src
                        logger.info(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.fx"),
                                    ("tag_id", target.tag_id),
                                    ("page", page_num),
                                    ("krw_per_cny", round(krw_per_cny_page, 4)),
                                    ("fx_source", fx_src),
                                ],
                                zh="本页抓取使用 CNY→KRW 汇率",
                            ),
                        )
                    except Exception as e:
                        logger.warning(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.fx.err"),
                                    ("tag_id", target.tag_id),
                                    ("page", page_num),
                                    ("err", str(e)[:300]),
                                ],
                                zh="本页无法获取汇率，韩元价留空",
                            ),
                        )

                    if need_backfill_listed_at and conn_db is not None:
                        from product_feed_kr.wecatalog.wecatalog_listed_at import (
                            wecatalog_listed_at_iso_from_list_item,
                        )

                        backfill_updates: dict[str, str] = {}
                        for it in new_items:
                            if not isinstance(it, dict):
                                continue
                            gid = (
                                it.get("goods_id")
                                or it.get("selfGoodsId")
                                or it.get("parent_goods_id")
                                or ""
                            )
                            if not isinstance(gid, str) or not gid:
                                continue
                            if gid not in need_backfill_listed_at:
                                continue
                            listed_iso = wecatalog_listed_at_iso_from_list_item(it)
                            if listed_iso:
                                backfill_updates[gid] = listed_iso
                        if backfill_updates:
                            n_bf = sqlite_backfill_wecatalog_listed_at(
                                conn_db,
                                album_id,
                                backfill_updates,
                            )
                            stats["listed_at_backfilled"] += n_bf
                            for gid in backfill_updates:
                                need_backfill_listed_at.discard(gid)

                    for it in new_items:
                        if not append_product_row(
                            it,
                            target.group_name,
                            target.tag_name,
                            target.tag_id,
                            target.shop_path,
                        ):
                            stop_flag = True
                            break

                    rec_count = len(records)
                    write_checkpoint()
                    if stop_flag:
                        break

                if not stop_flag and not tag_had_pages:
                    tp = stats.setdefault(TAG_PROGRESS_KEY, {})
                    tp[str(target.tag_id)] = {TAG_DONE_KEY: True, TAG_PAGE_NUM_KEY: 0}
                done_flag = tag_is_done(stats, target.tag_id)
                if not stop_flag and done_flag:
                    stats["tags_done"] += 1
                logger.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "scrape.tag.done"),
                            ("tag_id", target.tag_id),
                            ("group", target.group_name),
                            ("tag", target.tag_name),
                            ("done", done_flag),
                        ],
                        zh=f"标签「{target.group_name} / {target.tag_name}」遍历结束",
                    ),
                )
                write_checkpoint()

            for target in targets:
                if stop_flag:
                    break
                process_target(target, page)

            stats["records"] = len(records)
            stats["records_new"] = new_appended
            write_checkpoint()
            logger.info(
                "%s",
                pf_kv(
                    [
                        ("event", "scrape.done"),
                        ("records", stats["records"]),
                        ("new", stats["records_new"]),
                        ("skipped", stats["skipped_existing"]),
                        ("skipped_blacklist", stats["skipped_blacklist"] or None),
                        ("tags_total", stats["tags_total"]),
                        ("tags_done", stats["tags_done"]),
                        ("list_ok", stats["list_ok"] or None),
                        ("list_incomplete", stats["list_incomplete"] or None),
                        ("list_no_price", stats["list_no_price"] or None),
                        ("album_id", album_id),
                    ],
                    zh="按标签抓取流程正常结束",
                ),
            )
        finally:
            browser.close()
            p.stop()
    finally:
        if conn_db is not None:
            try:
                conn_db.close()
            except Exception:
                pass

    return stats


def main() -> int:
    delay_default = (getenv("WECATALOG_DETAIL_DELAY", "5") or "5").strip()
    max_list_pages_default = -1
    max_pages_cfg = (getenv("WECATALOG_SCRAPE_MAX_LIST_PAGES") or "").strip()
    if max_pages_cfg:
        try:
            max_list_pages_default = normalize_max_list_pages(int(max_pages_cfg))
        except ValueError:
            pass
    ap = argparse.ArgumentParser(description="wecatalog 店铺：按已配对标签拉列表 → SQLite")
    ap.add_argument(
        "--store-url",
        default="https://www.wecatalog.cn/weshop/store/_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg",
        help="店铺页 URL（含 /weshop/store/{albumId}）",
    )
    ap.add_argument("--trans-lang", default="zh", help="commodity/tags 的 transLang")
    ap.add_argument(
        "--detail-delay",
        type=str,
        default=delay_default,
        metavar="SEC|A,B",
        help="相邻微猫 API 间隔：固定秒数如 5；或闭区间随机如 3,8 / 3:8（每次在 [A,B] 内随机休眠）。未传本参数时默认读环境变量或 seven17.json 的 WECATALOG_DETAIL_DELAY。详情缓存命中不发起请求故不占间隔",
    )
    ap.add_argument(
        "--max-list-pages",
        type=int,
        default=max_list_pages_default,
        help="每个标签列表翻页上限（每页约 32 条）；-1 或 0 不限，直到 isLoadMore=false",
    )
    ap.add_argument(
        "--skip-detail",
        action="store_true",
        help="只拉列表，不写入商品",
    )
    ap.add_argument(
        "--checkpoint-every",
        type=int,
        default=0,
        help="每累计 N 条新增写入一次 checkpoint（0 不按条数额外写；仍会每列表页结束写）",
    )
    ap.add_argument(
        "--max-records",
        type=int,
        default=0,
        help="本Run最多新增多少条「分组×标签×商品」记录（0 不限）；不含库内已有 goods_id",
    )
    ap.add_argument(
        "--tags-from-start",
        action="store_true",
        help="忽略各标签翻页断点，从第一个已配对标签重新遍历",
    )
    ap.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="日志文件；常驻模式默认 data/wecatalog_scrape_store.log",
    )
    ap.add_argument("--once", action="store_true", help="只执行一轮后退出")
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG 级别（详情请求、跳过等更细）")
    ap.add_argument(
        "--headed",
        action="store_true",
        help="有界面运行浏览器（默认无头 headless，看不到窗口）",
    )
    ap.add_argument(
        "--worker-index",
        type=int,
        default=None,
        help="多进程 worker 编号（0 起，须与 --worker-count 同用；由 01 bat 启动）",
    )
    ap.add_argument(
        "--worker-count",
        type=int,
        default=None,
        help="多进程 worker 总数（通常等于抓取白名单 max_parallel）",
    )
    args = ap.parse_args()

    worker_mode = args.worker_index is not None or args.worker_count is not None
    if worker_mode:
        if args.worker_index is None or args.worker_count is None:
            ap.error("--worker-index 与 --worker-count 须同时指定")
        if args.worker_count < 1:
            ap.error("--worker-count 须 >= 1")
        if args.worker_index < 0 or args.worker_index >= args.worker_count:
            ap.error(f"--worker-index 须在 [0, {args.worker_count}) 内")
    else:
        args.worker_index = 0
        args.worker_count = 1

    try:
        d_lo, d_hi = parse_detail_delay_range(args.detail_delay)
    except ValueError as e:
        ap.error(f"无效 --detail-delay {args.detail_delay!r}: {e}")

    repeat = not args.once and not worker_mode
    log_file = args.log_file
    if repeat and log_file is None:
        from product_feed_kr._paths import REPO_ROOT

        log_file = REPO_ROOT / "data" / "wecatalog_scrape_store.log"
    configure_scrape_logging(log_file, verbose=args.verbose)

    max_list_pages = normalize_max_list_pages(args.max_list_pages)
    tag_resume = bool_env("WECATALOG_SCRAPE_TAG_RESUME", True)
    tags_from_start = args.tags_from_start or bool_env("WECATALOG_SCRAPE_TAGS_FROM_START", False)

    def _run_once() -> int:
        try:
            stats = scrape_store(
                args.store_url.strip(),
                trans_lang=args.trans_lang.strip() or "zh",
                detail_delay_range=(d_lo, d_hi),
                max_list_pages=max_list_pages,
                skip_detail=args.skip_detail,
                tag_resume=tag_resume,
                tags_from_start=tags_from_start,
                checkpoint_every=max(0, args.checkpoint_every),
                max_records=max(0, args.max_records),
                headed=args.headed,
                worker_index=args.worker_index,
                worker_count=args.worker_count,
            )
            print(json.dumps({"ok": True, **stats}, ensure_ascii=False))
            if stats.get("restart_fresh"):
                reload_seven17_config()
                return EXIT_RESTART_FRESH_DATA
            return 0
        except Exception as e:
            logger.exception(
                "%s",
                pf_kv([("event", "scrape.fatal"), ("err", str(e))], zh="抓取主流程未捕获异常，已中止"),
            )
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
            return 1

    lock_name = (
        f"wecatalog_scrape_store_w{args.worker_index}"
        if worker_mode
        else "wecatalog_scrape_store"
    )
    try:
        with single_instance_lock(lock_name):
            if repeat:
                return run_forever(
                    _run_once,
                    task_label="wecatalog_scrape_store",
                    logger=logger,
                    on_restart_fresh=reload_seven17_config,
                )
            return _run_once()
    except SystemExit as e:
        if e.code == EXIT_SINGLETON_CONFLICT:
            return EXIT_SINGLETON_CONFLICT
        raise


if __name__ == "__main__":
    raise SystemExit(main())
