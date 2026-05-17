"""微猫 wecatalog 店铺：先拉 **线上分类树**，再按其顺序遍历分组→标签拉商品详情，写入 **本地 SQLite**（``data/product_feed.db`` 或 ``PRODUCT_FEED_SQLITE``）。

1. **`commodity/tags`**：得到分组顺序与每个叶子 `tagId` / `tagName`，遍历顺序与相册后台一致。
2. **`album/personal/all`**：分页拉全店列表；**每拉一页即按分类树匹配并（按需）拉详情**，不先攒齐全部列表再处理。
3. **`wecatalog_tag_category_map.json`**：仅把 `(分组名, 标签名)` 映射为独立站 **`shop_category_path`**；
   未配置时该字段为 `null`，不影响爬取与遍历。

每条 **`pf_store_item`** 写入 **`detail_response`**（详情接口整包），**不写**列表卡片对象 `list_item`。
每条另有 **`uploaded_to_platform`**（布尔）：抓取时默认为 **`false`**。

增量：库内已有 **`goods_id`** 本次跳过（不请求详情、不重复插入）。抓取与上架对同一 ``.db`` 使用 **文件锁**（``*.db.lock``）互斥写。

列表：`POST .../album/personal/all`，分页用 `result.pagination.pageTimestamp` → 下一页 `timestamp`。

详情：`GET .../commodity/view?targetAlbumId=...&itemId=...`（浏览器会话内 fetch）。

**节流：** `--detail-delay` 作用于任意相邻两次微猫 API（首次请求不休眠）。写单个数如 ``5`` 表示固定 5 秒；写区间如 ``3,8`` 或 ``3:8`` 表示每次在闭区间 **[A,B]** 内均匀随机秒数再休眠。详情命中本进程缓存则不发起请求。

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
from datetime import datetime, timezone
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
    getenv,
    reload_seven17_config,
    restart_after_n,
)
from product_feed_kr.wecatalog_tag_mapping import resolve_category_path
from product_feed_kr.pf_log import configure_scrape_logging, pf_kv
from product_feed_kr.process_singleton import single_instance_lock

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

FETCH_DETAIL_JS = """
async ({ albumId, itemId }) => {
  const qs = new URLSearchParams({ targetAlbumId: albumId, itemId: itemId });
  const u = "https://www.wecatalog.cn/commodity/view?" + qs.toString();
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
    checkpoint_every: int = 0,
    max_records: int = 0,
    headed: bool = False,
) -> dict[str, Any]:
    from product_feed_kr.store_sqlite import (
        connect_sqlite,
        ensure_sqlite_schema,
        sqlite_checkpoint,
        sqlite_load_existing_products,
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
            ],
            zh="开始抓取微猫店铺：打开种子页并准备写 SQLite",
        ),
    )

    stats = {
        "album_id": album_id,
        "list_pages": 0,
        "list_items_unique": 0,
        "detail_ok": 0,
        "detail_err": 0,
        "records": 0,
        "records_prior": 0,
        "records_new": 0,
        "skipped_existing": 0,
        "restart_fresh": False,
        "throttle_delay_sec_range": [delay_lo, delay_hi],
    }
    restart_after_new = restart_after_n("WECATALOG_SCRAPE_RESTART_AFTER_ITEMS", 1000)

    conn_db = None
    records: list[dict[str, Any]] = []
    skip_ids_prior: set[str] = set()
    try:
        conn_db = connect_sqlite()
        ensure_sqlite_schema(conn_db)
        records, skip_ids_prior = sqlite_load_existing_products(conn_db, album_id)
        logger.info(
            "%s",
            pf_kv(
                [
                    ("event", "scrape.sqlite_load"),
                    ("records", len(records)),
                    ("skip_ids", len(skip_ids_prior)),
                    ("album_id", album_id),
                ],
                zh="已从 SQLite 载入本店已有商品，用于跳过已抓 goods_id",
            ),
        )

        stats["records_prior"] = len(records)

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
                    [("event", "scrape.list.begin"), ("max_list_pages", max_list_pages)],
                    zh="开始按页遍历店铺商品列表",
                ),
            )
            seen_detail: dict[str, dict[str, Any] | None] = {}
            detail_fetch_index = 0

            def write_checkpoint() -> None:
                meta_extra = {
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                    "stats": stats,
                }
                assert conn_db is not None
                sqlite_checkpoint(
                    conn_db,
                    album_id,
                    store_url=seed,
                    trans_lang=trans_lang,
                    detail_delay_sec=delay_lo,
                    skip_detail=skip_detail,
                    meta_extra=meta_extra,
                    records=records,
                )
                logger.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "scrape.checkpoint"),
                            ("products", len(records)),
                            ("album_id", album_id),
                        ],
                        zh="已写入 SQLite checkpoint（店铺信息+当前商品快照）",
                    ),
                )

            # 进入详情循环前先落库一次，避免长时间抓取中途崩溃时库内无快照
            write_checkpoint()
            logger.info(
                "%s",
                pf_kv(
                    [("event", "scrape.checkpoint.initial"), ("album_id", album_id)],
                    zh="进入详情循环前已落库首份快照",
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
                nonlocal new_appended, detail_fetch_index
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
                                zh="跳过库内已有 goods_id 的计数提示",
                            ),
                        )
                    return True

                detail_payload: dict[str, Any] | None = None
                if not skip_detail:
                    if gid in seen_detail:
                        detail_payload = seen_detail[gid]
                        logger.debug(
                            "%s",
                            pf_kv(
                                [("event", "scrape.detail.cache"), ("goods_id", gid[:24])],
                                zh="详情接口：复用本 run 已拉过的缓存",
                            ),
                        )
                    else:
                        gap.before(f"commodity/view 详情 #{detail_fetch_index + 1} goods_id={gid[:36]}")
                        detail_fetch_index += 1
                        logger.debug(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "scrape.detail.request"),
                                    ("n", detail_fetch_index),
                                    ("goods_id", gid[:36]),
                                ],
                                zh="正在请求单条商品详情 commodity/view",
                            ),
                        )
                        d_raw = page.evaluate(FETCH_DETAIL_JS, {"albumId": album_id, "itemId": gid})
                        if isinstance(d_raw, dict) and d_raw.get("errcode") in (0, None):
                            detail_payload = d_raw
                            stats["detail_ok"] += 1
                        else:
                            detail_payload = d_raw if isinstance(d_raw, dict) else {"error": str(d_raw)}
                            stats["detail_err"] += 1
                            logger.warning(
                                "%s",
                                pf_kv(
                                    [
                                        ("event", "scrape.detail.err"),
                                        ("goods_id", gid[:32]),
                                        (
                                            "errcode",
                                            d_raw.get("errcode") if isinstance(d_raw, dict) else None,
                                        ),
                                    ],
                                    zh="单条详情接口失败或 errcode 异常",
                                ),
                            )
                        seen_detail[gid] = detail_payload

                rec = {
                    "wecatalog_group": gname,
                    "wecatalog_tag": tname,
                    "tag_id": tid_int,
                    "shop_category_path": list(shop_path) if shop_path is not None else None,
                    "goods_id": gid,
                    "goods_url": goods_page_url(album_id, gid),
                    "uploaded_to_platform": False,
                    "detail_response": detail_payload,
                }
                records.append(rec)
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
                                ("goods_id", gid[:28]),
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
            for new_items, _raw_pg, page_num in iter_album_list_pages(
                page,
                album_id,
                max_pages=max_list_pages,
                gap=gap,
            ):
                stats["list_pages"] = page_num
                stats["list_items_unique"] += len(new_items)

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
                        for it in new_items:
                            if tid_int not in _tag_ids_on_item(it):
                                continue
                            if not append_product_row(it, gname, tname, tid_int, shop_path):
                                stop_run = True
                                break

                logger.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "scrape.list.page_done"),
                            ("page", page_num),
                            ("page_items", len(new_items)),
                            ("list_unique", stats["list_items_unique"]),
                            ("records", len(records)),
                        ],
                        zh="本列表页处理完并已 checkpoint",
                    ),
                )
                write_checkpoint()
                if stop_run:
                    break

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
                        ("detail_ok", stats["detail_ok"]),
                        ("detail_err", stats["detail_err"]),
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
    ap = argparse.ArgumentParser(description="wecatalog 店铺分类遍历 + 详情 → 本地 SQLite")
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
    ap.add_argument("--skip-detail", action="store_true", help="只拉列表匹配分类，不请求 commodity/view")
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
        "--log-file",
        type=Path,
        default=None,
        help="额外写入日志文件（UTF-8）；不设则仅 stderr",
    )
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

    configure_scrape_logging(args.log_file, verbose=args.verbose)

    try:
        with single_instance_lock("wecatalog_scrape_store"):
            stats = scrape_store(
                args.store_url.strip(),
                trans_lang=args.trans_lang.strip() or "zh",
                detail_delay_range=(d_lo, d_hi),
                max_list_pages=max(1, args.max_list_pages),
                skip_detail=args.skip_detail,
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
        logger.exception("%s", pf_kv([("event", "scrape.fatal"), ("err", str(e))], zh="抓取主流程未捕获异常，已中止"))
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
