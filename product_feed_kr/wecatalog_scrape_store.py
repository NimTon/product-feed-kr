"""微猫 wecatalog 店铺：先拉 **线上分类树**，再按其顺序遍历分组→标签拉 **popUpsInfoV2**，写入 **本地 SQLite**（``data/product_feed.db`` 或 ``PRODUCT_FEED_SQLITE``）。

1. **`commodity/tags`**：得到分组顺序与每个叶子 `tagId` / `tagName`，遍历顺序与相册后台一致。
2. **`album/personal/all`**：分页拉全店列表；**每拉一页即按分类树匹配并拉 popUps**，不先攒齐全部列表再处理。
3. **分类映射（两份）**：每次抓取开始前
   - **`config/wecatalog_tag_category_map.txt`** → **`wecatalog_tag_category_map.json`**（微猫分组/标签 → 韩文路径，用户维护）；
   - **`data/seven17_path_ca_map.json`**（韩文路径 → seven17 ``ca_id``，从 itemform 自动同步，需配置 ``SEVEN17_MB_*``）。
   未映射的 `(分组, 标签)` 会自动追加到 txt（路径占位 ``（待补全）``）；``tag_id`` 由 commodity/tags API 写入 JSON ``meta``，勿写在路径行尾。

每条 **`pf_store_item`** 只写入结构化字段（title / 图 URL / 价 / 尺码 / 颜色），**不保存** ``detail_response`` / ``popups_response`` 原始 JSON。
每条另有 **`uploaded_to_platform`**（布尔）：抓取时默认为 **`false`**。

增量：库内已有 **`goods_id`** 本次跳过（不请求 popUps、不写库）。**仅在本 run 抓到有效 title 后**才 upsert 对应商品行，不把启动时整表载入内存写回（避免覆盖并行 LLM 结果）。抓取与上架对同一 ``.db`` 使用 **文件锁**（``*.db.lock``）互斥写。

列表：`POST .../album/personal/all`，分页用 `result.pagination.pageTimestamp` → 下一页 `timestamp`。

商品数据：仅 ``GET .../popUpsInfoV2``（``result.commodity``：title / 图 / 价 / 尺码 / 颜色）。

**节流：** `--detail-delay` 作用于任意相邻两次微猫 API（首次请求不休眠）。写单个数如 ``5`` 表示固定 5 秒；写区间如 ``3,8`` 或 ``3:8`` 表示每次在闭区间 **[A,B]** 内均匀随机秒数再休眠。同一 ``goods_id`` 在本 run 内命中 popUps 缓存则不重复请求。

示例::

  python -m product_feed_kr.wecatalog_scrape_store \\
    --store-url \"https://www.wecatalog.cn/weshop/store/{albumId}\" \\
    --detail-delay 5

  python -m product_feed_kr.wecatalog_scrape_store \\
    --store-url \"https://www.wecatalog.cn/weshop/store/{albumId}\" \\
    --detail-delay 3,8

可选配置 ``PRODUCT_FEED_SQLITE``：SQLite 文件路径（默认 ``data/product_feed.db``）。

``WECATALOG_SCRAPE_RESTART_AFTER_ITEMS``（默认 1000）：本 run 新增写入 SQLite 达 N 条后退出码 **75**，供外层 bat 立即重跑以刷新进程与配置；0 关闭。

``WECATALOG_DETAIL_DELAY``（默认 ``5``）：未传命令行 ``--detail-delay`` 时的节流默认值；格式与 ``--detail-delay`` 相同（如 ``"3,8"`` 或 JSON 数组 ``[3, 8]``）。环境变量优先于 ``seven17.json``。

``WECATALOG_SCRAPE_SKIP_UNCATEGORIZED``（默认 ``true``）：仅爬取已在 ``wecatalog_tag_category_map.txt`` 中完成分类映射的商品，未映射的标签/分组下的商品将被跳过。设为 ``false`` / ``0`` 时也爬未映射商品（有分类的仍优先处理）。命令行 ``--skip-uncategorized`` 覆盖此配置。

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
from product_feed_kr.pf_time import now_cst8_iso
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from product_feed_kr.wecatalog_fetch_tags import (
    TAGS_PATH,
    _launch_browser,
    build_group_tree,
    tags_api_url,
)
from product_feed_kr.seven17_config import (
    EXIT_RESTART_FRESH_DATA,
    bool_env,
    getenv,
    reload_seven17_config,
    restart_after_n,
)
from product_feed_kr.wecatalog_tag_category_map_sync import (
    init_maps_at_scrape,
    sync_unmapped_tags_after_tags,
)
from product_feed_kr.wecatalog_popups import popups_response_ready
from product_feed_kr.wecatalog_scrape_fields import (
    attach_scrape_fields_to_record,
    fields_from_popups_response,
)
from product_feed_kr.wecatalog_tag_mapping import resolve_category_path
from product_feed_kr.pf_cli_loop import run_forever
from product_feed_kr.pf_log import configure_scrape_logging, pf_kv, pf_store_row_id_kv
from product_feed_kr.process_singleton import EXIT_SINGLETON_CONFLICT, single_instance_lock

# 与 `configure_scrape_logging` 默认 logger_name 一致；`-m` 运行时 __name__ 为 __main__，勿用 getLogger(__name__)。
logger = logging.getLogger("product_feed_kr.wecatalog_scrape_store")


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
  const r = await fetch(u, { credentials: "include" });
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


def iter_album_list_pages(
    page,
    album_id: str,
    *,
    max_pages: int,
    gap: InterRequestGap,
):
    """逐页请求 album/personal/all；每页 yield (本页新增去重 items, 原始响应, 当前页码 1-based)。"""
    seen: set[str] = set()
    ts: int | None = None
    pages = 0
    for _ in range(max_pages):
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
            gid = it.get("goods_id") or it.get("selfGoodsId") or it.get("id")
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
                    ("max_pages", max_pages),
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
    max_list_pages: int = 500,
    skip_detail: bool = False,
    skip_uncategorized: bool = False,
    auto_append_txt: bool = True,
    checkpoint_every: int = 0,
    max_records: int = 0,
    headed: bool = False,
) -> dict[str, Any]:
    from product_feed_kr.store_sqlite import (
        connect_sqlite,
        ensure_sqlite_schema,
        scrape_detail_ready,
        sqlite_checkpoint,
        sqlite_load_existing_goods_ids,
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
                ("auto_append_txt", auto_append_txt),
            ],
            zh="开始抓取微猫店铺：打开种子页并准备写 SQLite"
            + ("（跳过无分类商品）" if skip_uncategorized else "")
            + ("（txt 自动补全关闭）" if not auto_append_txt else ""),
        ),
    )

    stats = {
        "album_id": album_id,
        "list_pages": 0,
        "list_items_unique": 0,
        "popups_ok": 0,
        "popups_err": 0,
        "records": 0,
        "records_prior": 0,
        "records_new": 0,
        "skipped_existing": 0,
        "restart_fresh": False,
        "throttle_delay_sec_range": [delay_lo, delay_hi],
        "map_unmapped": 0,
        "map_txt_appended": 0,
        "path_ca_entries": None,
    }
    restart_after_new = restart_after_n("WECATALOG_SCRAPE_RESTART_AFTER_ITEMS", 1000)

    map_rows, path_ca_n = init_maps_at_scrape(logger)
    stats["path_ca_entries"] = path_ca_n
    if map_rows is not None:
        stats["map_rows"] = map_rows

    conn_db = None
    records: list[dict[str, Any]] = []
    skip_ids_prior: set[str] = set()
    try:
        conn_db = connect_sqlite()
        ensure_sqlite_schema(conn_db)
        skip_ids_prior, rows_prior = sqlite_load_existing_goods_ids(conn_db, album_id)
        logger.info(
            "%s",
            pf_kv(
                [
                    ("event", "scrape.sqlite_load"),
                    ("rows_prior", rows_prior),
                    ("skip_ids", len(skip_ids_prior)),
                    ("album_id", album_id),
                ],
                zh="已读库内已有 goods_id（不载入整行，仅用于跳过）",
            ),
        )

        stats["records_prior"] = rows_prior

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
            appended, unmapped = sync_unmapped_tags_after_tags(groups, logger, auto_append=auto_append_txt)
            stats["map_unmapped"] = len(unmapped)
            stats["map_txt_appended"] = appended

            logger.info(
                "%s",
                pf_kv(
                    [("event", "scrape.list.begin"), ("max_list_pages", max_list_pages)],
                    zh="开始按页遍历店铺商品列表",
                ),
            )
            seen_popups: dict[str, dict[str, Any] | None] = {}
            popups_fetch_index = 0

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
                if ready:
                    ck_kv.extend(pf_store_row_id_kv(ready[-1], album_id=album_id))
                logger.info(
                    "%s",
                    pf_kv(
                        ck_kv,
                        zh="写库：店铺进度 + 本 run 已抓到 popUps 的商品"
                        if not meta_only
                        else "写库：店铺元信息（尚无新商品）",
                    ),
                )

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

            def append_product_row(
                it: dict[str, Any],
                gname: str,
                tname: str,
                tid_int: int,
                shop_path: tuple[str, ...] | None,
            ) -> bool:
                """写入一条分组×标签×商品；返回 False 表示已达 max_records 应停止整次抓取。"""
                nonlocal new_appended, popups_fetch_index
                gid = it.get("goods_id") or it.get("selfGoodsId") or ""
                if not isinstance(gid, str) or not gid:
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
                if gid in seen_popups:
                    cached_pop = seen_popups[gid]
                    if isinstance(cached_pop, dict) and popups_response_ready(cached_pop):
                        scrape_fields = fields_from_popups_response(cached_pop)
                        logger.debug(
                            "%s",
                            pf_kv(
                                [("event", "scrape.popups.cache"), ("goods_id", gid)],
                                zh="popUpsInfoV2：复用本 run 已拉过的缓存",
                            ),
                        )
                else:
                    gap.before(
                        f"popUpsInfoV2 #{popups_fetch_index + 1} goods_id={gid}",
                    )
                    popups_fetch_index += 1
                    logger.debug(
                        "%s",
                        pf_kv(
                            [
                                ("event", "scrape.popups.request"),
                                ("n", popups_fetch_index),
                                ("goods_id", gid),
                            ],
                            zh="正在请求 popUpsInfoV2",
                        ),
                    )
                    p_raw = page.evaluate(
                        FETCH_POPUPS_JS,
                        {"sellerAlbumId": album_id, "commodityId": gid},
                    )
                    if isinstance(p_raw, dict) and popups_response_ready(p_raw):
                        scrape_fields = fields_from_popups_response(p_raw)
                        seen_popups[gid] = p_raw
                        stats["popups_ok"] += 1
                    else:
                        seen_popups[gid] = p_raw if isinstance(p_raw, dict) else {"error": str(p_raw)}
                        stats["popups_err"] += 1
                        logger.warning(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.popups.err"),
                                    ("goods_id", gid),
                                    (
                                        "errcode",
                                        p_raw.get("errcode") if isinstance(p_raw, dict) else None
                                    ),
                                ],
                                zh="popUpsInfoV2 失败或登录过期",
                            ),
                        )

                merged = scrape_fields if scrape_fields is not None else fields_from_popups_response(None)
                if not str(merged.get("commodity_title") or "").strip():
                    logger.warning(
                        "%s",
                        pf_kv(
                            [("event", "scrape.no_title"), ("goods_id", gid)],
                            zh="无商品标题，跳过入库（popUps 未返回 title）",
                        ),
                    )
                    return True
                rec = {
                    "wecatalog_group": gname,
                    "wecatalog_tag": tname,
                    "tag_id": tid_int,
                    "shop_category_path": list(shop_path) if shop_path is not None else None,
                    "goods_id": gid,
                    "goods_url": goods_page_url(album_id, gid),
                    "uploaded_to_platform": False,
                }
                attach_scrape_fields_to_record(rec, merged)
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
            ):
                stats["list_pages"] = page_num
                stats["list_items_unique"] += len(new_items)

                def _iter_groups_tags(*, categorized_only: bool):
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
                        ("skipped_uncategorized", skipped_uncategorized),
                        ("skip_uncategorized_enabled", skip_uncategorized),
                        ("popups_ok", stats["popups_ok"]),
                        ("popups_err", stats["popups_err"]),
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
    ap = argparse.ArgumentParser(description="wecatalog 店铺分类遍历 + popUpsInfoV2 → 本地 SQLite")
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
    ap.add_argument("--max-list-pages", type=int, default=500, help="列表分页上限（每页约 32 条）")
    ap.add_argument(
        "--skip-detail",
        action="store_true",
        help="只拉列表匹配分类，不请求 popUpsInfoV2",
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
        log_file = Path(__file__).resolve().parent.parent / "data" / "wecatalog_scrape_store.log"
    configure_scrape_logging(log_file, verbose=args.verbose)

    skip_uncat = args.skip_uncategorized if args.skip_uncategorized is not None else bool_env("WECATALOG_SCRAPE_SKIP_UNCATEGORIZED", True)
    auto_append = bool_env("WECATALOG_AUTO_APPEND_TXT", False)

    def _run_once() -> int:
        try:
            stats = scrape_store(
                args.store_url.strip(),
                trans_lang=args.trans_lang.strip() or "zh",
                detail_delay_range=(d_lo, d_hi),
                max_list_pages=max(1, args.max_list_pages),
                skip_detail=args.skip_detail,
                skip_uncategorized=skip_uncat,
                auto_append_txt=auto_append,
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
