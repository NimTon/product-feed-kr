"""抓取字段解析单测（仅 popUpsInfoV2）。"""

from __future__ import annotations

from product_feed_kr.wecatalog_scrape_fields import fields_from_popups_response


def test_popups_full_fields():
    popups = {
        "success": True,
        "errcode": 0,
        "result": {
            "commodity": {
                "title": "Ball size：35-41",
                "optimaPrice": "540.00",
                "imgsSrc": ["https://example.com/b.jpg"],
                "formats": [
                    {"formatName": "35", "formatType": 1},
                    {"formatName": "36", "formatType": 1},
                ],
                "colors": [],
            }
        },
    }
    m = fields_from_popups_response(popups)
    assert m["commodity_title"] == "Ball size：35-41"
    assert m["price_cny"] == "540"
    assert m["commodity_sizes"] == ["35", "36"]
    assert m["commodity_image_urls"] == ["https://example.com/b.jpg"]


def test_popups_commodity_name_field():
    """popUpsInfoV2 常用 commodityName，不一定有 title。"""
    popups = {
        "success": True,
        "errcode": 0,
        "result": {
            "commodity": {
                "commodityName": "TB 短裤",
                "optimaPrice": "335",
                "imgsSrc": [],
                "formats": [],
                "colors": [],
            }
        },
    }
    m = fields_from_popups_response(popups)
    assert m["commodity_title"] == "TB 短裤"
    assert m["price_cny"] == "335"


def test_popups_empty_response():
    m = fields_from_popups_response(None)
    assert m["commodity_title"] == ""
    assert m["commodity_sizes"] == []
