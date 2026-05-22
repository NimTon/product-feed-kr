"""微猫抓取结果 → 入库用结构化字段（不保存 detail/popups 原始 JSON）。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from product_feed_kr.wecatalog_popups import (
    extract_format_options,
    popups_optima_price_cny,
    popups_response_ready,
)


def _image_urls_from_commodity(commodity: dict[str, Any]) -> list[str]:
    urls_raw = commodity.get("imgsSrc") or commodity.get("imgs") or []
    if not isinstance(urls_raw, list):
        return []
    out: list[str] = []
    for u in urls_raw:
        if not isinstance(u, str):
            continue
        s = u.strip().split("|")[0].strip()
        if s.startswith(("http://", "https://")):
            out.append(s)
    return out


def _empty_scrape_fields() -> dict[str, Any]:
    return {
        "commodity_title": "",
        "price_cny": None,
        "commodity_goods_num": None,
        "commodity_image_urls": [],
        "commodity_tag_names": [],
        "commodity_sizes": [],
        "commodity_colors": [],
        "first_image_hash": None,
    }


def _price_from_detail_commodity(commodity: dict[str, Any]) -> str | None:
    raw_price = commodity.get("optimaPrice")
    if raw_price is not None and str(raw_price).strip() not in ("", "-1"):
        s = str(raw_price).strip()
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or None
    arr = commodity.get("priceArr") or []
    if isinstance(arr, list) and arr:
        first = arr[0]
        if isinstance(first, dict):
            v = first.get("value")
            if v is not None and str(v).strip() not in ("", "-1"):
                return str(v).strip()
    return None


def fields_from_commodity_dict(commodity: dict[str, Any] | None) -> dict[str, Any]:
    """从 ``result.commodity``（detail 或 popUps 共用）提取 title / 价 / 图 / 货号 / 标签。"""
    empty = _empty_scrape_fields()
    if not isinstance(commodity, dict):
        return empty

    title = str(commodity.get("title") or "").strip()
    image_urls = _image_urls_from_commodity(commodity)
    first_image_hash: str | None = None
    if image_urls:
        first_image_hash = hashlib.sha1(
            image_urls[0].encode("utf-8", errors="ignore"),
        ).hexdigest()

    tag_names: list[str] = []
    for t in commodity.get("tags") or []:
        if isinstance(t, dict) and t.get("tagName"):
            tag_names.append(str(t["tagName"]).strip())

    return {
        "commodity_title": title,
        "price_cny": _price_from_detail_commodity(commodity),
        "commodity_goods_num": str(commodity.get("goodsNum") or "").strip() or None,
        "commodity_image_urls": image_urls,
        "commodity_tag_names": tag_names,
        "commodity_sizes": [],
        "commodity_colors": [],
        "first_image_hash": first_image_hash,
    }


def fields_from_popups_response(popups_response: dict[str, Any] | None) -> dict[str, Any]:
    """从 ``popUpsInfoV2`` 提取 title / 图 / 价 / 尺码 / 颜色（与 detail 同结构的 commodity）。"""
    from product_feed_kr.wecatalog_popups import popups_commodity

    if not popups_response_ready(popups_response):
        return _empty_scrape_fields()
    com = popups_commodity(popups_response)
    out = fields_from_commodity_dict(com)
    opts = extract_format_options(popups_response)
    if opts.get("sizes"):
        out["commodity_sizes"] = list(opts["sizes"])
    if opts.get("colors"):
        out["commodity_colors"] = list(opts["colors"])
    p = popups_optima_price_cny(popups_response)
    if p:
        out["price_cny"] = p
    return out


def scrape_fields_to_db_columns(fields: dict[str, Any]) -> dict[str, Any]:
    """扁平字段 → SQLite 列值（含 JSON 列字符串）。"""
    urls = fields.get("commodity_image_urls") or []
    tags = fields.get("commodity_tag_names") or []
    sizes = fields.get("commodity_sizes") or []
    colors = fields.get("commodity_colors") or []
    return {
        "commodity_title": str(fields.get("commodity_title") or "").strip(),
        "price_cny": fields.get("price_cny"),
        "commodity_goods_num": fields.get("commodity_goods_num"),
        "commodity_image_urls_json": (
            json.dumps(urls, ensure_ascii=False) if urls else None
        ),
        "commodity_tag_names_json": (
            json.dumps(tags, ensure_ascii=False) if tags else None
        ),
        "commodity_sizes_json": (
            json.dumps(sizes, ensure_ascii=False) if sizes else None
        ),
        "commodity_colors_json": (
            json.dumps(colors, ensure_ascii=False) if colors else None
        ),
        "first_image_hash": fields.get("first_image_hash"),
    }


def parse_sizes_json(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    return []


def parse_colors_json(raw: Any) -> list[str]:
    return parse_sizes_json(raw)


def attach_scrape_fields_to_record(record: dict[str, Any], fields: dict[str, Any]) -> None:
    """写入内存 record（供 LLM / 上架读取，不含原始 API 包）。"""
    cols = scrape_fields_to_db_columns(fields)
    record["commodity_title"] = cols["commodity_title"]
    record["price_cny"] = cols["price_cny"]
    record["commodity_goods_num"] = cols["commodity_goods_num"]
    record["commodity_image_urls"] = parse_sizes_json(cols.get("commodity_image_urls_json"))
    record["commodity_tag_names"] = parse_sizes_json(cols.get("commodity_tag_names_json"))
    record["commodity_sizes"] = parse_sizes_json(cols.get("commodity_sizes_json"))
    record["commodity_colors"] = parse_sizes_json(cols.get("commodity_colors_json"))
    record["first_image_hash"] = cols.get("first_image_hash")
    record.pop("detail_response", None)
    record.pop("popups_response", None)
