"""将 **`wecatalog_scrape_store` 写入的本地 SQLite**（按 **`album_id`**）批量填入 seven17（Gnuboard5）
后台 **`itemform`** 并提交（Playwright）。不走 Excel。

凭据与分类可通过 **`config/seven17.json`**（复制 `config/seven17.example.json`）或环境变量提供，
环境变量优先。

必填：`SEVEN17_MB_ID`、`SEVEN17_MB_PASSWORD`。数据库：`PRODUCT_FEED_SQLITE`（默认 `data/product_feed.db`）。相册 ID：命令行 **`--album-id`**，或配置 / 环境变量 **`WECATALOG_ALBUM_ID`**（与店铺 URL 中 albumId 一致）。

后台分类 **`ca_id`**：微猫 (分组, 标签) → 韩文路径（``data/wecatalog_category_pairs.json``）→
``data/seven17_path_ca_map.json``。爬取阶段默认跳过无分类商品；分类仅走路径映射，不用 LLM 兜底。

常用可选：`SEVEN17_CONFIG`、`SEVEN17_BASE_URL`、`SEVEN17_HEADLESS`、`SEVEN17_STOCK_QTY`、
`SEVEN17_DEFAULT_PRICE`（仅非白名单货源无价格时兜底；白名单内无价上架不填 ``it_price``）、`SEVEN17_SC_TYPE`、`SEVEN17_MAX_IMAGES`、
`SEVEN17_NO_PRICE_ALLOW_CATEGORIES`（无价格允许上传白名单：匹配 map 分组/标签；无价时不换算、不填售价）、
`WEGO_TITLE_PREFIX`、`WEGO_DESC_TEMPLATE`、
`SEVEN17_CONVERT_CNY_TO_KRW`（默认 true：每条商品填表前拉汇率，把人民币售价换算为韩元填入 `it_price`）、
`SEVEN17_CNY_KRW_RATE`（可选固定汇率，填则不走接口）、`SEVEN17_CNY_KRW_FALLBACK`（接口失败时的 1 CNY 兑多少 KRW）、
`SEVEN17_FILL_IT_EXPLAN`（默认 false：不向后台填写 상품설명/모바일 상품설명；设为 true 恢复填写）、
`LISTING_LLM_RESTART_AFTER_ITEMS` / `SEVEN17_UPLOAD_RESTART_AFTER_ITEMS`（默认 1000：本 run 写回 LLM / 上架成功达 N 条后退出码 75，供外层立即重跑；0 关闭）、
`SEVEN17_UPLOAD_THREADS`（默认 1：上架工作线程数，每线程独立 Playwright 登录）。
`SEVEN17_UPLOAD_IMAGE_WORKERS`（默认 min(图片数, 8)：单条商品多图并行下载线程数）。
`SEVEN17_UPLOAD_IMAGE_PREFETCH_QUEUE`（默认 8，仅单线程上架：后台预取下 N 条商品图；0 关闭）。
检测到 ``login.php``（会话失效）时自动 ``login_admin`` 并重试当前商品一次。

上传模块不再调用 LLM：只读取 SQLite 已有的 ``listing_llm``（``name_ko`` 标题、``desc_ko``、``price_krw``、``attr_map_ko`` 等）。LLM 写回请用 ``python -m product_feed_kr.seven17.seven17_llm``（``02_LLM补全上架信息.bat``）。

真实上架：``SEVEN17_UPLOAD_THREADS`` >1 时同进程多线程（每线程独立 Playwright），每处理完一条后重新读库取下一条可上架记录。重复运行由进程锁拒绝（退出码 11）。

写库前对 ``.db`` 文件使用 ``filelock`` 独占锁（与抓取并行时互斥）。

运行示例：

  python -m product_feed_kr.seven17.seven17_upload --limit 1 --dry-run --keep-open
  python -m product_feed_kr.seven17.seven17_upload --album-id YOUR_ALBUM_ID --write-back --limit 5
  python -m product_feed_kr.seven17.seven17_upload --write-back
  python -m product_feed_kr.seven17.seven17_upload --write-back --once

默认进程内循环直至 Ctrl+C（单日志文件）；``--once`` 只跑一轮。``--preview-index`` 亦只跑一轮。

默认跳过 ``seven17_uploaded_at`` 非空（已上传平台）的记录。已开 LLM 且未完成 LLM 处理时跳过。

预览单条表单映射（不登录）::

  python -m product_feed_kr.seven17.seven17_upload --preview-index 0
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import queue
import re
import sys
import threading
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.request
from dataclasses import dataclass
from product_feed_kr.common.pf_time import now_cst8_iso
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from product_feed_kr.common.playwright_path import chromium_executable
from product_feed_kr.common.cny_krw_rate import cny_listing_amount_to_krw_won_str, fetch_krw_per_cny
from product_feed_kr.seven17.seven17_adm import login_admin
from product_feed_kr.common.seven17_config import bool_env as _cfg_bool
from product_feed_kr.common.seven17_config import getenv as _cfg_get
from product_feed_kr.common.seven17_config import getenv_required as _cfg_required
from product_feed_kr.common.seven17_config import (
    EXIT_RESTART_FRESH_DATA,
    reload_seven17_config,
    restart_after_n,
)
from product_feed_kr.seven17.seven17_category_llm import (
    category_map_suggest_message,
    load_seven17_ca_catalog,
    resolve_ca_id_for_store_record,
)
from product_feed_kr.wecatalog.wecatalog_tag_mapping import resolve_category_path, resolve_seven17_ca_id
from product_feed_kr.wego.wego_commodity import DEFAULT_WEGO_DESC_TEMPLATE, parse_price_str, parse_wego_product
from product_feed_kr.common.pf_log import (
    UPLOAD_LOGGER_NAMES,
    configure_pf_stderr,
    configure_upload_logging,
    log_item_separator,
    pf_goods_id,
    pf_kv,
    pf_store_row_id_kv,
)
from product_feed_kr.wego.wecatalog_store_record import commodity_from_wecatalog_record
from product_feed_kr.common.pf_cli_loop import default_log_path, run_forever
from product_feed_kr.common.process_singleton import EXIT_SINGLETON_CONFLICT, single_instance_lock

# 必须用稳定 logger 名：以 `python -m product_feed_kr.seven17.seven17_upload` 运行时 __name__ 为 __main__，
# getLogger(__name__) 会落到 __main__，configure_pf_stderr 挂在 product_feed_kr.seven17.seven17_upload 上则永远打不出。
_log = logging.getLogger("product_feed_kr.seven17.seven17_upload")
_upload_logging_configured = False

def _configure_upload_stderr_logging() -> None:
    """未在 main 中配置过文件日志时，仅挂 stderr（预览等子路径兜底）。"""
    global _upload_logging_configured
    if _upload_logging_configured:
        return
    configure_pf_stderr(*UPLOAD_LOGGER_NAMES)


def _init_upload_logging(*, log_file: Path | None = None, verbose: bool = False) -> None:
    global _upload_logging_configured
    configure_upload_logging(log_file=log_file, verbose=verbose)
    _upload_logging_configured = True


def _preview_text(s: str, max_len: int = 500) -> str:
    one = " ".join(str(s).split())
    return one if len(one) <= max_len else one[: max_len - 3] + "..."


def _fx_source_cn(code: str) -> str:
    """汇率来源标记 → 中文（ stderr 日志用）。"""
    return {
        "manual": "配置文件固定汇率 SEVEN17_CNY_KRW_RATE",
        "live": "实时接口",
        "fallback": "实时失败，兜底 SEVEN17_CNY_KRW_FALLBACK",
    }.get(code, code)


def _ms_since(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _upload_phase_log(
    event: str,
    zh: str,
    *,
    elapsed_ms: int | None = None,
    extra: list[tuple[str, Any]] | None = None,
    thread_label: str = "",
    rec: dict[str, Any] | None = None,
) -> None:
    """上架流程分阶段日志（带 ``elapsed_ms`` 便于排查卡顿）。"""
    kv: list[tuple[str, Any]] = [("event", event)]
    if thread_label:
        kv.append(("thread", thread_label))
    if rec is not None:
        kv.extend(pf_store_row_id_kv(rec))
    if elapsed_ms is not None:
        kv.append(("elapsed_ms", elapsed_ms))
    if extra:
        kv.extend(extra)
    _log.info("%s", pf_kv(kv, zh=zh))


_UPLOAD_SKIP_REASON_ZH: dict[str, str] = {
    "already_uploaded": "已上传",
    "duplicate_item": "重复不可处理",
    "no_detail": "无详情",
    "llm_not_processed": "未完成LLM",
    "llm_not_uploadable": "LLM未达上架条件",
    "listed_too_old": "微猫上架时间早于配置阈值",
    "no_category": "无分类",
    "no_images": "无图片",
    "video_media": "资源为视频",
}


def _login_admin_with_log(
    page: Any,
    *,
    base: str,
    mb_id: str,
    mb_password: str,
    redirect_full_url: str,
    thread_label: str = "",
) -> None:
    """登录后台并打阶段耗时日志。"""
    t0 = time.perf_counter()
    _upload_phase_log(
        "upload.login.begin",
        "后台登录：打开登录页并提交",
        thread_label=thread_label,
        extra=[("redirect", _preview_text(redirect_full_url, 96))],
    )
    login_admin(
        page,
        base=base,
        mb_id=mb_id,
        mb_password=mb_password,
        redirect_full_url=redirect_full_url,
    )
    _upload_phase_log(
        "upload.login.done",
        "后台登录完成（页面已 load）",
        elapsed_ms=_ms_since(t0),
        thread_label=thread_label,
        extra=[("page_url", _preview_text(page.url, 120))],
    )


def _resolve_krw_per_cny() -> tuple[float, str]:
    """1 CNY 兑多少 KRW；优先配置固定汇率，其次实时接口，失败则用 FALLBACK。"""
    manual = _cfg_get("SEVEN17_CNY_KRW_RATE")
    if manual and str(manual).strip():
        try:
            v = float(str(manual).strip().replace(",", ""))
        except ValueError as e:
            raise RuntimeError(f"SEVEN17_CNY_KRW_RATE 无效：{manual!r}") from e
        if v <= 0:
            raise RuntimeError("SEVEN17_CNY_KRW_RATE 须为正数")
        return v, "manual"
    try:
        return fetch_krw_per_cny(), "live"
    except Exception as e:
        fb = _cfg_get("SEVEN17_CNY_KRW_FALLBACK")
        if fb and str(fb).strip():
            try:
                v = float(str(fb).strip().replace(",", ""))
            except ValueError as e2:
                raise RuntimeError(f"SEVEN17_CNY_KRW_FALLBACK 无效：{fb!r}") from e2
            if v <= 0:
                raise RuntimeError("SEVEN17_CNY_KRW_FALLBACK 须为正数")
            _log.warning(
                "%s",
                pf_kv(
                    [("event", "fx.warn"), ("reason", "live_failed_use_fallback"), ("err", str(e))],
                    zh="实时 CNY→KRW 失败，已改用配置里的兜底汇率",
                ),
            )
            return v, "fallback"
        raise RuntimeError(
            "CNY→KRW 实时汇率不可用：请在 config 中设置 SEVEN17_CNY_KRW_FALLBACK（例如 \"200\"）"
            f"或 SEVEN17_CNY_KRW_RATE 固定汇率。详情：{e}",
        ) from e


def _wait_keep_open_browser() -> None:
    """关闭浏览器前暂停，便于查看当前页面。"""
    _configure_upload_stderr_logging()
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "keep_open"),
                ("hint", "浏览器保持打开；回到终端按 Enter 后关闭并退出"),
            ],
            zh="有界面模式调试：等待回车后关闭浏览器",
        ),
    )
    try:
        input()
    except EOFError:
        pass


def _stdout_utf8() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _env_required(name: str) -> str:
    return _cfg_required(name)


def _page_is_login(page: Any) -> bool:
    return "login.php" in (getattr(page, "url", None) or "")


def _upload_fail_is_session_expired(fail_reason: str | None, page: Any) -> bool:
    if fail_reason and "会话失效" in fail_reason:
        return True
    return _page_is_login(page)


def _relogin_upload_session(
    page: Any,
    ctx: UploadRunContext,
    *,
    thread_label: str = "",
    rec: dict[str, Any] | None = None,
) -> bool:
    """会话失效时重新登录；成功返回 True，仍停留在 login.php 返回 False。"""
    th_kv = [("thread", thread_label)] if thread_label else []
    row_kv = pf_store_row_id_kv(rec) if rec else []
    _log.warning(
        "%s",
        pf_kv(
            [
                *th_kv,
                ("event", "upload.session.relogin"),
                *row_kv,
                ("url", page.url),
            ],
            zh="检测到登录页，自动重新登录后台",
        ),
    )
    _login_admin_with_log(
        page,
        base=ctx.seven17_base,
        mb_id=ctx.mb_id,
        mb_password=ctx.mb_password,
        redirect_full_url=ctx.itemform_url,
        thread_label=thread_label,
    )
    if _page_is_login(page):
        _log.error(
            "%s",
            pf_kv(
                [
                    *th_kv,
                    ("event", "upload.session.relogin_fail"),
                    ("url", page.url),
                ],
                zh="重新登录失败，仍停留在登录页",
            ),
        )
        return False
    _log.info(
        "%s",
        pf_kv(
            [
                *th_kv,
                *row_kv,
                ("event", "upload.session.relogin_ok"),
            ],
            zh="重新登录成功，将重试当前商品",
        ),
    )
    return True


def _classify_after_submit(page, dialogs: list[str]) -> tuple[bool, str | None, str | None]:
    """根据提交后跳转 URL（及 alert）判断是否成功。

    그누보드5 常见成功形态：`itemform.php?w=u&it_id=...`（进入编辑刚保存的商品），
    或跳转到 `itemlist.php`。

    返回：(是否成功, 商品 it_id 若可解析, 失败原因简述)。
    """
    if dialogs:
        return False, None, "; ".join(dialogs)
    url = page.url
    if "login.php" in url:
        return False, None, "会话失效，回到登录页"

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    path_l = (parsed.path or "").lower()

    if "itemlist.php" in path_l:
        return True, None, None

    if "itemform.php" in path_l:
        w = (qs.get("w") or [None])[0]
        it_id = (qs.get("it_id") or [None])[0]
        if w == "u":
            if it_id:
                return True, it_id, None
            return False, None, "返回编辑模式但未解析到 it_id，请打开 headed 模式核对"

        if not w or w == "":
            return (
                False,
                None,
                "仍在商品新建页，可能有不满足的必填项或校验错误（建议 SEVEN17_HEADLESS=0 查看页面）",
            )

    return False, None, f"未识别的跳转地址: {url}"


def shop_admin_dialog_handler(dialogs: list[str]):
    """处理后台常见确认框：保存成功后「계속 입력하시겠습니까?」应选取消，才会跳到带 it_id 的编辑页。"""

    def _on_dialog(d) -> None:
        msg = d.message
        if "계속 입력" in msg:
            d.dismiss()
            return
        dialogs.append(msg)
        d.accept()

    return _on_dialog


def click_itemform_submit(page) -> None:
    """点击商品表单提交按钮（兼容需滚动方可点击的主题）。"""
    selectors = (
        "#btn_submit",
        'form[name="fitemform"] input[type="submit"].btn_submit',
        'form[name="fitemform"] input[type="submit"]',
    )
    last: Exception | None = None
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=30_000)
            loc.scroll_into_view_if_needed()
            loc.click(timeout=60_000)
            return
        except Exception as e:
            last = e
    raise RuntimeError(f"无法点击商品表单提交按钮（已尝试 {selectors}）：{last}")


def _download_image(url: str) -> Path:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://www.szwego.com/",
        },
        method="GET",
    )
    suffix = ".jpg"
    low = url.lower().split("?")[0]
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if low.endswith(ext):
            suffix = ext if ext != ".jpeg" else ".jpg"
            break
    dest: Path | None = None
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            if "image/" in ctype:
                if "image/png" in ctype:
                    suffix = ".png"
                elif "image/webp" in ctype:
                    suffix = ".webp"
                elif "image/gif" in ctype:
                    suffix = ".gif"
                elif "image/jpeg" in ctype or "image/jpg" in ctype:
                    suffix = ".jpg"
            elif ctype.startswith("video/"):
                raise RuntimeError(f"下载内容为视频而非图片: ctype={ctype} url={url}")
            # 防空图/防错误页面：必须有足够字节且签名像图片。
            if len(data) < 512:
                raise RuntimeError(f"图片下载体积异常（过小）: bytes={len(data)} url={url}")
            head = data[:16]
            is_jpg = head.startswith(b"\xFF\xD8\xFF")
            is_png = head.startswith(b"\x89PNG\r\n\x1a\n")
            is_gif = head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
            is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
            if not (is_jpg or is_png or is_gif or is_webp):
                raise RuntimeError(f"下载内容不是可识别图片: ctype={ctype or '-'} url={url}")
            fd, path = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            dest = Path(path)
            dest.write_bytes(data)
    except (urllib.error.URLError, OSError, RuntimeError):
        if dest is not None:
            dest.unlink(missing_ok=True)
        raise
    if dest is None:
        raise RuntimeError(f"图片下载失败：未生成本地文件 url={url}")
    return dest


def upload_image_download_workers(url_count: int) -> int:
    """单条商品多图并行下载的线程数（至少 1，最多 16）。"""
    n_urls = max(1, int(url_count))
    raw = _cfg_get("SEVEN17_UPLOAD_IMAGE_WORKERS")
    if raw is not None and str(raw).strip():
        try:
            n = int(str(raw).strip())
            return max(1, min(n, 16))
        except ValueError:
            pass
    return max(1, min(n_urls, 8))


def _download_images_for_upload(
    urls: list[str],
    *,
    thread_label: str = "",
    rec: dict[str, Any] | None = None,
) -> list[Path]:
    """并行下载商品图，保持与 ``urls`` 相同顺序（``it_img1`` 对应第一张）。"""
    if not urls:
        return []

    if len(urls) == 1:
        return [_download_image(urls[0])]

    workers = upload_image_download_workers(len(urls))
    results: list[Path | None] = [None] * len(urls)
    first_err: BaseException | None = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fut_map = {pool.submit(_download_image, u): i for i, u in enumerate(urls)}
        for fut in as_completed(fut_map):
            idx = fut_map[fut]
            try:
                results[idx] = fut.result()
            except BaseException as e:
                first_err = e
                break

    done_paths = [p for p in results if p is not None]
    if first_err is not None:
        for pth in done_paths:
            pth.unlink(missing_ok=True)
        if isinstance(first_err, (urllib.error.URLError, OSError)):
            raise first_err
        raise RuntimeError(str(first_err)) from first_err

    if len(done_paths) != len(urls):
        for pth in done_paths:
            pth.unlink(missing_ok=True)
        raise RuntimeError(f"图片下载未完成：期望 {len(urls)} 张，实际 {len(done_paths)} 张")

    return [p for p in results if p is not None]


def upload_image_prefetch_queue_size() -> int:
    """预取队列容量（0=关闭）。仅 ``SEVEN17_UPLOAD_THREADS=1`` 时生效。"""
    raw = _cfg_get("SEVEN17_UPLOAD_IMAGE_PREFETCH_QUEUE")
    if raw is not None and str(raw).strip():
        try:
            return max(0, min(int(str(raw).strip()), 32))
        except ValueError:
            pass
    return 8


@dataclass
class _PrefetchedImageBatch:
    row_key: tuple[str, int]
    paths: list[Path]
    error: str | None = None


class UploadImagePrefetchQueue:
    """单上架线程：后台维护最多 ``queue_size`` 条商品的已下载图片，填表时优先命中。"""

    def __init__(
        self,
        album_id: str,
        ctx: UploadRunContext,
        *,
        queue_size: int,
        thread_label: str = "prefetch",
    ) -> None:
        self._album_id = album_id
        self._ctx = ctx
        self._queue_size = max(1, int(queue_size))
        self._thread_label = thread_label
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._ready: dict[tuple[str, int], _PrefetchedImageBatch] = {}
        self._in_progress: set[tuple[str, int]] = set()
        self._active: set[tuple[str, int]] = set()
        self._session_skipped_ref: set[tuple[str, int]] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker,
            name="seven17-upload-image-prefetch",
            daemon=True,
        )

    def attach_session_skipped(self, session_skipped: set[tuple[str, int]]) -> None:
        self._session_skipped_ref = session_skipped

    def start(self) -> None:
        self._thread.start()
        _upload_phase_log(
            "upload.prefetch.start",
            "图片预取队列已启动",
            thread_label=self._thread_label,
            extra=[("queue_size", self._queue_size)],
        )

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=60.0)
        self._discard_all_ready()
        _upload_phase_log(
            "upload.prefetch.stop",
            "图片预取队列已停止",
            thread_label=self._thread_label,
        )

    def _discard_all_ready(self) -> None:
        with self._lock:
            batches = list(self._ready.values())
            self._ready.clear()
        for batch in batches:
            for pth in batch.paths:
                pth.unlink(missing_ok=True)

    def _occupied_keys(self) -> set[tuple[str, int]]:
        return set(self._ready) | set(self._in_progress) | set(self._active)

    def _pick_next_prefetch(
        self,
        conn: Any,
    ) -> tuple[dict[str, Any], list[str], tuple[str, int]] | None:
        from product_feed_kr.db.store_sqlite import sqlite_load_products_for_upload

        skipped = self._session_skipped_ref or set()
        occupied = self._occupied_keys()
        items = sqlite_load_products_for_upload(
            conn,
            self._album_id,
            skip_uploaded=self._ctx.skip_uploaded,
        )
        for rec in items:
            if not isinstance(rec, dict):
                continue
            key = _upload_row_key(rec)
            if key in occupied or key in skipped:
                continue
            reason = _upload_skip_reason(
                rec,
                skip_uploaded=self._ctx.skip_uploaded,
                llm_on=self._ctx.llm_on,
                default_price=self._ctx.default_price,
            )
            if reason is not None:
                continue
            com = commodity_from_wecatalog_record(rec)
            if com is None:
                continue
            try:
                prod = parse_wego_product(com, default_price_if_missing=self._ctx.default_price)
            except ValueError:
                continue
            urls = list(prod["image_urls"][: self._ctx.max_img])
            if not urls:
                continue
            return rec, urls, key
        return None

    def _worker(self) -> None:
        from product_feed_kr.db.store_sqlite import connect_sqlite, ensure_sqlite_schema

        conn = connect_sqlite()
        try:
            ensure_sqlite_schema(conn)
            while not self._stop.is_set():
                with self._cond:
                    if len(self._ready) >= self._queue_size:
                        self._cond.wait(timeout=1.0)
                        if self._stop.is_set():
                            break
                        continue
                picked = self._pick_next_prefetch(conn)
                if picked is None:
                    time.sleep(0.4)
                    continue
                rec, urls, key = picked
                with self._cond:
                    if (
                        key in self._ready
                        or key in self._in_progress
                        or key in self._active
                    ):
                        continue
                    if self._session_skipped_ref and key in self._session_skipped_ref:
                        continue
                    if len(self._ready) >= self._queue_size:
                        continue
                    self._in_progress.add(key)
                batch: _PrefetchedImageBatch
                try:
                    paths = _download_images_for_upload(
                        urls,
                        thread_label=self._thread_label,
                        rec=rec,
                    )
                    batch = _PrefetchedImageBatch(key, paths)
                except (urllib.error.URLError, OSError, RuntimeError) as e:
                    batch = _PrefetchedImageBatch(key, [], error=str(e))
                    _log.warning(
                        "%s",
                        pf_kv(
                            [
                                ("event", "upload.prefetch.fail"),
                                *pf_store_row_id_kv(rec),
                                ("error", str(e)[:200]),
                            ],
                            zh="预取图片失败，上架时将重试下载",
                        ),
                    )
                with self._cond:
                    self._in_progress.discard(key)
                    if key in self._active:
                        for pth in batch.paths:
                            pth.unlink(missing_ok=True)
                    elif len(self._ready) < self._queue_size:
                        self._ready[key] = batch
                    else:
                        for pth in batch.paths:
                            pth.unlink(missing_ok=True)
                    self._cond.notify_all()
        finally:
            conn.close()

    def acquire_images(
        self,
        rec: dict[str, Any],
        urls: list[str],
        *,
        thread_label: str = "",
        wait_timeout_sec: float = 120.0,
    ) -> tuple[list[Path], str]:
        """取图片路径；``source`` 为 ``prefetch`` 或 ``sync``。"""
        key = _upload_row_key(rec)
        wait_t0 = time.perf_counter()
        with self._cond:
            self._active.add(key)
            batch: _PrefetchedImageBatch | None = None
            while True:
                batch = self._ready.pop(key, None)
                if batch is not None:
                    break
                if key not in self._in_progress:
                    break
                remaining = wait_timeout_sec - (time.perf_counter() - wait_t0)
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(2.0, remaining))
        if batch is not None and batch.paths and not batch.error:
            return list(batch.paths), "prefetch"
        paths = _download_images_for_upload(urls, thread_label=thread_label, rec=rec)
        return paths, "sync"

    def release_record(self, rec: dict[str, Any]) -> None:
        key = _upload_row_key(rec)
        with self._cond:
            self._active.discard(key)
            self._cond.notify_all()


def _bool_env(name: str, default: bool) -> bool:
    return _cfg_bool(name, default)


def _ca_id_log_paths(wecatalog_group: str, wecatalog_tag: str) -> tuple[str, str]:
    """ca_id 日志用：韩文商城层级（map path）与中文侧常见的「分组 > 标签」。"""
    seg = resolve_category_path(wecatalog_group, wecatalog_tag)
    ko = " > ".join(seg) if seg else "—"
    parts = [x for x in (wecatalog_group.strip(), wecatalog_tag.strip()) if x]
    zh = " > ".join(parts) if parts else "—"
    return ko, zh


def _krw_price_str_usable(raw: Any) -> bool:
    s = str(raw or "").strip().replace(",", "")
    if not s or s in ("0", "0.0", "-1") or s.lower() == "null":
        return False
    try:
        return float(s) > 0
    except ValueError:
        return False


def _price_cny_usable(rec: dict[str, Any]) -> str:
    """从 ``price_cny`` 提取可用人民币价格；不可用返回空串。"""
    raw = parse_price_str(rec.get("price_cny"), "")
    return "" if raw in ("", "0", "0.0") else raw


def _record_price_krw_usable(rec: dict[str, Any]) -> str:
    """库内已有韩元售价（``price_krw`` 列或 ``commodity_min``）。"""
    from product_feed_kr.wego.wecatalog_store_record import record_price_krw

    s = record_price_krw(rec)
    return s if s and _krw_price_str_usable(s) else ""


def _upload_db_write_needed(price_src: str) -> bool:
    """``SEVEN17_WRITE_BACK_AFTER_LLM``：仅当韩元价非「直接沿用库内 price_krw」时写库。"""
    return price_src not in ("price_krw", "whitelist_no_price")


def _upload_skip_price_fill(rec: dict[str, Any], listing_price: str) -> bool:
    """无价白名单且当前无有效韩元售价：上架不换算、不填 ``it_price``。"""
    from product_feed_kr.listing.listing_llm_enrich import record_is_no_price_allowed_by_map_category

    return record_is_no_price_allowed_by_map_category(rec) and not _krw_price_str_usable(listing_price)


def _resolve_upload_listing_price_krw(
    rec: dict[str, Any],
    *,
    llm_on: bool,
    llm_data: dict[str, Any],
    prod: dict[str, Any],
    krw_per_cny: float | None,
) -> tuple[str, str]:
    """返回 (上架韩元售价字符串, 来源标记)。"""
    krw_db = _record_price_krw_usable(rec)
    if krw_db:
        return krw_db, "price_krw"
    if llm_on:
        krw_llm = str(llm_data.get("price_krw") or "").strip().replace(",", "")
        if _krw_price_str_usable(krw_llm):
            return krw_llm, "listing_llm"
    cny_db = _price_cny_usable(rec)
    if cny_db and krw_per_cny is not None:
        return cny_listing_amount_to_krw_won_str(cny_db, krw_per_cny), "price_cny"
    if llm_on:
        cny_llm = str(llm_data.get("cny_price") or "").strip()
        if cny_llm and cny_llm not in ("0", "0.0") and krw_per_cny is not None:
            return cny_listing_amount_to_krw_won_str(cny_llm, krw_per_cny), "listing_llm_cny"
    cny_prod = str(prod.get("price") or "").strip()
    if cny_prod and cny_prod not in ("0", "0.0") and krw_per_cny is not None:
        return cny_listing_amount_to_krw_won_str(cny_prod, krw_per_cny), "title_parse"
    return "", "none"


def _upload_title_from_record(
    rec: dict[str, Any],
    prod: dict[str, Any],
    *,
    llm_on: bool,
    title_prefix: str,
) -> str | None:
    """上架标题：LLM 开启时优先 ``name_ko``，否则用原标题。"""
    llm_data = rec.get("listing_llm") if isinstance(rec.get("listing_llm"), dict) else {}
    if llm_on:
        base = (
            str(llm_data.get("name_ko") or "").strip()
            or str(prod.get("title") or "").strip()
        )
    else:
        base = str(prod.get("title") or "").strip()
    if not base:
        return None
    return f"{title_prefix}{base}" if title_prefix else base


def _ko_option_pairs_from_attr_map(attr_map_ko: dict[str, Any] | None) -> list[tuple[str, str]]:
    """从 attr_map_ko 提取商品选项（韩文键），返回 [(옵션명, '값1,값2,...')]，最多 3 组。"""
    if not isinstance(attr_map_ko, dict):
        return []
    out: list[tuple[str, str]] = []
    for k, raw_vals in attr_map_ko.items():
        subject = str(k or "").strip()
        if not subject:
            continue
        # 要求韩文：옵션名里至少有一个韩文字母。
        if not any("\uac00" <= ch <= "\ud7a3" for ch in subject):
            continue
        vals: list[str] = []
        if isinstance(raw_vals, list):
            for v in raw_vals:
                sv = str(v or "").strip()
                if sv:
                    vals.append(sv)
        elif raw_vals is not None:
            sv = str(raw_vals).strip()
            if sv:
                vals.append(sv)
        uniq: list[str] = []
        seen: set[str] = set()
        for v in vals:
            if v in seen:
                continue
            seen.add(v)
            uniq.append(v)
        if not uniq:
            continue
        out.append((subject, ",".join(uniq)))
        if len(out) >= 3:
            break
    return out


def itemform_preview_dict_from_store_record(
    record: dict[str, Any],
    *,
    default_price: str,
    stock_qty: str,
    sc_type: str,
    max_images: int,
    desc_template: str,
    title_prefix: str,
    convert_cny_to_krw: bool,
    krw_per_cny: float | None,
) -> dict[str, Any]:
    """根据一条店铺记录生成与 `_fill_itemform` 对应的字段预览（含说明）。"""
    com = commodity_from_wecatalog_record(record)
    if com is None:
        raise ValueError("该记录无商品标题/抓取字段，无法预览")

    prod = parse_wego_product(com, default_price_if_missing=default_price)

    llm_on = _cfg_bool("OPENAI_ENRICH_LISTING", True)
    upload_title = _upload_title_from_record(
        record,
        prod,
        llm_on=llm_on,
        title_prefix=title_prefix,
    )
    preview_title_fallback = upload_title is None
    if upload_title is None:
        upload_title = f"{title_prefix}{prod['title']}" if title_prefix else prod["title"]
    desc_html = _desc_html_for_store_record(
        desc_template.strip(),
        prod,
        record,
        upload_title=upload_title,
        default_tpl=DEFAULT_WEGO_DESC_TEMPLATE,
    )

    max_images = max(1, min(max_images, 10))
    urls = prod["image_urls"][:max_images]
    img_slots: dict[str, str] = {f"it_img{i}": urls[i - 1] for i in range(1, len(urls) + 1)}

    warnings: list[str] = []
    llm_data = record.get("listing_llm") if isinstance(record.get("listing_llm"), dict) else {}
    if preview_title_fallback and llm_on:
        if not str(llm_data.get("name_ko") or "").strip():
            warnings.append(
                "缺少 LLM name_ko：预览暂用原标题；请运行 02_LLM补全上架信息.bat",
            )
    if not urls:
        warnings.append("无 imgsSrc/imgs 可用 URL：上架时会因无主图跳过本条")

    gid_top = str(record.get("goods_id") or prod["goods_id"] or "").strip()
    gname = str(record.get("wecatalog_group") or "")
    tname = str(record.get("wecatalog_tag") or "")
    resolved_ca, ca_src = resolve_ca_id_for_store_record(record, commodity=com)
    effective_ca = (resolved_ca or "").strip() or "(未解析：请补全 map / 韩文路径匹配)"
    if not resolve_seven17_ca_id(gname, tname):
        hint = category_map_suggest_message(
            record,
            ca_id=(resolved_ca or "").strip(),
            source=ca_src if resolved_ca else "none",
        )
        if hint:
            warnings.append(hint)

    fx_rate = krw_per_cny if convert_cny_to_krw else None
    listing_price, _price_src = _resolve_upload_listing_price_krw(
        record,
        llm_on=llm_on,
        llm_data=llm_data,
        prod=prod,
        krw_per_cny=fx_rate,
    )

    return {
        "form_fields": {
            "ca_id": effective_ca,
            "it_name": upload_title,
            "it_price": listing_price,
            "it_stock_qty": stock_qty,
            "it_use": "1（上架销售）",
            "it_sc_type": sc_type,
            "it_explan": desc_html,
            **img_slots,
        },
        "form_fields_note": (
            "ca_id：韩文路径→path_ca_map → DB 缓存（无 LLM 兜底）。"
            "shop_category_path 为韩文展示路径。图片先下载再填入 it_img1～。"
        ),
        "seven17_ca_source": ca_src if resolved_ca else "none",
        "wecatalog_record_summary": {
            "goods_id": gid_top,
            "goods_url": record.get("goods_url"),
            "wecatalog_group": record.get("wecatalog_group"),
            "wecatalog_tag": record.get("wecatalog_tag"),
            "tag_id": record.get("tag_id"),
            "shop_category_path": record.get("shop_category_path"),
        },
        "parsed_from_commodity": {
            "title": prod["title"],
            "goods_num": prod["goods_num"],
            "tag_names": prod["tag_names"],
            "image_url_count": len(prod["image_urls"]),
            "price_input_cny": cny_src,
            "price_listing_krw": listing_price if convert_cny_to_krw and krw_per_cny is not None else None,
        },
        "fx_cny_krw": (
            {"krw_per_cny": krw_per_cny, "applied": convert_cny_to_krw and krw_per_cny is not None}
            if convert_cny_to_krw and krw_per_cny is not None
            else {"applied": False}
        ),
        "warnings": warnings,
    }


def print_store_sqlite_itemform_preview(album_id: str, *, index: int) -> dict[str, Any]:
    """从 SQLite 加载该相册商品行，取第 index 条，打印表单预览 JSON。"""
    from product_feed_kr.db.store_sqlite import connect_sqlite, ensure_sqlite_schema, sqlite_load_products_for_upload

    _configure_upload_stderr_logging()
    aid = album_id.strip()
    if not aid:
        raise ValueError("album_id 不能为空")
    conn = connect_sqlite()
    try:
        ensure_sqlite_schema(conn)
        items = sqlite_load_products_for_upload(conn, aid, skip_uploaded=False)
    finally:
        conn.close()
    if not items:
        raise ValueError("SQLite 中该 album 无商品行")
    if index < 0 or index >= len(items):
        raise IndexError(f"preview-index={index} 超出范围（共 {len(items)} 条）")

    default_price = _cfg_get("SEVEN17_DEFAULT_PRICE", "0") or "0"
    stock_qty = _cfg_get("SEVEN17_STOCK_QTY", "100") or "100"
    sc_type = _cfg_get("SEVEN17_SC_TYPE", "1") or "1"
    max_img = int(_cfg_get("SEVEN17_MAX_IMAGES", "10") or "10")
    title_prefix = (_cfg_get("WEGO_TITLE_PREFIX") or "").strip()
    desc_tpl = (_cfg_get("WEGO_DESC_TEMPLATE") or "").strip()
    if not desc_tpl:
        desc_tpl = DEFAULT_WEGO_DESC_TEMPLATE

    convert_fx = _bool_env("SEVEN17_CONVERT_CNY_TO_KRW", True)
    krw_pc: float | None = None
    fx_meta: dict[str, Any] = {"convert_cny_to_krw": convert_fx}
    if convert_fx:
        krw_pc, fx_src = _resolve_krw_per_cny()
        fx_meta["krw_per_cny"] = krw_pc
        fx_meta["source"] = fx_src
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "fx.preview"),
                    ("krw_per_cny", krw_pc),
                    ("source", _fx_source_cn(fx_src or "")),
                ],
                zh="预览：本条使用的韩元/人民币汇率",
            ),
        )

    inner = itemform_preview_dict_from_store_record(
        items[index],
        default_price=default_price,
        stock_qty=stock_qty,
        sc_type=sc_type,
        max_images=max_img,
        desc_template=desc_tpl,
        title_prefix=title_prefix,
        convert_cny_to_krw=convert_fx,
        krw_per_cny=krw_pc,
    )

    payload: dict[str, Any] = {
        "ok": True,
        "album_id": aid,
        "preview_index": index,
        "products_in_sqlite": len(items),
        "fx": fx_meta,
        **inner,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _log_itemform_field_preview(
    *,
    gid: str,
    wecatalog_group: str,
    wecatalog_tag: str,
    ca_id: str,
    ca_path_ko: str,
    ca_path_zh: str,
    title: str,
    price: str,
    stock_qty: str,
    sc_type: str,
    desc_html: str,
    img_slots: str,
) -> None:
    """stderr：单条 itemform 字段快照（写入浏览器前），便于 grep `event=itemform`。"""
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "itemform"),
                ("goods_id", gid),
                ("group", wecatalog_group),
                ("tag", wecatalog_tag),
                ("ca_id", ca_id),
                ("path_ko", ca_path_ko),
                ("path_zh", ca_path_zh),
                ("it_price", price),
                ("it_stock_qty", stock_qty),
                ("it_sc_type", sc_type),
                ("it_use", "1"),
                ("title_len", len(title)),
                ("title", _preview_text(title, 220)),
                ("desc_len", len(desc_html)),
                ("desc", _preview_text(desc_html, 240)),
                ("imgs", img_slots),
            ],
            zh="即将写入后台商品表单的字段摘要",
        ),
    )


def _fill_itemform(
    page,
    *,
    goods_id: str = "",
    wecatalog_group: str = "",
    wecatalog_tag: str = "",
    ca_id: str,
    title: str,
    price: str,
    stock_qty: str,
    desc_html: str,
    image_path: Path | None = None,
    image_paths: list[Path] | None = None,
    sc_type: str,
    option_attr_map_ko: dict[str, Any] | None = None,
    ca_path_ko: str = "—",
    ca_path_zh: str = "—",
    price_cny_for_log: str | None = None,
    fx_krw_per_cny: float | None = None,
    price_src: str = "",
    thread_label: str = "",
) -> None:
    # 图片字段最多 it_img1~it_img10：这里统一裁剪，避免后续循环里越界或误传。
    fill_t0 = time.perf_counter()
    paths: list[Path] = []
    if image_paths:
        paths = [p for p in image_paths if p is not None][:10]
    elif image_path is not None:
        paths = [image_path]

    _configure_upload_stderr_logging()
    gid = (goods_id or "").strip() or "-"
    img_slots = ", ".join(f"it_img{i}={p.name}" for i, p in enumerate(paths, start=1)) if paths else "（无主图文件）"
    if (
        price_src in ("price_cny", "listing_llm_cny", "title_parse")
        and fx_krw_per_cny is not None
        and price_cny_for_log
    ):
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "fx.itemform"),
                    ("krw_per_cny", fx_krw_per_cny),
                    ("src_cny", price_cny_for_log),
                    ("it_price", price),
                ],
                zh="本条售价：人民币源价按汇率换算为韩元填报价",
            ),
        )
    # 说明字段是否写入（默认关闭，避免误覆盖历史运营文案）。
    fill_it_explan = _bool_env("SEVEN17_FILL_IT_EXPLAN", False)

    _log_itemform_field_preview(
        gid=gid,
        wecatalog_group=wecatalog_group,
        wecatalog_tag=wecatalog_tag,
        ca_id=ca_id,
        ca_path_ko=ca_path_ko,
        ca_path_zh=ca_path_zh,
        title=title,
        price=price,
        stock_qty=stock_qty,
        sc_type=sc_type,
        desc_html=desc_html,
        img_slots=img_slots,
    )
    if not fill_it_explan:
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "itemform.note"),
                    ("it_explan", "skip"),
                    ("it_mobile_explan", "skip"),
                    ("need", "SEVEN17_FILL_IT_EXPLAN=1"),
                ],
                zh="未填写商品说明（PC/移动），需环境变量开启",
            ),
        )

    # 必须等主表单 ready 后再填；否则 select/fill 会出现定位成功但值未落地的问题。
    wait_t0 = time.perf_counter()
    page.wait_for_selector('form[name="fitemform"]', timeout=60_000)
    _upload_phase_log(
        "upload.fill.wait_form",
        "等待 fitemform 表单出现在 DOM",
        elapsed_ms=_ms_since(wait_t0),
        thread_label=thread_label,
        extra=[("goods_id", gid)],
    )

    core_t0 = time.perf_counter()
    # 分类：必须来自 tag 映射后的 seven17 ca_id。
    page.select_option('select[name="ca_id"]', value=ca_id)

    # 标题/售价：无价白名单可不填售价。
    page.fill('input[name="it_name"]', title)
    if str(price or "").strip():
        page.fill('input[name="it_price"]', price)
    else:
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "itemform.note"),
                    ("it_price", "skip"),
                    ("reason", "whitelist_no_price"),
                ],
                zh="无价白名单：不填写售价字段",
            ),
        )

    # 库存：只写 stock_qty，和说明字段完全独立。
    stock = page.locator('input[name="it_stock_qty"]')
    if stock.count():
        stock.fill(stock_qty)

    # 上架状态（it_use=1）与销售类型（it_sc_type）是业务开关；若站点主题缺失控件则静默跳过。
    use = page.locator('select[name="it_use"]')
    if use.count():
        try:
            use.select_option(value="1")
        except Exception:
            pass

    sc = page.locator('select[name="it_sc_type"]')
    if sc.count():
        try:
            sc.select_option(value=sc_type)
        except Exception:
            pass

    # 商品选择选项：仅使用 LLM 的韩文 attr_map_ko；没有韩文选项则跳过。
    option_pairs = _ko_option_pairs_from_attr_map(option_attr_map_ko)
    if option_pairs:
        for idx, (subject, items_csv) in enumerate(option_pairs, start=1):
            page.fill(f'input[name="opt{idx}_subject"]', subject)
            page.fill(f'input[name="opt{idx}"]', items_csv)
        # 清空剩余的输入框，避免旧页面残留值干扰。
        for idx in range(len(option_pairs) + 1, 4):
            page.fill(f'input[name="opt{idx}_subject"]', "")
            page.fill(f'input[name="opt{idx}"]', "")
        btn_opt_create = page.locator('#option_table_create')
        if btn_opt_create.count():
            try:
                btn_opt_create.first.click(timeout=30_000)
                page.wait_for_timeout(800)
            except Exception:
                pass
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "itemform.option"),
                    ("goods_id", gid),
                    ("source", "attr_map_ko"),
                    ("opt_count", len(option_pairs)),
                    ("opt_subjects", ",".join(x[0] for x in option_pairs)),
                ],
                zh="已填写商品选择选项（韩文）",
            ),
        )
    else:
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "itemform.option"),
                    ("goods_id", gid),
                    ("reason", "no_korean_attr_map"),
                ],
                zh="跳过商品选择选项：无可用韩文选项",
            ),
        )

    _upload_phase_log(
        "upload.fill.core_fields",
        "已填写分类/标题/价格/库存/选项",
        elapsed_ms=_ms_since(core_t0),
        thread_label=thread_label,
        extra=[("goods_id", gid), ("opt_pairs", len(option_pairs))],
    )

    if fill_it_explan:
        editor_t0 = time.perf_counter()
        # 先直接写隐藏 textarea 的 value（纯 JS 赋值，不触发键盘输入，避免误打到焦点输入框）。
        page.evaluate(
            """(payload) => {
                const html = payload.html;
                const ids = payload.ids || [];
                for (let i = 0; i < ids.length; i++) {
                    const ta = document.getElementById(ids[i]);
                    if (!ta) continue;
                    ta.value = html;
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""",
            {"html": desc_html, "ids": ["it_explan", "it_mobile_explan"]},
        )

        # seven17 这里是 SmartEditor2（oEditors），不是 CKEditor。
        # 仅给 it_explan / it_mobile_explan 两个编辑器灌值，并回写到隐藏 textarea。
        try:
            page.wait_for_function(
                """() => {
                    const ed = window.oEditors;
                    if (!ed || !ed.getById) return false;
                    const ids = ['it_explan', 'it_mobile_explan'];
                    for (let i = 0; i < ids.length; i++) {
                        const arr = ed.getById[ids[i]];
                        if (arr && arr[0] && typeof arr[0].exec === 'function') return true;
                    }
                    return false;
                }""",
                timeout=15_000,
            )
        except Exception:
            pass

        page.evaluate(
            """(payload) => {
                const html = payload.html;
                const ids = payload.ids || [];

                // 先更新隐藏 textarea：即使编辑器实例尚未就绪，提交时也有值。
                for (let i = 0; i < ids.length; i++) {
                    const id = ids[i];
                    const ta = document.getElementById(id);
                    if (!ta) continue;
                    ta.value = html;
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                }

                // SmartEditor2 正式写入：仅针对“该 textarea 对应的编辑器 iframe”写入，
                // 避免通过通用命令把内容注入到当前焦点输入框（如 it_stock_qty）。
                const ed = window.oEditors;
                for (let i = 0; i < ids.length; i++) {
                    const id = ids[i];
                    const ta = document.getElementById(id);
                    if (!ta) continue;

                    // 1) 直接写入本 textarea 所在容器下的 SmartEditor2 iframe body（精确目标）。
                    try {
                        const holder = ta.parentElement || ta;
                        const iframe = holder.querySelector('iframe[src*="SmartEditor2Skin.html"]');
                        const doc = iframe && iframe.contentDocument;
                        const body = doc && doc.body;
                        if (body) {
                            body.innerHTML = html;
                        }
                    } catch (e) {
                        // 忽略 iframe 写入失败，继续走实例回写。
                    }

                    // 2) 调用 UPDATE_CONTENTS_FIELD 把编辑器内容同步回隐藏 textarea。
                    if (!ed || !ed.getById) continue;
                    const arr = ed.getById[id];
                    if (!arr || !arr[0] || typeof arr[0].exec !== 'function') continue;
                    try {
                        arr[0].exec('UPDATE_CONTENTS_FIELD', []);
                    } catch (e) {
                        // 忽略，避免单个编辑器失败阻塞整条上架。
                    }
                }
            }""",
            {"html": desc_html, "ids": ["it_explan", "it_mobile_explan"]},
        )
        _upload_phase_log(
            "upload.fill.editor",
            "已填写 SmartEditor 商品说明",
            elapsed_ms=_ms_since(editor_t0),
            thread_label=thread_label,
            extra=[("goods_id", gid)],
        )

    # 主图/附图：本地临时文件回填到 it_img1~it_imgN。
    img_fill_t0 = time.perf_counter()
    uploaded = 0
    for i, pth in enumerate(paths, start=1):
        inp = page.locator(f'input[type="file"][name="it_img{i}"]')
        if inp.count():
            one_t0 = time.perf_counter()
            inp.set_input_files(str(pth))
            uploaded += 1
            _upload_phase_log(
                "upload.fill.image_slot",
                "回填本地图片到 it_img 控件",
                elapsed_ms=_ms_since(one_t0),
                thread_label=thread_label,
                extra=[("goods_id", gid), ("slot", i), ("file", pth.name)],
            )
    _upload_phase_log(
        "upload.fill.images",
        "主图/附图回填完成",
        elapsed_ms=_ms_since(img_fill_t0),
        thread_label=thread_label,
        extra=[("goods_id", gid), ("uploaded", uploaded), ("paths", len(paths))],
    )
    _upload_phase_log(
        "upload.fill.internal_done",
        "填表子流程全部完成",
        elapsed_ms=_ms_since(fill_t0),
        thread_label=thread_label,
        extra=[("goods_id", gid)],
    )


def _desc_html_for_store_record(
    tpl: str,
    prod: dict[str, Any],
    record: dict[str, Any],
    *,
    upload_title: str,
    default_tpl: str,
) -> str:
    """商品说明 HTML；模板占位符与 wego 一致，并可选用 wecatalog_group / wecatalog_tag / shop_category_path。"""
    tags_join = ", ".join(prod["tag_names"]) if prod["tag_names"] else "—"
    scp = record.get("shop_category_path")
    if isinstance(scp, list):
        path_str = " > ".join(str(x) for x in scp if x)
    else:
        path_str = ""
    base = tpl.strip() or default_tpl
    return base.format(
        title=html.escape(upload_title),
        goods_id=html.escape(prod["goods_id"]),
        goods_num=html.escape(prod["goods_num"]),
        tags=html.escape(tags_join),
        wecatalog_group=html.escape(str(record.get("wecatalog_group") or "—")),
        wecatalog_tag=html.escape(str(record.get("wecatalog_tag") or "—")),
        shop_category_path=html.escape(path_str or "—"),
    )


def _desc_html_from_llm_ko(desc_ko: str) -> str:
    """将 LLM 韩文描述转换为简单 HTML（按换行转 `<br>`）。"""
    text = str(desc_ko or "").strip()
    if not text:
        return ""
    lines = [html.escape(x.strip()) for x in text.splitlines() if x and x.strip()]
    if not lines:
        return ""
    return "<br>".join(lines)


def _upload_row_key(rec: dict[str, Any]) -> tuple[str, int]:
    gid = str(rec.get("goods_id") or "")
    try:
        tag_id = int(rec.get("tag_id") or 0)
    except (TypeError, ValueError):
        tag_id = 0
    return (gid, tag_id)


def _resolve_no_price_whitelist_upload_price_krw(
    rec: dict[str, Any],
    *,
    default_price: str,
    krw_per_cny: float | None,
) -> str:
    """白名单分类无价时，用 ``SEVEN17_DEFAULT_PRICE`` 换算韩元售价。"""
    from product_feed_kr.listing.listing_llm_enrich import record_is_no_price_allowed_by_map_category

    if not record_is_no_price_allowed_by_map_category(rec):
        return ""
    cny = parse_price_str(default_price, "")
    if not cny or cny in ("0", "0.0"):
        return ""
    if krw_per_cny is not None:
        krw = cny_listing_amount_to_krw_won_str(cny, krw_per_cny)
        return krw if _krw_price_str_usable(krw) else ""
    return cny if _krw_price_str_usable(cny) else ""


def _upload_skip_reason(
    rec: dict[str, Any],
    *,
    skip_uploaded: bool,
    llm_on: bool,
    default_price: str,
) -> str | None:
    """不可上架时返回原因码；可上架返回 None。"""
    from product_feed_kr.wecatalog.wecatalog_listed_at import record_skipped_as_listed_too_old

    if record_skipped_as_listed_too_old(rec):
        return "listed_too_old"
    if skip_uploaded and rec.get("seven17_uploaded_at"):
        return "already_uploaded"
    if rec.get("can_process") is False:
        return "duplicate_item"
    com = commodity_from_wecatalog_record(rec)
    if not isinstance(com, dict):
        return "no_detail"
    try:
        prod = parse_wego_product(com, default_price_if_missing=default_price)
    except ValueError:
        return "no_detail"
    if llm_on and not rec.get("llm_processed_at"):
        return "llm_not_processed"
    if llm_on:
        from product_feed_kr.listing.listing_llm_enrich import record_llm_uploadable_at_upload

        if not record_llm_uploadable_at_upload(rec):
            return "llm_not_uploadable"
    ca_id, _ca_src = resolve_ca_id_for_store_record(rec)
    if not (ca_id or "").strip():
        return "no_category"
    from product_feed_kr.wego.wego_commodity import commodity_raw_media_urls, filter_image_urls

    raw_media = commodity_raw_media_urls(com)
    image_urls = filter_image_urls(raw_media)
    if not image_urls:
        if raw_media:
            return "video_media"
        return "no_images"
    return None


def _count_uploadable_pending(
    conn: Any,
    album_id: str,
    *,
    skip_uploaded: bool,
    llm_on: bool,
    default_price: str,
) -> int:
    """统计当前库内满足上架条件的商品条数。"""
    from product_feed_kr.db.store_sqlite import sqlite_load_products_for_upload

    items = sqlite_load_products_for_upload(conn, album_id, skip_uploaded=skip_uploaded)
    n = 0
    for rec in items:
        if not isinstance(rec, dict):
            continue
        if (
            _upload_skip_reason(
                rec,
                skip_uploaded=skip_uploaded,
                llm_on=llm_on,
                default_price=default_price,
            )
            is None
        ):
            n += 1
    return n


def _log_upload_pending(
    conn: Any,
    ctx: UploadRunContext,
    *,
    thread_label: str = "",
    threads: int | None = None,
    last_goods_id: str | None = None,
    last_outcome: str | None = None,
) -> None:
    t0 = time.perf_counter()
    pending_upload = _count_uploadable_pending(
        conn,
        ctx.album_id,
        skip_uploaded=ctx.skip_uploaded,
        llm_on=ctx.llm_on,
        default_price=ctx.default_price,
    )
    count_ms = _ms_since(t0)
    kv: list[tuple[str, Any]] = [
        ("event", "upload.pending"),
        ("album_id", ctx.album_id),
        ("pending_upload", pending_upload),
        ("limit", ctx.limit if ctx.limit is not None else "none"),
        ("count_elapsed_ms", count_ms),
    ]
    if threads is not None:
        kv.append(("threads", threads))
    if thread_label:
        kv.append(("thread", thread_label))
    if last_goods_id:
        kv.append(("last_goods_id", pf_goods_id({"goods_id": last_goods_id})))
    if last_outcome:
        kv.append(("last_outcome", last_outcome))
    _log.info("%s", pf_kv(kv, zh="待上传商品数"))


def _refresh_upload_runtime_caches() -> None:
    """每轮上架前刷新分类相关缓存，避免长驻进程读到旧映射。"""
    from product_feed_kr.seven17.seven17_path_ca_map import invalidate_path_ca_cache
    from product_feed_kr.wecatalog.wecatalog_tag_mapping import invalidate_mapping_cache

    t0 = time.perf_counter()
    invalidate_mapping_cache()
    invalidate_path_ca_cache()
    # seven17 分类目录是 lru_cache；运行时文件更新后需主动清理。
    cache_clear = getattr(load_seven17_ca_catalog, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()
    _upload_phase_log(
        "upload.cache.refresh",
        "上架前已刷新分类映射缓存",
        elapsed_ms=_ms_since(t0),
    )


def upload_thread_count() -> int:
    """``SEVEN17_UPLOAD_THREADS``：上架工作线程数（默认 1）；每线程独立浏览器登录。"""
    raw = _cfg_get("SEVEN17_UPLOAD_THREADS")
    if raw is None or not str(raw).strip():
        return 1
    try:
        n = int(str(raw).strip())
    except ValueError:
        return 1
    return max(1, min(n, 16))


def _claim_next_uploadable(
    conn: Any,
    album_id: str,
    *,
    skip_uploaded: bool,
    session_skipped: set[tuple[str, int]],
    llm_on: bool,
    default_price: str,
    claim_lock: threading.Lock | None = None,
    in_flight: set[tuple[str, int]] | None = None,
) -> dict[str, Any] | None:
    """每次从 SQLite 重新加载，按微猫上架时间早的优先取第一条可上架且本 run 未占用/未跳过的记录。"""
    from product_feed_kr.db.store_sqlite import sqlite_load_products_for_upload

    def _scan() -> dict[str, Any] | None:
        t0 = time.perf_counter()
        items = sqlite_load_products_for_upload(conn, album_id, skip_uploaded=skip_uploaded)
        load_ms = _ms_since(t0)
        scanned = 0
        skip_counts: dict[str, int] = {}
        for rec in items:
            if not isinstance(rec, dict):
                continue
            scanned += 1
            key = _upload_row_key(rec)
            if key in session_skipped:
                skip_counts["session_skipped"] = skip_counts.get("session_skipped", 0) + 1
                continue
            if in_flight is not None and key in in_flight:
                skip_counts["in_flight"] = skip_counts.get("in_flight", 0) + 1
                continue
            reason = _upload_skip_reason(
                rec,
                skip_uploaded=skip_uploaded,
                llm_on=llm_on,
                default_price=default_price,
            )
            if reason is not None:
                session_skipped.add(key)
                skip_counts[reason] = skip_counts.get(reason, 0) + 1
                continue
            if in_flight is not None:
                in_flight.add(key)
            skip_summary = ",".join(
                f"{_UPLOAD_SKIP_REASON_ZH.get(k, k)}:{v}"
                for k, v in sorted(skip_counts.items())
            ) or "—"
            _upload_phase_log(
                "upload.claim.found",
                "读库扫描后选中本条待上架",
                elapsed_ms=_ms_since(t0),
                extra=[
                    ("rows_loaded", len(items)),
                    ("load_ms", load_ms),
                    ("scanned", scanned),
                    ("skip_summary", skip_summary),
                ],
                rec=rec,
            )
            return rec
        skip_summary = ",".join(
            f"{_UPLOAD_SKIP_REASON_ZH.get(k, k)}:{v}"
            for k, v in sorted(skip_counts.items())
        ) or "—"
        _upload_phase_log(
            "upload.claim.empty",
            "读库扫描未找到可上架商品",
            elapsed_ms=_ms_since(t0),
            extra=[
                ("rows_loaded", len(items)),
                ("load_ms", load_ms),
                ("scanned", scanned),
                ("skip_summary", skip_summary),
            ],
        )
        return None

    if claim_lock is not None:
        with claim_lock:
            return _scan()
    return _scan()


@dataclass(frozen=True)
class UploadRunContext:
    album_id: str
    skip_uploaded: bool
    dry_run: bool
    write_back: bool
    limit: int | None
    itemform_url: str
    seven17_base: str
    mb_id: str
    mb_password: str
    stock_qty: str
    default_price: str
    sc_type: str
    max_img: int
    title_prefix: str
    tpl: str
    convert_fx: bool
    write_back_after_llm: bool
    llm_on: bool
    restart_after_upload: int


def _release_upload_claim(
    row_key: tuple[str, int],
    session_skipped: set[tuple[str, int]],
    *,
    mark_session_skip: bool,
    claim_lock: threading.Lock | None,
    in_flight: set[tuple[str, int]] | None,
) -> None:
    def _do() -> None:
        if mark_session_skip:
            session_skipped.add(row_key)
        if in_flight is not None:
            in_flight.discard(row_key)

    if claim_lock is not None:
        with claim_lock:
            _do()
    else:
        _do()


def _upload_loop_should_stop(ctx: UploadRunContext, stats: dict[str, Any]) -> bool:
    if ctx.limit is not None and (stats["ok"] + stats["fail"] + stats["skip"]) >= ctx.limit:
        return True
    if ctx.restart_after_upload > 0 and stats["ok"] >= ctx.restart_after_upload:
        return True
    return False


def _process_upload_record(
    rec: dict[str, Any],
    page: Any,
    conn: Any,
    ctx: UploadRunContext,
    dialogs: list[str],
    *,
    thread_label: str = "",
    image_prefetch: UploadImagePrefetchQueue | None = None,
) -> tuple[Literal["ok", "fail", "skip"], dict[str, Any] | None]:
    """处理单条上架（填表/提交）；返回结果与可选 errors 条目。"""
    from product_feed_kr.db.store_sqlite import sqlite_mark_uploaded, sqlite_update_upload_fields

    aid = ctx.album_id
    gid = str(rec.get("goods_id") or "").strip() or "-"
    th_kv = [("thread", thread_label)] if thread_label else []
    record_t0 = time.perf_counter()

    _upload_phase_log(
        "upload.record.begin",
        "开始处理单条上架（尚未打开/刷新表单）",
        thread_label=thread_label,
        rec=rec,
        extra=[("max_img", ctx.max_img), ("dry_run", 1 if ctx.dry_run else 0)],
    )

    prep_t0 = time.perf_counter()
    com = commodity_from_wecatalog_record(rec)
    if com is None:
        return "fail", {"goods_id": gid, "error": "无商品标题/抓取字段，无法上架"}

    llm_data = rec.get("listing_llm") if isinstance(rec.get("listing_llm"), dict) else {}
    try:
        prod = parse_wego_product(com, default_price_if_missing=ctx.default_price)
    except ValueError as e:
        return "fail", {"goods_id": gid, "error": str(e)}

    gname = str(rec.get("wecatalog_group") or "")
    tname = str(rec.get("wecatalog_tag") or "")
    ca_id, ca_src = resolve_ca_id_for_store_record(rec, commodity=com)
    ca_id = (ca_id or "").strip()
    if not ca_id:
        return (
            "fail",
            {
                "goods_id": gid,
                "error": "无法解析 seven17 分类：请在 05 商品库浏览「分类配对」中配置韩文路径并同步 path_ca_map",
            },
        )
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "upload.ca_id"),
                *pf_store_row_id_kv(rec),
                ("ca_id", ca_id),
                ("source", ca_src),
            ],
            zh="本条上架分类来源",
        ),
    )
    upload_title = _upload_title_from_record(
        rec,
        prod,
        llm_on=ctx.llm_on,
        title_prefix=ctx.title_prefix,
    )
    if upload_title is None:
        return (
            "fail",
            {
                "goods_id": gid,
                "error": "缺少上架标题：请运行 LLM 补全（name_ko）或检查商品标题",
            },
        )
    desc_ko = str(llm_data.get("desc_ko") or "").strip()
    if not desc_ko:
        return (
            "skip",
            {
                "goods_id": gid,
                "error": "缺少韩文描述 desc_ko：请运行 LLM 生成描述，未命中时不上架",
            },
        )
    desc_html = _desc_html_from_llm_ko(desc_ko)
    desc_src = "llm_desc_ko"
    _log.info(
        "%s",
        pf_kv(
            [
                *th_kv,
                ("event", "desc.source"),
                *pf_store_row_id_kv(rec),
                ("source", desc_src),
                ("desc_len", len(desc_html)),
            ],
            zh="本条商品说明来源",
        ),
    )

    urls = prod["image_urls"][: ctx.max_img]
    if not urls:
        from product_feed_kr.wego.wego_commodity import commodity_raw_media_urls

        if commodity_raw_media_urls(com):
            return "skip", {"goods_id": gid, "error": "商品媒体为视频，无上架静态图，已跳过"}
        return "fail", {"goods_id": gid, "error": "无主图/图片 URL（imgsSrc/imgs）"}

    _upload_phase_log(
        "upload.record.prep_done",
        "本条上架字段/分类/标题/描述已就绪，开始取图片",
        elapsed_ms=_ms_since(prep_t0),
        thread_label=thread_label,
        rec=rec,
        extra=[("img_count", len(urls))],
    )

    img_t0 = time.perf_counter()
    img_source = "sync"
    try:
        if image_prefetch is not None:
            tmp_files, img_source = image_prefetch.acquire_images(
                rec,
                urls,
                thread_label=thread_label,
            )
        else:
            tmp_files = _download_images_for_upload(
                urls,
                thread_label=thread_label,
                rec=rec,
            )
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        if image_prefetch is not None:
            image_prefetch.release_record(rec)
        return "fail", {"goods_id": gid, "error": f"图片下载失败: {e}"}

    _upload_phase_log(
        "upload.images_done",
        "商品图片已就绪",
        elapsed_ms=_ms_since(img_t0),
        thread_label=thread_label,
        rec=rec,
        extra=[
            ("count", len(tmp_files)),
            ("source", img_source),
        ],
    )

    dialogs.clear()
    try:
        price_t0 = time.perf_counter()
        listing_price, price_src = _resolve_upload_listing_price_krw(
            rec,
            llm_on=ctx.llm_on,
            llm_data=llm_data,
            prod=prod,
            krw_per_cny=None,
        )
        skip_price_fill = _upload_skip_price_fill(rec, listing_price)

        krw_pc: float | None = None
        fx_src: str | None = None
        fx_rate: float | None = None
        if (
            not skip_price_fill
            and ctx.convert_fx
            and price_src not in ("price_krw", "listing_llm")
            and not _krw_price_str_usable(listing_price)
        ):
            fx_t0 = time.perf_counter()
            krw_pc, fx_src = _resolve_krw_per_cny()
            _upload_phase_log(
                "upload.fx.resolve",
                "解析 CNY→KRW 汇率",
                elapsed_ms=_ms_since(fx_t0),
                thread_label=thread_label,
                rec=rec,
                extra=[
                    ("krw_per_cny", krw_pc),
                    ("source", _fx_source_cn(fx_src or "")),
                ],
            )
            fx_rate = krw_pc
            listing_price, price_src = _resolve_upload_listing_price_krw(
                rec,
                llm_on=ctx.llm_on,
                llm_data=llm_data,
                prod=prod,
                krw_per_cny=fx_rate,
            )

        if (
            ctx.llm_on
            and not skip_price_fill
            and price_src not in ("price_krw", "listing_llm")
        ):
            _log.warning(
                "%s",
                pf_kv(
                    [
                        *th_kv,
                        ("event", "upload.price_fallback"),
                        *pf_store_row_id_kv(rec),
                        ("fallback", price_src),
                        ("price_krw", listing_price),
                    ],
                    zh="使用人民币换算或标题解析等回退韩元价",
                ),
            )
        cny_src = _price_cny_usable(rec) or str(rec.get("price_cny") or "").strip() or None

        if skip_price_fill:
            listing_price = ""
            price_src = "whitelist_no_price"
            _upload_phase_log(
                "upload.price.resolve",
                "无价白名单：不换算、不填售价",
                elapsed_ms=_ms_since(price_t0),
                thread_label=thread_label,
                rec=rec,
                extra=[("price_src", price_src)],
            )
        elif not str(listing_price).strip():
            if fx_rate is None and ctx.convert_fx:
                krw_pc, fx_src = _resolve_krw_per_cny()
                fx_rate = krw_pc
            listing_price = _resolve_no_price_whitelist_upload_price_krw(
                rec,
                default_price=ctx.default_price,
                krw_per_cny=fx_rate,
            )
            if listing_price:
                price_src = "whitelist_default"
        if not skip_price_fill and not str(listing_price).strip():
            return "skip", {"goods_id": gid, "error": "无法得到有效售价，已跳过"}

        if not skip_price_fill:
            _upload_phase_log(
                "upload.price.resolve",
                "韩元售价已确定",
                elapsed_ms=_ms_since(price_t0),
                thread_label=thread_label,
                rec=rec,
                extra=[("price_src", price_src), ("it_price", listing_price)],
            )

        rec["price_krw"] = listing_price if listing_price else None
        fx_log_rate: float | None = fx_rate if price_src in (
            "price_cny",
            "listing_llm_cny",
            "title_parse",
            "whitelist_default",
        ) else None
        pcny_log: str | None = (
            (_price_cny_usable(rec) or None) if fx_log_rate is not None else None
        )
        if ctx.write_back_after_llm and _upload_db_write_needed(price_src):
            wb_t0 = time.perf_counter()
            sqlite_update_upload_fields(conn, aid, rec)
            _upload_phase_log(
                "upload.db.price_write",
                "写回上架衍生字段到 SQLite（本次韩元价经换算或新得出）",
                elapsed_ms=_ms_since(wb_t0),
                thread_label=thread_label,
                rec=rec,
                extra=[("price_src", price_src)],
            )

        ca_ko, ca_zh = _ca_id_log_paths(gname, tname)
        last_fail_reason: str | None = None
        for upload_attempt in range(2):
            if upload_attempt > 0:
                _upload_phase_log(
                    "upload.attempt.retry",
                    "提交失败后会话失效，重登后重试本条",
                    thread_label=thread_label,
                    rec=rec,
                    extra=[("attempt", upload_attempt + 1)],
                )
                if not _relogin_upload_session(page, ctx, thread_label=thread_label, rec=rec):
                    return (
                        "fail",
                        {
                            "goods_id": gid,
                            "error": "会话失效后重新登录失败（仍停留在 login.php）",
                        },
                    )
                dialogs.clear()

            goto_t0 = time.perf_counter()
            _upload_phase_log(
                "upload.goto.itemform",
                "打开后台新增商品表单页",
                thread_label=thread_label,
                rec=rec,
                extra=[("attempt", upload_attempt + 1), ("url", ctx.itemform_url)],
            )
            page.goto(ctx.itemform_url, wait_until="domcontentloaded", timeout=120_000)
            _upload_phase_log(
                "upload.goto.itemform.done",
                "商品表单页 domcontentloaded",
                elapsed_ms=_ms_since(goto_t0),
                thread_label=thread_label,
                rec=rec,
                extra=[("page_url", _preview_text(page.url, 120))],
            )
            if _page_is_login(page):
                if upload_attempt == 0 and _relogin_upload_session(
                    page, ctx, thread_label=thread_label, rec=rec
                ):
                    dialogs.clear()
                    page.goto(ctx.itemform_url, wait_until="domcontentloaded", timeout=120_000)
                if _page_is_login(page):
                    return (
                        "fail",
                        {
                            "goods_id": gid,
                            "error": "打开商品表单时被重定向到登录页",
                        },
                    )

            fill_t0 = time.perf_counter()
            _upload_phase_log(
                "upload.fill.begin",
                "开始在浏览器中填写表单",
                thread_label=thread_label,
                rec=rec,
            )
            _fill_itemform(
                page,
                goods_id=gid,
                wecatalog_group=gname,
                wecatalog_tag=tname,
                ca_id=ca_id,
                ca_path_ko=ca_ko,
                ca_path_zh=ca_zh,
                title=upload_title,
                price=listing_price,
                stock_qty=ctx.stock_qty,
                desc_html=desc_html,
                image_path=None,
                image_paths=tmp_files,
                sc_type=ctx.sc_type,
                option_attr_map_ko=llm_data.get("attr_map_ko")
                if isinstance(llm_data.get("attr_map_ko"), dict)
                else None,
                price_cny_for_log=pcny_log,
                fx_krw_per_cny=fx_log_rate,
                price_src=price_src,
                thread_label=thread_label,
            )
            _upload_phase_log(
                "upload.fill.done",
                "浏览器表单填写完成",
                elapsed_ms=_ms_since(fill_t0),
                thread_label=thread_label,
                rec=rec,
            )

            if ctx.dry_run:
                dry_payload: dict[str, Any] = {
                    "dry_run": True,
                    "goods_id": gid,
                    "title": upload_title,
                    "price_cny": cny_src,
                    "it_price_krw": listing_price,
                }
                if llm_data:
                    dry_payload["listing_llm"] = {
                        "cny_price": llm_data.get("cny_price"),
                        "attr_map": llm_data.get("attr_map"),
                        "attr_map_ko": llm_data.get("attr_map_ko"),
                        "name_zh": llm_data.get("name_zh"),
                        "name_ko": llm_data.get("name_ko"),
                        "desc_zh": llm_data.get("desc_zh"),
                        "desc_ko": llm_data.get("desc_ko"),
                    }
                if ctx.convert_fx and krw_pc is not None:
                    dry_payload["fx_krw_per_cny"] = krw_pc
                    dry_payload["fx_source"] = fx_src
                print(json.dumps(dry_payload, ensure_ascii=False))
                return "ok", None

            submit_t0 = time.perf_counter()
            _upload_phase_log(
                "upload.submit.click",
                "点击商品表单提交",
                thread_label=thread_label,
                rec=rec,
            )
            click_itemform_submit(page)
            page.wait_for_load_state("load", timeout=120_000)
            _upload_phase_log(
                "upload.submit.wait_load",
                "提交后等待页面 load",
                elapsed_ms=_ms_since(submit_t0),
                thread_label=thread_label,
                rec=rec,
            )

            ok_submit, it_id, fail_reason = _classify_after_submit(page, dialogs)
            if ok_submit:
                if ctx.write_back:
                    rec["uploaded_to_platform"] = True
                    rec["seven17_uploaded_at"] = now_cst8_iso()
                    sqlite_mark_uploaded(conn, aid, rec)
                row_out: dict[str, Any] = {
                    "ok": True,
                    "goods_id": gid,
                    "title": upload_title,
                    "it_price": listing_price,
                }
                if ctx.convert_fx and krw_pc is not None:
                    row_out["price_cny"] = cny_src
                    row_out["fx_krw_per_cny"] = krw_pc
                if it_id:
                    row_out["it_id"] = it_id
                print(json.dumps(row_out, ensure_ascii=False))
                _upload_phase_log(
                    "upload.record.done",
                    "本条上架成功",
                    elapsed_ms=_ms_since(record_t0),
                    thread_label=thread_label,
                    rec=rec,
                    extra=[("it_id", it_id or "")],
                )
                return "ok", None

            last_fail_reason = fail_reason
            if upload_attempt == 0 and _upload_fail_is_session_expired(fail_reason, page):
                continue
            break

        _upload_phase_log(
            "upload.record.fail",
            "本条上架失败",
            elapsed_ms=_ms_since(record_t0),
            thread_label=thread_label,
            rec=rec,
            extra=[("error", last_fail_reason or "未知错误")],
        )
        return "fail", {"goods_id": gid, "error": last_fail_reason or "未知错误"}
    except Exception as e:
        _upload_phase_log(
            "upload.record.exception",
            "本条上架异常",
            elapsed_ms=_ms_since(record_t0),
            thread_label=thread_label,
            rec=rec,
            extra=[("error", str(e))],
        )
        return "fail", {"goods_id": gid, "error": str(e)}
    finally:
        if image_prefetch is not None:
            image_prefetch.release_record(rec)
        for pth in tmp_files:
            pth.unlink(missing_ok=True)


def _upload_claim_loop(
    page: Any,
    conn: Any,
    ctx: UploadRunContext,
    dialogs: list[str],
    stats: dict[str, Any],
    *,
    session_skipped: set[tuple[str, int]],
    claim_lock: threading.Lock | None = None,
    in_flight: set[tuple[str, int]] | None = None,
    thread_label: str = "",
    aggregate_stats: dict[str, Any] | None = None,
    aggregate_lock: threading.Lock | None = None,
    image_prefetch: UploadImagePrefetchQueue | None = None,
) -> None:
    """每次上架完成后重新读库取下一条。"""
    while True:
        if aggregate_stats is not None and aggregate_lock is not None:
            with aggregate_lock:
                if _upload_loop_should_stop(ctx, aggregate_stats):
                    if (
                        ctx.restart_after_upload > 0
                        and aggregate_stats["ok"] >= ctx.restart_after_upload
                    ):
                        aggregate_stats["restart_fresh"] = True
                        _log.info(
                            "%s",
                            pf_kv(
                                [
                                    ("event", "upload.restart_after"),
                                    ("ok_count", aggregate_stats["ok"]),
                                    ("restart_after", ctx.restart_after_upload),
                                    *([("thread", thread_label)] if thread_label else []),
                                ],
                                zh="已达配置的上架成功条数阈值，结束本进程",
                            ),
                        )
                    break
        elif _upload_loop_should_stop(ctx, stats):
            if ctx.restart_after_upload > 0 and stats["ok"] >= ctx.restart_after_upload:
                stats["restart_fresh"] = True
                _log.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "upload.restart_after"),
                            ("ok_count", stats["ok"]),
                            ("restart_after", ctx.restart_after_upload),
                            *([("thread", thread_label)] if thread_label else []),
                        ],
                        zh="已达配置的上架成功条数阈值，结束本进程",
                    ),
                )
            break

        _upload_phase_log(
            "upload.claim.start",
            "读库扫描下一条可上架商品",
            thread_label=thread_label,
        )
        rec = _claim_next_uploadable(
            conn,
            ctx.album_id,
            skip_uploaded=ctx.skip_uploaded,
            session_skipped=session_skipped,
            llm_on=ctx.llm_on,
            default_price=ctx.default_price,
            claim_lock=claim_lock,
            in_flight=in_flight,
        )
        if rec is None:
            break

        log_item_separator(_log)
        row_key = _upload_row_key(rec)
        outcome, err_entry = _process_upload_record(
            rec,
            page,
            conn,
            ctx,
            dialogs,
            thread_label=thread_label,
            image_prefetch=image_prefetch,
        )
        target = aggregate_stats if aggregate_stats is not None else stats
        lock = aggregate_lock
        if lock is not None:
            with lock:
                if outcome == "ok":
                    target["ok"] += 1
                elif outcome == "skip":
                    target["skip"] += 1
                else:
                    target["fail"] += 1
                    if err_entry:
                        target["errors"].append(err_entry)
        else:
            if outcome == "ok":
                target["ok"] += 1
            elif outcome == "skip":
                target["skip"] += 1
            else:
                target["fail"] += 1
                if err_entry:
                    target["errors"].append(err_entry)
        _release_upload_claim(
            row_key,
            session_skipped,
            mark_session_skip=(outcome != "ok" or ctx.dry_run),
            claim_lock=claim_lock,
            in_flight=in_flight,
        )
        gid_done = str(rec.get("goods_id") or "").strip() or "-"
        _log_upload_pending(
            conn,
            ctx,
            thread_label=thread_label,
            last_goods_id=gid_done,
            last_outcome=outcome,
        )


def _run_upload_multithread_claim_loop(
    ctx: UploadRunContext,
    *,
    thread_count: int,
    mb_id: str,
    mb_password: str,
    base: str,
    headless: bool,
    exe: Path,
) -> dict[str, Any]:
    from product_feed_kr.db.store_sqlite import connect_sqlite, ensure_sqlite_schema

    stats: dict[str, Any] = {"ok": 0, "fail": 0, "skip": 0, "errors": [], "restart_fresh": False}
    claim_lock = threading.Lock()
    in_flight: set[tuple[str, int]] = set()
    session_skipped: set[tuple[str, int]] = set()
    stats_lock = threading.Lock()
    stop_event = threading.Event()
    work_q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max(2, thread_count * 2))

    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "upload.threads.start"),
                ("threads", thread_count),
                ("mode", "dispatcher_queue"),
            ],
            zh="多线程上架：主线程分配任务到队列，worker 仅消费",
        ),
    )

    def _dispatch() -> None:
        conn = connect_sqlite()
        ensure_sqlite_schema(conn)
        try:
            while not stop_event.is_set():
                with stats_lock:
                    if _upload_loop_should_stop(ctx, stats):
                        if ctx.restart_after_upload > 0 and stats["ok"] >= ctx.restart_after_upload:
                            if not stats["restart_fresh"]:
                                stats["restart_fresh"] = True
                                _log.info(
                                    "%s",
                                    pf_kv(
                                        [
                                            ("event", "upload.restart_after"),
                                            ("ok_count", stats["ok"]),
                                            ("restart_after", ctx.restart_after_upload),
                                        ],
                                        zh="已达配置的上架成功条数阈值，结束本进程",
                                    ),
                                )
                        stop_event.set()
                        break
                rec = _claim_next_uploadable(
                    conn,
                    ctx.album_id,
                    skip_uploaded=ctx.skip_uploaded,
                    session_skipped=session_skipped,
                    llm_on=ctx.llm_on,
                    default_price=ctx.default_price,
                    claim_lock=claim_lock,
                    in_flight=in_flight,
                )
                if rec is None:
                    break
                while not stop_event.is_set():
                    try:
                        work_q.put(rec, timeout=0.5)
                        break
                    except queue.Full:
                        continue
        finally:
            conn.close()
            for _ in range(thread_count):
                while True:
                    try:
                        work_q.put(None, timeout=0.5)
                        break
                    except queue.Full:
                        continue

    def _worker(worker_id: int) -> None:
        label = f"upload-{worker_id}"
        dialogs: list[str] = []
        try:
            with sync_playwright() as p:
                br_t0 = time.perf_counter()
                browser = p.chromium.launch(headless=headless, executable_path=str(exe))
                _upload_phase_log(
                    "upload.browser.launch",
                    "Playwright 已启动 Chromium",
                    elapsed_ms=_ms_since(br_t0),
                    thread_label=label,
                    extra=[("headless", 1 if headless else 0)],
                )
                try:
                    page = browser.new_page()
                    page.on("dialog", shop_admin_dialog_handler(dialogs))
                    _login_admin_with_log(
                        page,
                        base=base,
                        mb_id=mb_id,
                        mb_password=mb_password,
                        redirect_full_url=ctx.itemform_url,
                        thread_label=label,
                    )
                    if "login.php" in page.url:
                        raise RuntimeError(f"[{label}] 登录失败：仍停留在登录页")
                    conn = connect_sqlite()
                    ensure_sqlite_schema(conn)
                    try:
                        while True:
                            if stop_event.is_set() and work_q.empty():
                                break
                            try:
                                rec = work_q.get(timeout=0.5)
                            except queue.Empty:
                                continue
                            if rec is None:
                                work_q.task_done()
                                break
                            row_key = _upload_row_key(rec)
                            gid_done = str(rec.get("goods_id") or "").strip() or "-"
                            outcome: Literal["ok", "fail", "skip"] = "fail"
                            err_entry: dict[str, Any] | None = None
                            try:
                                log_item_separator(_log)
                                outcome, err_entry = _process_upload_record(
                                    rec,
                                    page,
                                    conn,
                                    ctx,
                                    dialogs,
                                    thread_label=label,
                                )
                            except Exception as e:
                                err_txt = str(e)
                                err_entry = {"goods_id": gid_done, "error": err_txt}
                                _log.warning(
                                    "%s",
                                    pf_kv(
                                        [
                                            ("event", "upload.record.error"),
                                            ("thread", label),
                                            ("goods_id", pf_goods_id({"goods_id": gid_done})),
                                            ("err", err_txt),
                                        ],
                                        zh="单条上架处理异常，已计为失败并释放 claim",
                                    ),
                                )
                                outcome = "fail"
                            finally:
                                with stats_lock:
                                    if outcome == "ok":
                                        stats["ok"] += 1
                                    elif outcome == "skip":
                                        stats["skip"] += 1
                                    else:
                                        stats["fail"] += 1
                                        if err_entry:
                                            stats["errors"].append(err_entry)
                                    if _upload_loop_should_stop(ctx, stats):
                                        if (
                                            ctx.restart_after_upload > 0
                                            and stats["ok"] >= ctx.restart_after_upload
                                            and not stats["restart_fresh"]
                                        ):
                                            stats["restart_fresh"] = True
                                            _log.info(
                                                "%s",
                                                pf_kv(
                                                    [
                                                        ("event", "upload.restart_after"),
                                                        ("ok_count", stats["ok"]),
                                                        ("restart_after", ctx.restart_after_upload),
                                                        ("thread", label),
                                                    ],
                                                    zh="已达配置的上架成功条数阈值，结束本进程",
                                                ),
                                            )
                                        stop_event.set()
                                _release_upload_claim(
                                    row_key,
                                    session_skipped,
                                    mark_session_skip=(outcome != "ok" or ctx.dry_run),
                                    claim_lock=claim_lock,
                                    in_flight=in_flight,
                                )
                                _log_upload_pending(
                                    conn,
                                    ctx,
                                    thread_label=label,
                                    last_goods_id=gid_done,
                                    last_outcome=outcome,
                                )
                                work_q.task_done()
                    finally:
                        conn.close()
                finally:
                    browser.close()
        except Exception as e:
            _log.warning(
                "%s",
                pf_kv(
                    [("event", "upload.thread.error"), ("thread", label), ("err", str(e))],
                    zh="上架工作线程异常退出",
                ),
            )

    threads = [
        threading.Thread(target=_worker, args=(i,), name=f"seven17-upload-{i}", daemon=True)
        for i in range(thread_count)
    ]
    dispatcher = threading.Thread(target=_dispatch, name="upload-dispatcher", daemon=True)
    dispatcher.start()
    for t in threads:
        t.start()
    dispatcher.join()
    for t in threads:
        t.join()

    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "upload.threads.done"),
                ("threads", thread_count),
                ("ok", stats["ok"]),
                ("fail", stats["fail"]),
                ("skip", stats["skip"]),
            ],
            zh="多线程上架本 run 结束",
        ),
    )
    return stats


def upload_from_wecatalog_store(
    album_id: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    skip_uploaded: bool = True,
    write_back: bool = False,
    keep_open: bool = False,
) -> dict[str, Any]:
    """从 SQLite 逐条上架：每处理完一条（成功/失败/跳过）后重新读库，取下一条可上架记录。

    字段来源：结构化抓取字段组装的 ``commodity`` → `parse_wego_product`；
    `ca_id`：map → 韩文路径匹配 → DB 缓存（无分类 LLM 兜底）。
    上传阶段不发起 listing LLM；分类 LLM 仅在 map/缓存均无时调用。
    **LLM 开关生效且 ``cny_price`` 为空** 时跳过。
    """
    mb_id = _env_required("SEVEN17_MB_ID")
    mb_password = _env_required("SEVEN17_MB_PASSWORD")

    aid = album_id.strip()
    if not aid:
        raise ValueError("album_id 不能为空")

    _configure_upload_stderr_logging()

    from product_feed_kr.db.store_sqlite import connect_sqlite, ensure_sqlite_schema

    conn_db = connect_sqlite()
    ensure_sqlite_schema(conn_db)

    base = (_cfg_get("SEVEN17_BASE_URL", "https://www.seven17.kr") or "https://www.seven17.kr").rstrip("/")
    headless = _bool_env("SEVEN17_HEADLESS", True)
    if keep_open:
        headless = False
    stock_qty = _cfg_get("SEVEN17_STOCK_QTY", "100") or "100"
    default_price = _cfg_get("SEVEN17_DEFAULT_PRICE", "0") or "0"
    sc_type = _cfg_get("SEVEN17_SC_TYPE", "1") or "1"
    max_img = int(_cfg_get("SEVEN17_MAX_IMAGES", "10") or "10")
    max_img = max(1, min(max_img, 10))
    title_prefix = (_cfg_get("WEGO_TITLE_PREFIX") or "").strip()
    tpl = (_cfg_get("WEGO_DESC_TEMPLATE") or "").strip()
    upload_threads = upload_thread_count()

    exe = chromium_executable()
    if not exe:
        raise FileNotFoundError(
            "未找到 chrome-win/chrome.exe，或设置 PLAYWRIGHT_CHROMIUM_EXECUTABLE",
        )

    itemform_url = f"{base}/adm/shop_admin/itemform.php"
    stats: dict[str, Any] = {"ok": 0, "fail": 0, "skip": 0, "errors": [], "restart_fresh": False}
    restart_after_upload = restart_after_n("SEVEN17_UPLOAD_RESTART_AFTER_ITEMS", 1000)
    convert_fx = _bool_env("SEVEN17_CONVERT_CNY_TO_KRW", True)
    write_back_after_llm = _cfg_bool("SEVEN17_WRITE_BACK_AFTER_LLM", True)
    llm_on = _cfg_bool("OPENAI_ENRICH_LISTING", True)

    upload_ctx = UploadRunContext(
        album_id=aid,
        skip_uploaded=skip_uploaded,
        dry_run=dry_run,
        write_back=write_back,
        limit=limit,
        itemform_url=itemform_url,
        seven17_base=base,
        mb_id=mb_id,
        mb_password=mb_password,
        stock_qty=stock_qty,
        default_price=default_price,
        sc_type=sc_type,
        max_img=max_img,
        title_prefix=title_prefix,
        tpl=tpl,
        convert_fx=convert_fx,
        write_back_after_llm=write_back_after_llm,
        llm_on=llm_on,
        restart_after_upload=restart_after_upload,
    )

    try:
        run_t0 = time.perf_counter()
        _upload_phase_log(
            "upload.run.begin",
            "上架 run 开始",
            extra=[
                ("album_id", aid),
                ("threads", upload_threads),
                ("headless", 1 if headless else 0),
                ("dry_run", 1 if dry_run else 0),
            ],
        )
        _refresh_upload_runtime_caches()
        _log_upload_pending(conn_db, upload_ctx, threads=upload_threads)
        _upload_phase_log(
            "upload.run.ready",
            "缓存刷新与待上传统计完成，即将启动浏览器",
            elapsed_ms=_ms_since(run_t0),
        )

        if upload_threads > 1:
            if keep_open:
                _log.warning(
                    "%s",
                    pf_kv(
                        [("event", "upload.keep_open_ignored"), ("threads", upload_threads)],
                        zh="多线程上架时忽略 keep_open",
                    ),
                )
            stats = _run_upload_multithread_claim_loop(
                upload_ctx,
                thread_count=upload_threads,
                mb_id=mb_id,
                mb_password=mb_password,
                base=base,
                headless=headless,
                exe=exe,
            )
        else:
            dialogs: list[str] = []
            session_skipped: set[tuple[str, int]] = set()
            _log.info(
                "%s",
                pf_kv(
                    [
                        ("event", "upload.mode"),
                        ("mode", "claim_next"),
                        ("threads", 1),
                    ],
                    zh="上架：每处理一条后重新读库取下一条",
                ),
            )
            prefetcher: UploadImagePrefetchQueue | None = None
            prefetch_n = upload_image_prefetch_queue_size()
            if prefetch_n > 0:
                prefetcher = UploadImagePrefetchQueue(
                    aid,
                    upload_ctx,
                    queue_size=prefetch_n,
                )
                prefetcher.attach_session_skipped(session_skipped)
                prefetcher.start()
            try:
                with sync_playwright() as p:
                    br_t0 = time.perf_counter()
                    browser = p.chromium.launch(headless=headless, executable_path=str(exe))
                    _upload_phase_log(
                        "upload.browser.launch",
                        "Playwright 已启动 Chromium",
                        elapsed_ms=_ms_since(br_t0),
                        extra=[("headless", 1 if headless else 0)],
                    )
                    try:
                        page = browser.new_page()
                        page.on("dialog", shop_admin_dialog_handler(dialogs))

                        _login_admin_with_log(
                            page,
                            base=base,
                            mb_id=mb_id,
                            mb_password=mb_password,
                            redirect_full_url=itemform_url,
                        )

                        if "login.php" in page.url:
                            raise RuntimeError(
                                "登录失败：仍停留在登录页（账号密码、验证码或权限）。",
                            )

                        _upload_claim_loop(
                            page,
                            conn_db,
                            upload_ctx,
                            dialogs,
                            stats,
                            session_skipped=session_skipped,
                            image_prefetch=prefetcher,
                        )
                    finally:
                        if keep_open:
                            _wait_keep_open_browser()
                        browser.close()
            finally:
                if prefetcher is not None:
                    prefetcher.stop()

    finally:
        if conn_db is not None:
            try:
                conn_db.close()
            except Exception:
                pass

    return stats


def _loop_log_basename(args: argparse.Namespace) -> str:
    if args.dry_run:
        return "seven17_upload_dryrun"
    return "seven17_upload"


def _run_once(args: argparse.Namespace) -> int:
    """单次上架 run（不含进程锁与 argparse）。"""
    aid = (args.album_id or _cfg_get("WECATALOG_ALBUM_ID") or "").strip()
    if args.preview_index is not None:
        try:
            print_store_sqlite_itemform_preview(aid, index=args.preview_index)
            return 0
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
            return 1

    try:
        stats = upload_from_wecatalog_store(
            aid,
            limit=args.limit,
            dry_run=args.dry_run,
            skip_uploaded=not args.include_uploaded,
            write_back=args.write_back,
            keep_open=args.keep_open,
        )
        print(json.dumps({"ok": stats["fail"] == 0, **stats}, ensure_ascii=False))
        if stats.get("restart_fresh"):
            reload_seven17_config()
            return EXIT_RESTART_FRESH_DATA
        return 0 if stats["fail"] == 0 else 2
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite 店铺数据 → seven17 后台商品表单录入")
    parser.add_argument(
        "--album-id",
        default=None,
        metavar="ID",
        help="微猫相册 albumId；省略时使用 seven17.json / 环境变量 WECATALOG_ALBUM_ID",
    )
    parser.add_argument(
        "--preview-index",
        type=int,
        default=None,
        metavar="N",
        help="仅预览 SQLite 中第 N 条商品的表单映射 JSON（不登录）；指定后忽略上架流程",
    )
    parser.add_argument(
        "--include-uploaded",
        action="store_true",
        help="仍处理 seven17_uploaded_at 非空（已上传平台）的记录",
    )
    parser.add_argument(
        "--write-back",
        action="store_true",
        help="上架成功后将 uploaded_to_platform + seven17_uploaded_at 写回 SQLite",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多处理多少条「待上架」记录（不含跳过 uploaded；默认不限制）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="登录并填表但不点击后台「确认」提交",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="结束前不立即关闭浏览器：强制有界面模式，填完后在本终端按 Enter 再关闭并退出",
    )
    parser.add_argument(
        "--llm-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="日志文件（UTF-8）；常驻模式下未指定则 data/logs/{任务}_{时间}.log",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只执行一轮后退出（默认循环直至 Ctrl+C）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 级别")
    args = parser.parse_args()

    if args.llm_only:
        _log.warning(
            "%s",
            pf_kv(
                [("event", "llm_only.deprecated")],
                zh="--llm-only 已移除，请改用 python -m product_feed_kr.seven17.seven17_llm 或 02_LLM补全上架信息.bat",
            ),
        )
        from product_feed_kr import seven17_llm

        return seven17_llm.main()

    _stdout_utf8()
    repeat = not args.once and args.preview_index is None
    log_file = args.log_file
    if repeat and log_file is None:
        log_file = default_log_path(_loop_log_basename(args))
    _init_upload_logging(log_file=log_file, verbose=args.verbose)

    aid = (args.album_id or _cfg_get("WECATALOG_ALBUM_ID") or "").strip()
    if not aid:
        parser.error(
            "缺少相册 ID：请传入 --album-id，或在 config/seven17.json / 环境变量中设置 WECATALOG_ALBUM_ID"
            "（与抓取店铺 URL 中的 albumId 一致）。",
        )

    lock_name = "seven17_upload_session"
    try:
        with single_instance_lock(lock_name):
            if repeat:
                return run_forever(
                    lambda: _run_once(args),
                    task_label=lock_name,
                    logger=_log,
                    on_restart_fresh=reload_seven17_config,
                    round_delay_sec=0.0,
                )
            return _run_once(args)
    except SystemExit as e:
        if e.code == EXIT_SINGLETON_CONFLICT:
            return EXIT_SINGLETON_CONFLICT
        raise


# 兼容旧 import：from product_feed_kr.seven17.seven17_upload import enrich_llm_for_sqlite_records
from product_feed_kr.seven17.seven17_llm import enrich_llm_for_sqlite_records  # noqa: E402,F401

if __name__ == "__main__":
    raise SystemExit(main())
