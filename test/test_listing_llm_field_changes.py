"""listing_llm 字段变更日志摘要。"""

from product_feed_kr.listing.listing_llm_enrich import listing_llm_field_changes


def test_listing_llm_field_changes_detects_scalar_and_attr() -> None:
    before = {
        "name_ko": "旧名",
        "attr_map": {"尺码": ["36"]},
    }
    after = {
        "name_ko": "새 이름",
        "attr_map": {"尺码": ["35", "36"], "颜色": ["白"]},
        "attr_map_ko": {"색상": ["화이트"]},
    }
    s = listing_llm_field_changes(before, after)
    assert "name_ko:旧名→" in s
    assert "attr_map.尺码:" in s
    assert "attr_map.颜色:∅→" in s
    assert "attr_map_ko.색상:∅→" in s


def test_listing_llm_field_changes_no_change() -> None:
    d = {"name_zh": "同", "attr_map": {"尺码": ["M"]}}
    assert listing_llm_field_changes(d, dict(d)) == "(无变化)"
