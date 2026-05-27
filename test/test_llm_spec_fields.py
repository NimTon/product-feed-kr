"""规格拆列：LLM 纠错回写 commodity_*。"""
from __future__ import annotations

from unittest.mock import patch

from product_feed_kr.db.llm_spec_fields import spec_columns_from_listing_llm


@patch("product_feed_kr.listing.listing_llm_enrich.listing_llm_color_vision_enabled", return_value=True)
def test_llm_overwrites_scrape_sizes_and_colors(_mock_colors: object) -> None:
    """LLM 有 attr_map 时覆盖库内爬取的中文尺码/颜色。"""
    ll = {
        "attr_map": {"尺码": ["40", "41"], "颜色": ["白"]},
        "attr_map_ko": {"사이즈": ["250", "255"], "색상": ["화이트"]},
    }
    row = {"commodity_sizes_json": '["36","37"]', "commodity_colors_json": '["黑"]'}
    cols = spec_columns_from_listing_llm(ll, row)
    assert cols["commodity_sizes_json"] == '["40", "41"]'
    assert cols["commodity_colors_json"] == '["白"]'
    assert "250" in (cols["sizes_ko_json"] or "")


@patch("product_feed_kr.listing.listing_llm_enrich.listing_llm_color_vision_enabled", return_value=True)
def test_llm_fills_commodity_when_no_scrape(_mock_colors: object) -> None:
    ll = {"attr_map": {"尺码": ["40"], "颜色": ["白"]}, "attr_map_ko": {"사이즈": ["250"], "색상": ["화이트"]}}
    row = {"commodity_sizes_json": None, "commodity_colors_json": None}
    cols = spec_columns_from_listing_llm(ll, row)
    assert cols["commodity_sizes_json"] == '["40"]'
    assert cols["commodity_colors_json"] == '["白"]'
    assert "250" in (cols["sizes_ko_json"] or "")


@patch("product_feed_kr.listing.listing_llm_enrich.listing_llm_color_vision_enabled", return_value=False)
def test_llm_overwrites_sizes_when_vision_off(_mock_colors: object) -> None:
    ll = {
        "attr_map": {"尺码": ["S", "M", "L"], "颜色": ["白色", "蓝色"]},
        "attr_map_ko": {"색상": ["화이트", "블루"]},
    }
    row = {"commodity_sizes_json": '["0","1","2","3","4"]', "commodity_colors_json": '["黑"]'}
    cols = spec_columns_from_listing_llm(ll, row)
    assert cols["commodity_sizes_json"] == '["S", "M", "L"]'
    assert cols["commodity_colors_json"] == '["白色", "蓝色"]'


@patch("product_feed_kr.listing.listing_llm_enrich.listing_llm_color_vision_enabled", return_value=False)
def test_llm_no_sizes_leaves_commodity_sizes_null(_mock_colors: object) -> None:
    """LLM 未输出尺码时不改 commodity_sizes_json（UPDATE COALESCE 保留原值）。"""
    ll = {"attr_map": {"颜色": ["白"]}, "attr_map_ko": {"색상": ["화이트"]}}
    row = {"commodity_sizes_json": '["36"]', "commodity_colors_json": None}
    cols = spec_columns_from_listing_llm(ll, row)
    assert cols["commodity_sizes_json"] is None
    assert cols["commodity_colors_json"] == '["白"]'
