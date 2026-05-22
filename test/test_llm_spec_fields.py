"""规格拆列：中文 commodity_*、韩文 *_ko_json。"""
from __future__ import annotations

from product_feed_kr.llm_spec_fields import (
    effective_sizes_colors_zh,
    spec_columns_from_listing_llm,
)


def test_scrape_wins_over_llm_sizes():
    ll = {"attr_map": {"尺码": ["99"], "颜色": ["红"]}, "attr_map_ko": {"사이즈": ["99"]}}
    row = {"commodity_sizes_json": '["36","37"]', "commodity_colors_json": '["黑"]'}
    cols = spec_columns_from_listing_llm(ll, row)
    assert cols["commodity_sizes_json"] is None
    assert cols["commodity_colors_json"] is None
    sizes, colors = effective_sizes_colors_zh(row)
    assert sizes == ["36", "37"]
    assert colors == ["黑"]


def test_llm_fills_commodity_when_no_scrape():
    ll = {"attr_map": {"尺码": ["40"], "颜色": ["白"]}, "attr_map_ko": {"사이즈": ["250"], "색상": ["화이트"]}}
    row = {"commodity_sizes_json": None, "commodity_colors_json": None}
    cols = spec_columns_from_listing_llm(ll, row)
    assert cols["commodity_sizes_json"] == '["40"]'
    assert cols["commodity_colors_json"] == '["白"]'
    assert "250" in (cols["sizes_ko_json"] or "")
