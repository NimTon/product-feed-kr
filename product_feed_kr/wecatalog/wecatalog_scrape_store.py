"""微猫 wecatalog 店铺：``commodity/tags`` + ``album/personal/all`` 列表直解析 → SQLite。

默认 **不** 逐条请求 ``popUpsInfoV2``（列表项已含 title / optimaPrice / formats / colors / imgsSrc）。
``--use-popups`` 或 ``WECATALOG_SCRAPE_USE_POPUPS=true`` 可恢复旧逻辑。

列表项缺 title/图 → ``pf_scrape_skip``（``list_incomplete``）；无价且分类不在白名单 → ``list_no_price``；库内已有 ``goods_id`` 跳过。
``--detail-delay`` 节流作用于 **相邻两次微猫 API**（tags、列表翻页）；列表模式下不对每条商品休眠。

示例::

  python -m product_feed_kr.wecatalog.wecatalog_scrape_store \\
    --store-url \"https://www.wecatalog.cn/weshop/store/{albumId}\" \\
    --detail-delay 5

  python -m product_feed_kr.wecatalog.wecatalog_scrape_store \\
    --store-url \"https://www.wecatalog.cn/weshop/store/{albumId}\" \\
    --detail-delay 3,8

可选配置 ``PRODUCT_FEED_SQLITE``：SQLite 文件路径（默认 ``data/product_feed.db``）。

``WECATALOG_SCRAPE_RESTART_AFTER_ITEMS``（默认 1000）：本 run 新增写入 SQLite 达 N 条后退出码 **75**，供外层 bat 立即重跑以刷新进程与配置；0 关闭。

``WECATALOG_DETAIL_DELAY``（默认 ``5``）：未传命令行 ``--detail-delay`` 时的节流默认值；格式与 ``--detail-delay`` 相同（如 ``"3,8"`` 或 JSON 数组 ``[3, 8]``）。环境变量优先于 ``seven17.json``。

``WECATALOG_SCRAPE_SKIP_UNCATEGORIZED``（默认 ``true``）：仅爬取已在 ``data/wecatalog_category_pairs.json`` 中完成分类映射的商品，未映射的标签/分组下的商品将被跳过。分类映射请在 **05_查看商品库** 的「分类配对」中维护。设为 ``false`` / ``0`` 时也爬未映射商品。

日志：与上架脚本同一套格式（**`event=`** + 短模块名 **`scrape:`**）；默认 **INFO** stderr；**`--log-file`** UTF-8；**`-v`** DEBUG。**`--headed`** 有界面浏览器。
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
from product_feed_kr.wecatalog.wecatalog_popups import (
    POPUPS_ERR_COMMODITY_INVALID,
    POPUPS_ERR_LOGIN_EXPIRED,
    popups_errcode,
    popups_errmsg,
    popups_response_ready,
    popups_scrape_skip_reason,
)
from product_feed_kr.wecatalog.wecatalog_scrape_fields import (
    attach_scrape_fields_to_record,
    fields_from_list_item,
    fields_from_popups_response,
    list_item_scrape_ready,
    scrape_no_price_skip_needed,
)
from product_feed_kr.wecatalog.wecatalog_tag_mapping import resolve_category_path
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
                logger.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "scrape.throttle"),
                            ("delay_sec", round(sec, 3)),
                            ("delay_range", [self._lo, self._hi]),
                            ("next", label),
                        ],
                        zh=f"请求节流：休眠 {sec:.3g}s 后发起「{label}」",
                    ),
                )
                time.sleep(sec)
        self._n += 1


FETCH_LIST_JS = """
async ({ albumId, pageTimestamp }) => {
  let url;
  if (pageTimestamp == null) {
    url = `https://www.wecatalog.cn/album/personal/all?&albumId=${encodeURIComponent(albumId)}`
      + `&startDate=&endDate=&requestDataType=`;
  } else {
    url = `https://www.wecatalog.cn/album/personal/all?&albumId=${encodeURIComponent(albumId)}`
      + `&startDate=&endDate=&slipType=1&timestamp=${pageTimestamp}&requestDataType=`;
  }
  const r = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "Accept": "application/json",
    },
    body: "{}",
  });
  return await r.json();
}
"""

FETCH_TAGS_JS = """
async (apiUrl) => {
  const r = await fetch(apiUrl, { credentials: "include" });
  return await r.json();
}
"""

FETCH_POPUPS_JS = """
async ({ sellerAlbumId, commodityId }) => {
  const qs = new URLSearchParams({
    sellerAlbumId,
    commodityId,
    popUpsType: "individualShopping",
  });
  const u = "https://www.wecatalog.cn/newOrder/api/v1/shoppingCart/popUpsInfoV2?" + qs.toString();
  const r = await fetch(u, {
    credentials: "include",
    headers: { Accept: "application/json, text/plain, */*" },
  });
  return await r.json();
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


def _tag_ids_on_item(item: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for t in item.get("tags") or []:
        if isinstance(t, dict) and t.get("tagId") is not None:
            try:
                out.add(int(t["tagId"]))
            except (TypeError, ValueError):
                continue
    return out


def normalize_max_list_pages(raw: int | None) -> int:
    """``<= 0``（含 ``-1``）不限页数；正数为最大翻页次数。"""
    if raw is None or raw <= 0:
        return -1
    return int(raw)


LIST_PAGE_NUM_KEY = "list_page_num"
LIST_PAGE_NEXT_TS_KEY = "list_page_next_ts"


def list_progress_from_stats(stats: dict[str, Any] | None) -> tuple[int, int | None]:
    """从 ``pf_store_info.stats_json`` 读取列表翻页断点（已完成的页码、下一页 timestamp）。"""
    if not isinstance(stats, dict):
        return 0, None
    page_num = 0
    raw_num = stats.get(LIST_PAGE_NUM_KEY)
    if raw_num is not None:
        try:
            page_num = max(0, int(raw_num))
        except (TypeError, ValueError):
            page_num = 0
    raw_ts = stats.get(LIST_PAGE_NEXT_TS_KEY)
    if raw_ts is None:
        return page_num, None
    try:
        return page_num, int(raw_ts)
    except (TypeError, ValueError):
        return page_num, None


def update_list_progress_in_stats(stats: dict[str, Any], *, page_num: int, raw_page: dict[str, Any]) -> None:
    """本页处理完后更新断点；列表正常结束时清除 ``list_page_next_ts``。"""
    stats[LIST_PAGE_NUM_KEY] = int(page_num)
    result = raw_page.get("result") if isinstance(raw_page.get("result"), dict) else {}
    pag = result.get("pagination") if isinstance(result.get("pagination"), dict) else {}
    next_ts = pag.get("pageTimestamp")
    if pag.get("isLoadMore") and next_ts is not None:
        try:
            stats[LIST_PAGE_NEXT_TS_KEY] = int(next_ts)
        except (TypeError, ValueError):
            stats.pop(LIST_PAGE_NEXT_TS_KEY, None)
    else:
        stats.pop(LIST_PAGE_NEXT_TS_KEY, None)


def clear_list_progress_in_stats(stats: dict[str, Any]) -> None:
    stats.pop(LIST_PAGE_NUM_KEY, None)
    stats.pop(LIST_PAGE_NEXT_TS_KEY, None)


def iter_album_list_pages(
    page,
    album_id: str,
    *,
    max_pages: int,
    gap: InterRequestGap,
    resume_page_ts: int | None = None,
    resume_pages: int = 0,
):
    """逐页请求 album/personal/all；每页 yield (本页新增去重 items, 原始响应, 当前页码 1-based)。

    ``max_pages <= 0`` 时不限页数，直到 ``isLoadMore=false`` 或列表为空/异常。
    ``resume_page_ts`` 非空时从该 ``pageTimestamp`` 继续（断点续拉）。
    """
    seen: set[str] = set()
    ts: int | None = resume_page_ts
    pages = max(0, int(resume_pages))
    unlimited = max_pages <= 0
    if resume_page_ts is not None:
        logger.info(
            "%s",
            pf_kv(
                [
                    ("event", "scrape.list.resume"),
                    ("list_page_num", pages),
                    ("list_page_next_ts", resume_page_ts),
                ],
                zh=f"从列表翻页断点继续（已完成 {pages} 页，下一请求 timestamp={resume_page_ts}）",
            ),
        )
    while True:
        if not unlimited and pages >= max_pages:
            logger.info(
                "%s",
                pf_kv(
                    [
                        ("event", "scrape.list.page_limit"),
                        ("page", pages),
                        ("max_pages", max_pages),
                    ],
                    zh=f"已达列表翻页上限 {max_pages} 页，停止继续请求",
                ),
            )
            break
        gap.before(f"album/personal/all 列表第 {pages + 1} 页")
        raw = page.evaluate(FETCH_LIST_JS, {"albumId": album_id, "pageTimestamp": ts})
        pages += 1
        if not isinstance(raw, dict) or raw.get("errcode") not in (0, None):
            logger.warning(
                "%s",
                pf_kv(
                    [
                        ("event", "scrape.list.bad_response"),
                        ("page", pages),
                        ("errcode", raw.get("errcode") if isinstance(raw, dict) else None),
                    ],
                    zh="店铺列表接口返回异常或非成功 errcode，停止翻页",
                ),
            )
            break
        result = raw.get("result")
        if not isinstance(result, dict):
            logger.warning(
                "%s",
                pf_kv(
                    [("event", "scrape.list.no_result"), ("page", pages)],
                    zh="列表响应里没有 result 对象",
                ),
            )
            break
        items = result.get("items")
        if not isinstance(items, list) or len(items) == 0:
            logger.info(
                "%s",
                pf_kv(
                    [("event", "scrape.list.empty_items"), ("page", pages)],
                    zh="本页商品列表为空，列表遍历结束",
                ),
            )
            break

        page_new_items: list[dict[str, Any]] = []
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
                page_new_items.append(it)
                page_new += 1

        pag = result.get("pagination") if isinstance(result.get("pagination"), dict) else {}
        logger.info(
            "%s",
            pf_kv(
                [
                    ("event", "scrape.list.page"),
                    ("page", pages),
                    ("max_pages", max_pages if not unlimited else -1),
                    ("items", len(items)),
                    ("page_new", page_new),
                    ("seen", len(seen)),
                    ("isLoadMore", pag.get("isLoadMore")),
                ],
                zh="已拉取一页店铺列表并统计本页新增/累计去重",
            ),
        )

        yield page_new_items, raw, pages

        if not pag.get("isLoadMore"):
            logger.info(
                "%s",
                pf_kv([("event", "scrape.list.no_more"), ("isLoadMore", 0)], zh="分页标记无更多页，列表结束"),
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
    skip_uncategorized: bool = False,
    use_list_only: bool = True,
    list_resume: bool = True,
    list_from_start: bool = False,
    checkpoint_every: int = 0,
    max_records: int = 0,
    headed: bool = False,
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

    logger.info(
        "%s",
        pf_kv(
            [
                ("event", "scrape.start"),
                ("album_id", album_id),
                ("seed", seed),
                ("sqlite", str(sqlite_db_path())),
                ("throttle_delay_sec_range", [delay_lo, delay_hi]),
                ("skip_uncategorized", skip_uncategorized),
                ("use_list_only", use_list_only),
            ],
            zh="开始抓取微猫店铺：打开种子页并准备写 SQLite"
            + ("（列表直解析，不请求 popUps）" if use_list_only else "（逐条 popUpsInfoV2）")
            + ("（跳过无分类商品）" if skip_uncategorized else ""),
        ),
    )

    stats = {
        "album_id": album_id,
        "list_pages": 0,
        "list_items_unique": 0,
        "popups_ok": 0,
        "popups_err": 0,
        "popups_expired": 0,
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
        "use_list_only": use_list_only,
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

        resume_page_ts: int | None = None
        resume_pages = 0
        if list_resume and not list_from_start:
            snap = sqlite_load_store_snapshot(conn_db, album_id)
            prior_stats = snap.get("stats") if isinstance(snap, dict) else None
            done_pages, next_ts = list_progress_from_stats(
                prior_stats if isinstance(prior_stats, dict) else None,
            )
            if next_ts is not None:
                resume_page_ts = next_ts
                resume_pages = done_pages
                stats[LIST_PAGE_NUM_KEY] = done_pages
                stats[LIST_PAGE_NEXT_TS_KEY] = next_ts
            elif isinstance(prior_stats, dict) and LIST_PAGE_NUM_KEY in prior_stats:
                stats[LIST_PAGE_NUM_KEY] = prior_stats.get(LIST_PAGE_NUM_KEY)

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
            logger.info(
                "%s",
                pf_kv([("event", "scrape.tags_ok"), ("groups", len(groups))], zh="分类/标签树拉取成功"),
            )

            logger.info(
                "%s",
                pf_kv(
                    [
                        ("event", "scrape.list.begin"),
                        ("max_list_pages", max_list_pages),
                        ("list_resume", list_resume and not list_from_start),
                        ("list_page_num", resume_pages or None),
                        ("list_page_next_ts", resume_page_ts),
                    ],
                    zh="开始按页遍历店铺商品列表"
                    + ("（从断点续拉）" if resume_page_ts is not None else ""),
                ),
            )
            seen_popups: dict[str, dict[str, Any] | None] = {}

            def write_checkpoint(*, meta_only: bool = False) -> None:
                meta_extra = {
                    "saved_at": now_cst8_iso(),
                    "stats": stats,
                }
                assert conn_db is not None
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
                        "写库：店铺进度 + 本 run 已抓到 popUps 的商品"
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
            popups_fetch_n = 0
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
                nonlocal new_appended, popups_fetch_n
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
                                zh="跳过 pf_scrape_skip 中已失效 goods_id（不请求 popUps）",
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
                                zh="跳过库内已有 goods_id（不抓 popUps、不写库）",
                            ),
                        )
                    return True

                if skip_detail:
                    return True

                scrape_fields: dict[str, Any] | None = None
                pop_resp: dict[str, Any] | None = None

                if use_list_only:
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
                                    zh="列表项缺 title/图，已记入 pf_scrape_skip（不请求 popUps）",
                                ),
                            )
                        return True
                elif gid in seen_popups:
                    cached_pop = seen_popups[gid]
                    if isinstance(cached_pop, dict):
                        pop_resp = cached_pop
                        if popups_response_ready(cached_pop):
                            scrape_fields = fields_from_popups_response(cached_pop)
                            logger.debug(
                                "%s",
                                pf_kv(
                                    [("event", "scrape.popups.cache"), ("goods_id", gid)],
                                    zh="popUpsInfoV2：复用本 run 已拉过的缓存",
                                ),
                            )
                else:
                    popups_fetch_n += 1
                    gap.before(
                        f"popUpsInfoV2 #{popups_fetch_n} goods_id={gid}",
                    )
                    logger.debug(
                        "%s",
                        pf_kv(
                            [
                                ("event", "scrape.popups.request"),
                                ("n", popups_fetch_n),
                                ("goods_id", gid),
                            ],
                            zh="正在请求 popUpsInfoV2",
                        ),
                    )
                    p_raw = page.evaluate(
                        FETCH_POPUPS_JS,
                        {"sellerAlbumId": album_id, "commodityId": gid},
                    )
                    pop_resp = p_raw if isinstance(p_raw, dict) else {"error": str(p_raw)}
                    seen_popups[gid] = pop_resp
                    if popups_response_ready(pop_resp):
                        scrape_fields = fields_from_popups_response(pop_resp)
                        stats["popups_ok"] += 1
                    else:
                        stats["popups_err"] += 1

                if not use_list_only and scrape_fields is None:
                    ec = popups_errcode(pop_resp)
                    em = popups_errmsg(pop_resp)
                    if ec == POPUPS_ERR_COMMODITY_INVALID:
                        stats["popups_expired"] += 1
                        skip_ids_prior.add(gid)
                        skip_ids_blacklist.add(gid)
                        g_url = goods_page_url(album_id, gid)
                        skip_reason = popups_scrape_skip_reason(pop_resp, errcode=ec)
                        if skip_reason and conn_db is not None:
                            sqlite_record_scrape_skip(
                                conn_db,
                                album_id,
                                gid,
                                reason=skip_reason,
                                errcode=ec,
                                errmsg=em,
                                goods_url=g_url,
                            )
                        logger.info(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.popups.skip_invalid"),
                                    ("goods_id", gid),
                                    ("goods_url", g_url),
                                    ("errcode", ec),
                                    ("errmsg", em or None),
                                ],
                                zh=f"popUpsInfoV2：商品已失效，已记入 pf_scrape_skip goods_url={g_url}",
                            ),
                        )
                    elif ec == POPUPS_ERR_LOGIN_EXPIRED:
                        logger.warning(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.popups.login_expired"),
                                    ("goods_id", gid),
                                    ("errcode", ec),
                                    ("errmsg", em or None),
                                ],
                                zh="popUpsInfoV2：登录已过期，请用 --headed 在浏览器内登录微猫后重试",
                            ),
                        )
                    else:
                        logger.warning(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.popups.err"),
                                    ("goods_id", gid),
                                    ("errcode", ec),
                                    ("errmsg", em or None),
                                ],
                                zh="popUpsInfoV2 未返回商品数据"
                                + (f"：{em}" if em else ""),
                            ),
                        )
                    return True

                if not str(scrape_fields.get("commodity_title") or "").strip():
                    logger.warning(
                        "%s",
                        pf_kv(
                            [
                                ("event", "scrape.popups.no_title"),
                                ("goods_id", gid),
                                ("goods_url", goods_page_url(album_id, gid)),
                            ],
                            zh="popUpsInfoV2 成功但无商品标题（commodityName/title 均为空），跳过入库",
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

                if use_list_only:
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

            stop_run = False
            skipped_uncategorized = 0
            for new_items, _raw_pg, page_num in iter_album_list_pages(
                page,
                album_id,
                max_pages=max_list_pages,
                gap=gap,
                resume_page_ts=resume_page_ts,
                resume_pages=resume_pages,
            ):
                resume_page_ts = None
                stats["list_pages"] = page_num
                stats["list_items_unique"] += len(new_items)
                if isinstance(_raw_pg, dict):
                    update_list_progress_in_stats(stats, page_num=page_num, raw_page=_raw_pg)
                from product_feed_kr.common.cny_krw_rate import resolve_krw_per_cny

                krw_per_cny_page = None
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
                                ("page", page_num),
                                ("krw_per_cny", round(krw_per_cny_page, 4)),
                                ("fx_source", fx_src),
                            ],
                            zh="本页抓取使用 CNY→KRW 汇率（韩元价在入库时按千韩元取整）",
                        ),
                    )
                except Exception as e:
                    logger.warning(
                        "%s",
                        pf_kv(
                            [
                                ("event", "scrape.fx.err"),
                                ("page", page_num),
                                ("err", str(e)[:300]),
                            ],
                            zh="本页无法获取汇率，商品仍将写入人民币价，韩元价留空",
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
                        if not isinstance(gid, str) or not gid or gid not in need_backfill_listed_at:
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
                        if n_bf and (
                            stats["listed_at_backfilled"] <= n_bf
                            or stats["listed_at_backfilled"] % 500 == 0
                        ):
                            logger.info(
                                "%s",
                                pf_kv(
                                    [
                                        ("event", "scrape.listed_at.backfill"),
                                        ("page", page_num),
                                        ("goods_id", len(backfill_updates)),
                                        ("rows", n_bf),
                                        ("remaining", len(need_backfill_listed_at)),
                                    ],
                                    zh="列表翻页补全微猫上架时间",
                                ),
                            )

                def _iter_groups_tags(*, categorized_only: bool) -> None:
                    """遍历 groups×tags 处理 new_items；categorized_only=True 时只处理有分类的。"""
                    nonlocal stop_run, skipped_uncategorized
                    for g in groups:
                        if stop_run:
                            break
                        gname = str(g.get("groupName") or "").strip()
                        raw_tags = g.get("tags") or []
                        if not isinstance(raw_tags, list):
                            continue
                        for t in raw_tags:
                            if stop_run:
                                break
                            if not isinstance(t, dict):
                                continue
                            tid = t.get("tagId")
                            tname = str(t.get("tagName") or "").strip()
                            if tid is None or not tname:
                                continue
                            try:
                                tid_int = int(tid)
                            except (TypeError, ValueError):
                                continue
                            shop_path = resolve_category_path(gname, tname)
                            has_category = shop_path is not None and all(
                                "（待补全）" not in seg for seg in shop_path
                            )
                            if categorized_only and not has_category:
                                continue
                            if not categorized_only and has_category:
                                continue
                            for it in new_items:
                                if tid_int not in _tag_ids_on_item(it):
                                    continue
                                if not has_category:
                                    skipped_uncategorized += 1
                                if not append_product_row(it, gname, tname, tid_int, shop_path):
                                    stop_run = True
                                    break
                            if stop_run:
                                break

                _iter_groups_tags(categorized_only=True)
                if not stop_run and not skip_uncategorized:
                    _iter_groups_tags(categorized_only=False)

                logger.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "scrape.list.page_done"),
                            ("page", page_num),
                            ("page_items", len(new_items)),
                            ("list_unique", stats["list_items_unique"]),
                            ("records", len(records)),
                            ("uncategorized_items", skipped_uncategorized if skipped_uncategorized else None),
                        ],
                        zh="本列表页处理完并已 checkpoint",
                    ),
                )
                write_checkpoint()
                if stop_run:
                    break

            stats["records"] = len(records)
            stats["records_new"] = new_appended
            stats["skipped_uncategorized"] = skipped_uncategorized
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
                        ("skipped_uncategorized", skipped_uncategorized),
                        ("skip_uncategorized_enabled", skip_uncategorized),
                        ("use_list_only", use_list_only),
                        ("list_ok", stats["list_ok"] or None),
                        ("list_incomplete", stats["list_incomplete"] or None),
                        ("list_no_price", stats["list_no_price"] or None),
                        ("popups_ok", stats["popups_ok"] or None),
                        ("popups_err", stats["popups_err"] or None),
                        ("popups_expired", stats["popups_expired"] or None),
                        ("album_id", album_id),
                    ],
                    zh="店铺抓取流程正常结束",
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
    ap = argparse.ArgumentParser(description="wecatalog 店铺：tags + 列表 → SQLite（默认列表直解析）")
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
        help="列表翻页上限（每页约 32 条）；-1 或 0 不限，直到 isLoadMore=false",
    )
    ap.add_argument(
        "--skip-detail",
        action="store_true",
        help="只拉列表匹配分类，不写入商品详情",
    )
    ap.add_argument(
        "--use-popups",
        action="store_true",
        help="恢复旧逻辑：逐条请求 popUpsInfoV2（默认仅用 album/personal/all 列表字段）",
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
        "--skip-uncategorized",
        action="store_true",
        default=None,
        help="跳过未映射分类的商品（仅爬有分类的）；未传时读 WECATALOG_SCRAPE_SKIP_UNCATEGORIZED 配置",
    )
    ap.add_argument(
        "--list-from-start",
        action="store_true",
        help="忽略库内列表翻页断点，从第 1 页重新遍历",
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
    args = ap.parse_args()

    try:
        d_lo, d_hi = parse_detail_delay_range(args.detail_delay)
    except ValueError as e:
        ap.error(f"无效 --detail-delay {args.detail_delay!r}: {e}")

    repeat = not args.once
    log_file = args.log_file
    if repeat and log_file is None:
        from product_feed_kr._paths import REPO_ROOT

        log_file = REPO_ROOT / "data" / "wecatalog_scrape_store.log"
    configure_scrape_logging(log_file, verbose=args.verbose)

    skip_uncat = args.skip_uncategorized if args.skip_uncategorized is not None else bool_env("WECATALOG_SCRAPE_SKIP_UNCATEGORIZED", True)
    use_list_only = not args.use_popups and not bool_env("WECATALOG_SCRAPE_USE_POPUPS", False)
    max_list_pages = normalize_max_list_pages(args.max_list_pages)
    list_resume = bool_env("WECATALOG_SCRAPE_LIST_RESUME", True)
    list_from_start = args.list_from_start or bool_env("WECATALOG_SCRAPE_LIST_FROM_START", False)

    def _run_once() -> int:
        try:
            stats = scrape_store(
                args.store_url.strip(),
                trans_lang=args.trans_lang.strip() or "zh",
                detail_delay_range=(d_lo, d_hi),
                max_list_pages=max_list_pages,
                skip_detail=args.skip_detail,
                skip_uncategorized=skip_uncat,
                use_list_only=use_list_only,
                list_resume=list_resume,
                list_from_start=list_from_start,
                checkpoint_every=max(0, args.checkpoint_every),
                max_records=max(0, args.max_records),
                headed=args.headed,
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

    try:
        with single_instance_lock("wecatalog_scrape_store"):
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
