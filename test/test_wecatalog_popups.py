"""popUpsInfoV2 解析单测。"""
from __future__ import annotations

from product_feed_kr.wecatalog_popups import (
    extract_format_options,
    popups_optima_price_cny,
    popups_response_ready,
)


def _sample() -> dict:
    return {
        "success": True,
        "errcode": 0,
        "result": {
            "commodity": {
                "optimaPrice": "540.00",
                "formats": [
                    {"formatName": "35", "formatId": 193486904, "formatType": 1},
                    {"formatName": "36", "formatId": 193486912, "formatType": 1},
                    {"formatName": "41", "formatId": 193486928, "formatType": 1},
                ],
                "colors": [],
                "skus": [{"skuName": "35"}, {"skuName": "41"}],
            }
        },
    }


def test_popups_sizes_and_price():
    resp = _sample()
    assert popups_response_ready(resp)
    opts = extract_format_options(resp)
    assert opts["sizes"] == ["35", "36", "41"]
    assert opts["colors"] == []
    assert popups_optima_price_cny(resp) == "540"
