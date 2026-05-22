"""商品规格：中文 ``commodity_sizes_json`` / ``commodity_colors_json``（爬取 + 缺时 LLM 写入同列）；韩文 ``*_ko_json``。"""

from __future__ import annotations

import json
from typing import Any

SIZE_ZH = "尺码"
COLOR_ZH = "颜色"
SIZE_KO = "사이즈"
COLOR_KO = "색상"


def parse_json_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    try:
        if isinstance(raw, str):
            data = json.loads(raw) if raw.strip() else []
        elif isinstance(raw, list):
            data = raw
        else:
            return []
        if not isinstance(data, list):
            return []
        return [str(x).strip() for x in data if str(x).strip()]
    except (json.JSONDecodeError, TypeError):
        return []


def _list_from_attr(attr: dict[str, Any], key: str) -> list[str]:
    raw = attr.get(key)
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if raw is not None and str(raw).strip():
        return [str(raw).strip()]
    return []


def lists_from_attr_map(attr: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    am = attr or {}
    return _list_from_attr(am, SIZE_ZH), _list_from_attr(am, COLOR_ZH)


def lists_from_attr_map_ko(attr: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    am = attr or {}
    return _list_from_attr(am, SIZE_KO), _list_from_attr(am, COLOR_KO)


def dumps_json_list(items: list[str] | None) -> str | None:
    if not items:
        return None
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return None
    return json.dumps(cleaned, ensure_ascii=False)


def zh_sizes_colors_from_row(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """中文尺码/颜色（``commodity_sizes_json`` / ``commodity_colors_json``）。"""
    return (
        parse_json_str_list(row.get("commodity_sizes_json")),
        parse_json_str_list(row.get("commodity_colors_json")),
    )


def effective_sizes_colors_zh(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """中文规格：``commodity_sizes_json`` / ``commodity_colors_json``。"""
    return zh_sizes_colors_from_row(row)


def effective_sizes_colors_ko(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """韩文规格：``sizes_ko_json`` / ``colors_ko_json``。"""
    return (
        parse_json_str_list(row.get("sizes_ko_json")),
        parse_json_str_list(row.get("colors_ko_json")),
    )


def listing_llm_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """从扁平行组装内存 ``listing_llm``。"""
    from product_feed_kr.listing_llm_enrich import _cny_price_field_usable

    lp = str(row.get("llm_processed_at") or "").strip()
    nzh = str(row.get("llm_name_zh") or "").strip()
    nko = str(row.get("llm_name_ko") or "").strip()
    dzh = str(row.get("llm_desc_zh") or "").strip()
    dko = str(row.get("llm_desc_ko") or "").strip()
    src0 = str(row.get("llm_source") or "").strip()
    rsn0 = str(row.get("llm_reason") or "").strip()
    if not any((lp, nzh, nko, dzh, dko, src0, rsn0)) and not _cny_price_field_usable(
        row.get("price_cny"),
    ):
        return None

    ll: dict[str, Any] = {}
    if lp:
        ll["processed_at"] = lp
    if nzh:
        ll["name_zh"] = nzh
    if nko:
        ll["name_ko"] = nko
    if dzh:
        ll["desc_zh"] = dzh
    if dko:
        ll["desc_ko"] = dko

    cp = row.get("price_cny")
    if _cny_price_field_usable(cp):
        ll["cny_price"] = str(cp).strip()

    src = str(row.get("llm_source") or "").strip()
    if src:
        ll["source"] = src
    elif lp and nzh:
        ll["source"] = "openai"

    reason = str(row.get("llm_reason") or "").strip()
    if reason:
        ll["reason"] = reason

    attr_zh, attr_ko = listing_llm_attr_maps_from_row(row)
    if attr_zh:
        ll["attr_map"] = attr_zh
    if attr_ko:
        ll["attr_map_ko"] = attr_ko
    return ll


def attr_map_zh_from_lists(sizes: list[str], colors: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if sizes:
        out[SIZE_ZH] = list(sizes)
    if colors:
        out[COLOR_ZH] = list(colors)
    return out


def attr_map_ko_from_lists(sizes_ko: list[str], colors_ko: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if sizes_ko:
        out[SIZE_KO] = list(sizes_ko)
    if colors_ko:
        out[COLOR_KO] = list(colors_ko)
    return out


def spec_columns_from_listing_llm(
    ll: dict[str, Any] | None,
    record: dict[str, Any] | None = None,
) -> dict[str, str | None]:
    """LLM 写库：缺中文尺码/颜色时写入 ``commodity_*``（与爬取同列）；韩文写 ``*_ko_json``。"""
    empty = {
        "commodity_sizes_json": None,
        "commodity_colors_json": None,
        "sizes_ko_json": None,
        "colors_ko_json": None,
    }
    if not isinstance(ll, dict):
        return empty

    row = record if isinstance(record, dict) else {}
    zh_sizes, zh_colors = zh_sizes_colors_from_row(row)
    ll_sizes, ll_colors = lists_from_attr_map(
        ll.get("attr_map") if isinstance(ll.get("attr_map"), dict) else None,
    )
    ll_sizes_ko, ll_colors_ko = lists_from_attr_map_ko(
        ll.get("attr_map_ko") if isinstance(ll.get("attr_map_ko"), dict) else None,
    )

    fill_sizes: list[str] = []
    fill_colors: list[str] = []
    if not zh_sizes and ll_sizes:
        fill_sizes = ll_sizes
    if not zh_colors and ll_colors:
        fill_colors = ll_colors

    return {
        "commodity_sizes_json": dumps_json_list(fill_sizes),
        "commodity_colors_json": dumps_json_list(fill_colors),
        "sizes_ko_json": dumps_json_list(ll_sizes_ko),
        "colors_ko_json": dumps_json_list(ll_colors_ko),
    }


def listing_llm_attr_maps_from_row(row: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """供上架组装 attr_map。"""
    sizes, colors = effective_sizes_colors_zh(row)
    sizes_ko, colors_ko = effective_sizes_colors_ko(row)
    return attr_map_zh_from_lists(sizes, colors), attr_map_ko_from_lists(sizes_ko, colors_ko)
