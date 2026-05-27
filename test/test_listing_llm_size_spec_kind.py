"""LLM size_spec_kind 与鞋类毫米换算。"""

from __future__ import annotations

from product_feed_kr.listing.listing_llm_enrich import (
    apply_listing_size_fix_from_zh,
    listing_llm_wants_shoe_size_mm,
    normalize_size_spec_kind,
    parse_listing_llm_response,
)


def test_normalize_size_spec_kind_aliases():
    assert normalize_size_spec_kind("footwear") == "footwear"
    assert normalize_size_spec_kind("鞋靴") == "footwear"
    assert normalize_size_spec_kind("apparel") == "apparel"
    assert normalize_size_spec_kind("服装") == "apparel"
    assert normalize_size_spec_kind("") is None
    assert normalize_size_spec_kind("unknown") is None


def test_listing_llm_wants_shoe_size_mm():
    assert listing_llm_wants_shoe_size_mm({"size_spec_kind": "footwear"}) is True
    assert listing_llm_wants_shoe_size_mm({"size_spec_kind": "apparel"}) is False
    assert listing_llm_wants_shoe_size_mm({}) is False


def test_apply_listing_size_fix_footwear_forces_mm():
    payload = {
        "size_spec_kind": "footwear",
        "attr_map": {"尺码": ["39", "40", "41"]},
        "attr_map_ko": {},
    }
    apply_listing_size_fix_from_zh(payload)
    assert payload["attr_map_ko"]["사이즈"] == ["245", "250", "260"]


def test_apply_listing_size_fix_without_kind_copies_zh():
    payload = {
        "attr_map": {"尺码": ["39", "40", "41"]},
        "attr_map_ko": {},
    }
    apply_listing_size_fix_from_zh(payload)
    assert payload["attr_map_ko"]["사이즈"] == ["39", "40", "41"]


def test_apply_listing_size_fix_apparel_keeps_letters():
    payload = {
        "size_spec_kind": "apparel",
        "attr_map": {"尺码": ["0", "1", "2"]},
        "attr_map_ko": {},
    }
    apply_listing_size_fix_from_zh(payload, listing_hint="TB 短裤")
    assert payload["attr_map"]["尺码"] == ["S", "M", "L"]
    assert payload["attr_map_ko"]["사이즈"] == ["S", "M", "L"]


def test_parse_listing_llm_response_keeps_size_spec_kind():
    raw = '{"size_spec_kind": "footwear", "name_zh": "x", "attr_map": {"尺码": ["40"]}}'
    out = parse_listing_llm_response(raw)
    assert out.get("size_spec_kind") == "footwear"
