"""微猫 wecatalog 店铺页：Playwright 监听商品列表 JSON，滚动分页，增量写入 xlsx。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from playwright.sync_api import Response, sync_playwright

from product_feed_kr.playwright_path import chromium_executable


def _extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if payload.get("errcode") not in (0, None):
        return []
    r = payload.get("result")
    if isinstance(r, dict):
        items = r.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    return []


def _first_image_url(img: Any) -> str:
    if isinstance(img, str):
        return img.strip()
    if isinstance(img, dict):
        for k in ("url", "src", "link", "imgUrl", "imageUrl"):
            v = img.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _flatten_row(item: dict[str, Any]) -> dict[str, Any]:
    gid = item.get("goods_id") or item.get("id") or item.get("commodity_id")
    title = (
        item.get("goods_name")
        or item.get("title")
        or item.get("item_name")
        or item.get("name")
        or ""
    )
    if not isinstance(title, str):
        title = str(title)
    price = (
        item.get("optimaPrice")
        or item.get("itemPrice")
        or item.get("price")
        or item.get("show_price")
        or item.get("retail_price")
        or item.get("default_price")
        or ""
    )
    if price == "" and isinstance(item.get("priceArr"), list) and item["priceArr"]:
        p0 = item["priceArr"][0]
        if isinstance(p0, dict) and p0.get("value") is not None:
            price = p0.get("value")
    imgs = item.get("imgs") or item.get("images") or item.get("img_list") or []
    urls: list[str] = []
    if isinstance(imgs, list):
        for im in imgs:
            u = _first_image_url(im)
            if u:
                urls.append(u)
    return {
        "goods_id": gid if gid is not None else "",
        "title": title.strip(),
        "price": str(price).strip() if price is not None else "",
        "image_urls": "|".join(urls),
        "json": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
    }


def export_wecatalog(
    url: str,
    out_xlsx: Path,
    *,
    max_scrolls: int = 50,
    scroll_pause_ms: float = 900,
    idle_rounds: int = 4,
) -> dict[str, Any]:
    exe = chromium_executable()
    if not exe:
        raise FileNotFoundError(
            "未找到 chrome-win/chrome.exe，或设置 PLAYWRIGHT_CHROMIUM_EXECUTABLE",
        )

    out_xlsx.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "goods"
    headers = ["goods_id", "title", "price", "image_urls", "json"]
    ws.append(headers)

    seen: set[Any] = set()
    last_count = 0

    def save_if_grew() -> None:
        nonlocal last_count
        if len(seen) > last_count:
            last_count = len(seen)
            wb.save(out_xlsx)

    def on_response(response: Response) -> None:
        try:
            ct = (response.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            if response.status != 200:
                return
            data = response.json()
        except Exception:
            return
        items = _extract_items(data)
        if not items:
            return
        new = False
        for it in items:
            rowd = _flatten_row(it)
            gid = rowd["goods_id"]
            if gid in seen:
                continue
            seen.add(gid)
            ws.append([rowd[h] for h in headers])
            new = True
        if new:
            save_if_grew()

    stats = {"rows": 0, "scrolls": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=str(exe),
        )
        try:
            page = browser.new_page()
            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(int(scroll_pause_ms))

            stable = 0
            for i in range(max_scrolls):
                before = len(seen)
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(int(scroll_pause_ms))
                stats["scrolls"] = i + 1
                if len(seen) == before:
                    stable += 1
                else:
                    stable = 0
                if stable >= idle_rounds and len(seen) > 0:
                    break

            save_if_grew()
            stats["rows"] = len(seen)
        finally:
            browser.close()

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="wecatalog 商品导出 xlsx（增量保存）")
    parser.add_argument(
        "--url",
        default="https://shop00269423.wecatalog.cn/t/jgHapPR",
        help="店铺或专辑页链接",
    )
    parser.add_argument(
        "--out",
        default="data/wecatalog_export.xlsx",
        help="输出 xlsx 路径",
    )
    parser.add_argument("--max-scrolls", type=int, default=50)
    parser.add_argument("--idle-rounds", type=int, default=4)
    args = parser.parse_args()

    try:
        stats = export_wecatalog(
            args.url,
            Path(args.out),
            max_scrolls=args.max_scrolls,
            idle_rounds=args.idle_rounds,
        )
        print(json.dumps({"ok": True, **stats, "out": args.out}, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
