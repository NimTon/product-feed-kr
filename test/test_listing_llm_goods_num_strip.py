"""LLM 文案剔除货号/款号数字。"""

from __future__ import annotations

from product_feed_kr.listing.listing_llm_enrich import strip_listing_llm_goods_num_refs


def test_strip_goods_num_from_desc() -> None:
    record = {"commodity_goods_num": "790038", "commodity_title": "790038"}
    payload = {
        "name_zh": "古驰女士手提包 790038 款",
        "desc_zh": "型号为 790038，彰显品牌独特风格。",
        "desc_ko": "모델 번호 790038 은 브랜드의 독특한 스타일을 나타냅니다.",
    }
    strip_listing_llm_goods_num_refs(record, payload, listing_hint="790038")
    assert "790038" not in payload["name_zh"]
    assert "790038" not in payload["desc_zh"]
    assert "790038" not in payload["desc_ko"]
    assert "古驰" in payload["name_zh"]
    assert "브랜드" in payload["desc_ko"]
