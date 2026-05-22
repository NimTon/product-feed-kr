"""微猫店铺记录 → 上架/LLM 用的 ``commodity`` 风格 dict（由结构化抓取字段组装）。"""

from __future__ import annotations

import json
from typing import Any

from product_feed_kr.wecatalog_scrape_fields import parse_colors_json, parse_sizes_json


def commodity_from_wecatalog_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """用抓取入库的结构化字段（title / 图 / 价）组装 commodity。"""
    title = str(record.get("commodity_title") or "").strip()
    if not title:
        cm = record.get("commodity_min")
        if isinstance(cm, dict):
            title = str(cm.get("title") or "").strip()

    image_urls: list[str] = []
    raw_urls = record.get("commodity_image_urls")
    if isinstance(raw_urls, list):
        image_urls = [str(u).strip() for u in raw_urls if str(u).strip()]
    if not image_urls:
        j = record.get("commodity_image_urls_json")
        if not j and isinstance(record.get("commodity_min"), dict):
            image_urls = list(record["commodity_min"].get("image_urls") or [])
        else:
            image_urls = parse_sizes_json(j)

    if not title:
        return None

    price_raw = record.get("price_cny")
    if price_raw is None and isinstance(record.get("commodity_min"), dict):
        price_raw = record["commodity_min"].get("price_raw")

    ll = record.get("listing_llm")
    if isinstance(ll, dict):
        cp = ll.get("cny_price")
        if cp is not None and str(cp).strip() not in ("", "null"):
            price_raw = str(cp).strip()

    goods_num = str(record.get("commodity_goods_num") or "").strip()
    if not goods_num and isinstance(record.get("commodity_min"), dict):
        goods_num = str(record["commodity_min"].get("goods_num") or "").strip()

    com: dict[str, Any] = {
        "title": title,
        "imgsSrc": image_urls,
        "optimaPrice": str(price_raw).strip() if price_raw is not None else "",
        "goodsNum": goods_num,
    }
    gid = str(record.get("goods_id") or "").strip()
    if gid:
        com["goods_id"] = gid
    return com


def record_scrape_sizes(record: dict[str, Any]) -> list[str]:
    if isinstance(record.get("commodity_sizes"), list):
        return [str(x).strip() for x in record["commodity_sizes"] if str(x).strip()]
    return parse_sizes_json(record.get("commodity_sizes_json"))


def record_scrape_colors(record: dict[str, Any]) -> list[str]:
    if isinstance(record.get("commodity_colors"), list):
        return [str(x).strip() for x in record["commodity_colors"] if str(x).strip()]
    return parse_colors_json(record.get("commodity_colors_json"))


def record_price_cny(record: dict[str, Any]) -> str | None:
    """当前条可用人民币价（``price_cny`` 列或 commodity_min）。"""
    raw = record.get("price_cny")
    if raw is None and isinstance(record.get("commodity_min"), dict):
        raw = record["commodity_min"].get("price_raw")
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("0", "0.0", "-1"):
        return None
    return s


def record_scrape_price_raw(record: dict[str, Any]) -> str | None:
    """兼容旧名，等同 ``record_price_cny``。"""
    return record_price_cny(record)
