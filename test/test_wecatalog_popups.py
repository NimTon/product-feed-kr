"""popUpsInfoV2 解析单测。"""
from __future__ import annotations

from product_feed_kr.wecatalog.wecatalog_popups import (
    POPUPS_ERR_COMMODITY_INVALID,
    POPUPS_ERR_LOGIN_EXPIRED,
    extract_format_options,
    popups_errcode,
    popups_errmsg,
    popups_optima_price_cny,
    popups_response_ready,
    popups_skip_permanent,
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


def test_popups_errcodes():
    assert popups_errcode({"errcode": 2530002, "errmsg": "商品已失效"}) == POPUPS_ERR_COMMODITY_INVALID
    assert popups_errmsg({"errcode": 9, "errmsg": "登录已过期，请重新登录。"}) == "登录已过期，请重新登录。"
    assert popups_skip_permanent(POPUPS_ERR_COMMODITY_INVALID)
    assert not popups_skip_permanent(POPUPS_ERR_LOGIN_EXPIRED)


def test_popups_sizes_and_price():
    resp = _sample()
    assert popups_response_ready(resp)
    opts = extract_format_options(resp)
    assert opts["sizes"] == ["35", "36", "41"]
    assert opts["colors"] == []
    assert popups_optima_price_cny(resp) == "540"
