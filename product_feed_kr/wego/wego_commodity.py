"""微猫 / 微购风格的 `commodity` dict → 上架用扁平字段（供 `seven17_upload`）。"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_WEGO_DESC_TEMPLATE = (
    "<p><strong>상품명</strong>：{title}</p>"
    "<p><strong>원본 goods_id</strong>：{goods_id}</p>"
    "<p><strong>货号 goodsNum</strong>：{goods_num}</p>"
    "<p><strong>标签</strong>：{tags}</p>"
    "<p>来源：微购/album JSON 导入。</p>"
)


_TITLE_CNY_PRICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 微商标题常见：💰290、💰 290.5-Chrome...
    re.compile(r"💰\s*(\d+(?:\.\d+)?)"),
    re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(?:人民币|RMB|rmb)\s*[:：]?\s*(\d+(?:\.\d+)?)"),
    # P270、p 399（标价写法；P 前不可为字母数字，避免误匹配如 GP270）
    re.compile(r"(?<![A-Za-z0-9])P\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE),
)


def price_from_title_cny(title: str) -> str | None:
    """从标题中抓取人民币标价（¥/￥/💰/人民币/RMB/P 数字），命中即返回数字字符串，否则 None。"""
    if not (title and title.strip()):
        return None
    for rx in _TITLE_CNY_PRICE_PATTERNS:
        m = rx.search(title)
        if m:
            return m.group(1)
    return None


def parse_price_str(raw: str | None, default: str) -> str:
    if raw is None:
        return default
    s = str(raw).strip()
    if not s:
        return default
    digits = re.sub(r"[^\d.]", "", s)
    if not digits:
        return default
    if "." in digits:
        return str(int(float(digits))) if digits.replace(".", "").isdigit() else digits.split(".")[0]
    return digits


def commodity_image_urls(obj: dict[str, Any]) -> list[str]:
    """从 commodity 风格 dict 抽取主图/附图 URL 列表（不要求有价格）。"""
    urls_raw = obj.get("imgsSrc") or obj.get("imgs") or []
    if not isinstance(urls_raw, list):
        urls_raw = []
    image_urls: list[str] = []
    for u in urls_raw:
        if isinstance(u, str):
            u = u.strip().split("|")[0].strip()
            if u.startswith(("http://", "https://")):
                image_urls.append(u)
    return image_urls


def parse_wego_product(
    obj: dict[str, Any],
    *,
    default_price_if_missing: str | None = None,
) -> dict[str, Any]:
    """从单条 commodity 风格 dict 抽取上传所需字段。"""
    title = str(obj.get("title") or "").strip()
    if not title:
        raise ValueError("JSON 缺少 title")

    raw_price = obj.get("optimaPrice")
    if raw_price is not None and str(raw_price).strip() == "-1":
        raw_price = None
    if raw_price is None or str(raw_price).strip() == "":
        raw_price = None
        name_price = obj.get("itemNamePrice")
        if name_price is not None and str(name_price).strip() not in ("", "-1"):
            raw_price = name_price
    if raw_price is None or str(raw_price).strip() == "":
        arr = obj.get("priceArr") or []
        if isinstance(arr, list) and arr:
            first = arr[0]
            if isinstance(first, dict) and first.get("value") is not None:
                v = first.get("value")
                if v is not None and str(v).strip() not in ("", "-1"):
                    raw_price = v
    if raw_price is None or str(raw_price).strip() == "":
        tp = price_from_title_cny(title)
        if tp is not None:
            raw_price = tp
    if raw_price is None or str(raw_price).strip() == "":
        if default_price_if_missing is not None and str(default_price_if_missing).strip() != "":
            raw_price = default_price_if_missing
        else:
            raise ValueError(
                "JSON 缺少 optimaPrice、itemNamePrice、有效的 priceArr[0].value，且 title 中未匹配到 "
                "¥/￥/💰/人民币/RMB/P 数字 等形式价格",
            )

    srp = str(raw_price).strip()
    price_str = parse_price_str(srp, "0")
    if price_str == "0" and srp not in ("0", "0.0"):
        price_str = srp

    image_urls = commodity_image_urls(obj)

    goods_id = str(obj.get("goods_id") or obj.get("selfGoodsId") or "").strip() or "-"
    goods_num = str(obj.get("goodsNum") or "").strip() or "—"

    tag_names: list[str] = []
    tags = obj.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict) and t.get("tagName"):
                tag_names.append(str(t["tagName"]).strip())

    return {
        "title": title,
        "price": price_str,
        "image_urls": image_urls,
        "goods_id": goods_id,
        "goods_num": goods_num,
        "tag_names": tag_names,
    }
