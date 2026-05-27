"""微猫抓取结果 → 入库用结构化字段（不保存 detail/popups 原始 JSON）。

``album/personal/all`` 列表项字段映射（勿与 popUps 内层 commodity 混淆）::

  goods_id / selfGoodsId     → 商品 ID（不用 parent_goods_id 除非前两者缺失）
  title                      → 标题（含 💰 标价行）
  optimaPrice                → 主价格（字符串，如 ``"335"``）
  itemNamePrice              → 标题价回退（数字，``fillPriceWithItemNamePrice`` 时有效）
  priceArr[0].value          → 价目表回退
  itemPrice                  → 常为空，**不读**
  imgsSrc                    → 原图 URL（优先）
  imgs                       → 缩略图 URL（imgsSrc 无时）
  images                     → 常为 ``[]``，**不读**
  formats (formatType 1)     → 尺码
  colors (formatType 2)      → 颜色
  time_stamp                 → 微猫上架时间（毫秒，入库 ``wecatalog_listed_at``）
  tags[].tagId / tagName     → 店铺标签
  goodsNum                   → 货号
  shop_id / selfShopId       → 店铺 ID
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from product_feed_kr.wecatalog.wecatalog_popups import (
    extract_format_options,
    extract_format_options_from_commodity,
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


def _image_urls_from_list_item(item: dict[str, Any]) -> list[str]:
    urls = _image_urls_from_commodity(item)
    if urls:
        return urls
    for key in ("cover", "img", "image", "thumbnail", "coverImg"):
        v = item.get(key)
        if isinstance(v, str):
            s = v.strip().split("|")[0].strip()
            if s.startswith(("http://", "https://")):
                return [s]
    return []


def _title_from_item_dict(item: dict[str, Any]) -> str:
    return str(
        item.get("title")
        or item.get("commodityName")
        or item.get("commodityTitle")
        or item.get("name")
        or ""
    ).strip()


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
        "wecatalog_listed_at": None,
    }


def _price_from_detail_commodity(commodity: dict[str, Any]) -> str | None:
    raw_price = commodity.get("optimaPrice")
    if raw_price is not None and str(raw_price).strip() not in ("", "-1"):
        s = str(raw_price).strip()
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or None
    name_price = commodity.get("itemNamePrice")
    if name_price is not None and str(name_price).strip() not in ("", "-1"):
        return str(name_price).strip()
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

    title = str(
        commodity.get("title")
        or commodity.get("commodityName")
        or commodity.get("name")
        or ""
    ).strip()
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

    from product_feed_kr.wecatalog.wecatalog_listed_at import wecatalog_listed_at_iso_from_list_item

    return {
        "commodity_title": title,
        "price_cny": _price_from_detail_commodity(commodity),
        "commodity_goods_num": str(commodity.get("goodsNum") or "").strip() or None,
        "commodity_image_urls": image_urls,
        "commodity_tag_names": tag_names,
        "commodity_sizes": [],
        "commodity_colors": [],
        "first_image_hash": first_image_hash,
        "wecatalog_listed_at": wecatalog_listed_at_iso_from_list_item(commodity),
    }


def detail_response_ready(resp: Any) -> bool:
    """``commodity/view`` 成功且含 ``result.commodity``。"""
    if not isinstance(resp, dict) or resp.get("errcode") not in (0, None):
        return False
    result = resp.get("result")
    if not isinstance(result, dict):
        return False
    return isinstance(result.get("commodity"), dict)


def fields_from_detail_response(detail_response: dict[str, Any] | None) -> dict[str, Any]:
    """从 ``commodity/view`` 提取 title / 图 / 价（无 popUps 尺码/颜色维度）。"""
    if not detail_response_ready(detail_response):
        return _empty_scrape_fields()
    assert isinstance(detail_response, dict)
    result = detail_response.get("result")
    assert isinstance(result, dict)
    com = result.get("commodity")
    return fields_from_commodity_dict(com if isinstance(com, dict) else None)


def list_item_scrape_ready(
    list_item: dict[str, Any] | None,
    fields: dict[str, Any] | None = None,
) -> bool:
    """列表项是否足以入库（有标题且至少一张图）。"""
    if not isinstance(list_item, dict):
        return False
    f = fields if fields is not None else fields_from_list_item(list_item)
    if not str(f.get("commodity_title") or "").strip():
        return False
    urls = f.get("commodity_image_urls") or []
    return bool(urls)


def scrape_fields_has_price_cny(fields: dict[str, Any] | None) -> bool:
    """抓取字段是否含可用人民币价（含标题 💰 解析结果）。"""
    from product_feed_kr.listing.listing_llm_enrich import _cny_price_field_usable

    if not isinstance(fields, dict):
        return False
    return _cny_price_field_usable(fields.get("price_cny"))


def scrape_no_price_skip_needed(
    *,
    wecatalog_group: str,
    wecatalog_tag: str,
    scrape_fields: dict[str, Any],
) -> bool:
    """无价且 (分组, 标签) 不在 ``SEVEN17_NO_PRICE_ALLOW_CATEGORIES`` 白名单 → 应跳过。"""
    if scrape_fields_has_price_cny(scrape_fields):
        return False
    from product_feed_kr.listing.listing_llm_enrich import record_is_no_price_allowed_by_map_category

    stub = {
        "wecatalog_group": str(wecatalog_group or "").strip(),
        "wecatalog_tag": str(wecatalog_tag or "").strip(),
    }
    return not record_is_no_price_allowed_by_map_category(stub)


def fields_from_list_item(list_item: dict[str, Any] | None) -> dict[str, Any]:
    """从 ``album/personal/all`` 列表项提取 title / 图 / 价 / 尺码 / 颜色。"""
    if not isinstance(list_item, dict):
        return _empty_scrape_fields()
    out = fields_from_commodity_dict(list_item)
    title = _title_from_item_dict(list_item)
    if title and not str(out.get("commodity_title") or "").strip():
        out["commodity_title"] = title
    image_urls = _image_urls_from_list_item(list_item)
    if image_urls and not out.get("commodity_image_urls"):
        out["commodity_image_urls"] = image_urls
        out["first_image_hash"] = hashlib.sha1(
            image_urls[0].encode("utf-8", errors="ignore"),
        ).hexdigest()
    if not out.get("price_cny") and title:
        from product_feed_kr.wego.wego_commodity import price_from_title_cny

        tp = price_from_title_cny(title)
        if tp:
            out["price_cny"] = tp
    if not out.get("commodity_tag_names"):
        tag_names: list[str] = []
        for t in list_item.get("tags") or []:
            if isinstance(t, dict) and t.get("tagName"):
                tag_names.append(str(t["tagName"]).strip())
        if tag_names:
            out["commodity_tag_names"] = tag_names
    opts = extract_format_options_from_commodity(list_item)
    if opts.get("sizes"):
        out["commodity_sizes"] = list(opts["sizes"])
    if opts.get("colors"):
        out["commodity_colors"] = list(opts["colors"])
    return out


def merge_list_item_fallback(
    scrape_fields: dict[str, Any] | None,
    list_item: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """popUps 失败或缺 title 时，用列表项补全；返回 (字段, 是否使用了列表兜底)。"""
    fb = fields_from_list_item(list_item)
    if not str(fb.get("commodity_title") or "").strip():
        return scrape_fields, False
    if scrape_fields is None:
        return fb, True
    out = dict(scrape_fields)
    used = False
    if not str(out.get("commodity_title") or "").strip():
        out["commodity_title"] = fb["commodity_title"]
        used = True
    for key in ("price_cny", "commodity_goods_num", "first_image_hash"):
        if not out.get(key) and fb.get(key):
            out[key] = fb[key]
            used = True
    for key in ("commodity_image_urls", "commodity_tag_names", "commodity_sizes", "commodity_colors"):
        cur = out.get(key) or []
        fb_val = fb.get(key) or []
        if not cur and fb_val:
            out[key] = list(fb_val)
            used = True
    return out, used


def fields_from_popups_response(popups_response: dict[str, Any] | None) -> dict[str, Any]:
    """从 ``popUpsInfoV2`` 提取 title / 图 / 价 / 尺码 / 颜色（与 detail 同结构的 commodity）。"""
    from product_feed_kr.wecatalog.wecatalog_popups import popups_commodity

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


def apply_price_krw_from_cny(
    fields: dict[str, Any],
    *,
    krw_per_cny: float | None,
) -> None:
    """按抓取时汇率将 ``price_cny`` 换算为 ``price_krw``（千韩元取整）。"""
    if krw_per_cny is None or krw_per_cny <= 0:
        fields["price_krw"] = None
        return
    from product_feed_kr.common.cny_krw_rate import cny_amount_to_krw_won_str

    fields["price_krw"] = cny_amount_to_krw_won_str(
        str(fields.get("price_cny") or ""),
        float(krw_per_cny),
    )


def scrape_fields_to_db_columns(fields: dict[str, Any]) -> dict[str, Any]:
    """扁平字段 → SQLite 列值（含 JSON 列字符串）。"""
    urls = fields.get("commodity_image_urls") or []
    tags = fields.get("commodity_tag_names") or []
    sizes = fields.get("commodity_sizes") or []
    sizes_ko = fields.get("commodity_sizes_ko") or []
    colors = fields.get("commodity_colors") or []
    return {
        "commodity_title": str(fields.get("commodity_title") or "").strip(),
        "price_cny": fields.get("price_cny"),
        "price_krw": fields.get("price_krw"),
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
        "sizes_ko_json": (
            json.dumps(sizes_ko, ensure_ascii=False) if sizes_ko else None
        ),
        "first_image_hash": fields.get("first_image_hash"),
        "wecatalog_listed_at": fields.get("wecatalog_listed_at"),
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


def attach_scrape_fields_to_record(
    record: dict[str, Any],
    fields: dict[str, Any],
    *,
    krw_per_cny: float | None = None,
) -> None:
    """写入内存 record（供 LLM / 上架读取，不含原始 API 包）。"""
    from product_feed_kr.wecatalog.wecatalog_size_fix import apply_scrape_size_fix

    apply_price_krw_from_cny(fields, krw_per_cny=krw_per_cny)
    apply_scrape_size_fix(fields)
    cols = scrape_fields_to_db_columns(fields)
    record["commodity_title"] = cols["commodity_title"]
    record["price_cny"] = cols["price_cny"]
    record["price_krw"] = cols.get("price_krw")
    record["commodity_goods_num"] = cols["commodity_goods_num"]
    record["commodity_image_urls"] = parse_sizes_json(cols.get("commodity_image_urls_json"))
    record["commodity_tag_names"] = parse_sizes_json(cols.get("commodity_tag_names_json"))
    record["commodity_sizes"] = parse_sizes_json(cols.get("commodity_sizes_json"))
    record["commodity_colors"] = parse_sizes_json(cols.get("commodity_colors_json"))
    record["sizes_ko_json"] = cols.get("sizes_ko_json")
    record["first_image_hash"] = cols.get("first_image_hash")
    record["wecatalog_listed_at"] = cols.get("wecatalog_listed_at")
    record.pop("detail_response", None)
    record.pop("popups_response", None)
