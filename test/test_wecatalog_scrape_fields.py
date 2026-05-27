"""抓取字段解析单测（仅 popUpsInfoV2）。"""

from __future__ import annotations

from product_feed_kr.wecatalog.wecatalog_scrape_fields import (
    fields_from_list_item,
    fields_from_popups_response,
    list_item_scrape_ready,
    merge_list_item_fallback,
    scrape_fields_has_price_cny,
    scrape_no_price_skip_needed,
)


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


def test_scrape_no_price_skip_whitelist(monkeypatch) -> None:
    monkeypatch.setattr(
        "product_feed_kr.listing.listing_llm_enrich._no_price_allow_group_tag_pairs",
        lambda: frozenset({("G1", "T白名单")}),
    )
    fields = {"price_cny": None, "commodity_title": "无标价商品"}
    assert scrape_no_price_skip_needed(
        wecatalog_group="G1",
        wecatalog_tag="T其他",
        scrape_fields=fields,
    )
    assert not scrape_no_price_skip_needed(
        wecatalog_group="G1",
        wecatalog_tag="T白名单",
        scrape_fields=fields,
    )
    fields_priced = {"price_cny": "100"}
    assert not scrape_no_price_skip_needed(
        wecatalog_group="G1",
        wecatalog_tag="T其他",
        scrape_fields=fields_priced,
    )


def test_scrape_fields_has_price_from_title() -> None:
    fields = fields_from_list_item(
        {
            "title": "💰335 短裤",
            "imgsSrc": ["https://example.com/a.jpg"],
        }
    )
    assert scrape_fields_has_price_cny(fields)


def test_album_personal_all_list_item_real_shape():
    """与 album/personal/all 实际列表项字段一致（images 常空、图在 imgsSrc）。"""
    list_it = {
        "goods_id": "_dI0qfD6FBI8YWUGnGmoc1Er281Tw6Y_4DtHyGcg",
        "selfGoodsId": "_dI0qfD6FBI8YWUGnGmoc1Er281Tw6Y_4DtHyGcg",
        "itemNamePrice": 335,
        "optimaPrice": "335",
        "itemPrice": "",
        "priceArr": [{"value": 335, "priceType": 2}],
        "images": [],
        "imgs": ["https://example.com/thumb.jpg?thumbnail/!320x320"],
        "imgsSrc": ["https://example.com/full.jpg"],
        "title": "💰335（TB夏季三色织带双股毛圈棉五分裤）\nTHOM BROWNE …",
        "formats": [
            {"formatType": "1", "formatId": "197312464", "formatName": "0"},
            {"formatType": "1", "formatId": "193595688", "formatName": "1"},
            {"formatType": "1", "formatId": "193595689", "formatName": "2"},
            {"formatType": "1", "formatId": "193595690", "formatName": "3"},
            {"formatType": "1", "formatId": "193595692", "formatName": "4"},
        ],
        "colors": [
            {"formatType": "2", "formatId": 193725966, "formatName": "浅灰"},
            {"formatType": "2", "formatId": 193628024, "formatName": "藏青"},
            {"formatType": "2", "formatId": 193545707, "formatName": "黑色"},
        ],
        "tags": [{"tagId": 90066248, "tagName": "톰브라운Thom Browne"}],
        "goodsNum": "259988",
        "shop_id": "_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg",
    }
    m = fields_from_list_item(list_it)
    assert m["commodity_title"].startswith("💰335")
    assert m["price_cny"] == "335"
    assert m["commodity_image_urls"] == ["https://example.com/full.jpg"]
    assert m["commodity_sizes"] == ["0", "1", "2", "3", "4"]
    assert m["commodity_colors"] == ["浅灰", "藏青", "黑色"]
    assert m["commodity_tag_names"] == ["톰브라운Thom Browne"]
    assert m["commodity_goods_num"] == "259988"
    assert list_item_scrape_ready(list_it, m)


def test_list_item_price_item_name_price_fallback():
    list_it = {
        "title": "无 optimaPrice 仅有 itemNamePrice",
        "itemNamePrice": 299,
        "optimaPrice": "",
        "itemPrice": "",
        "imgsSrc": ["https://example.com/a.jpg"],
    }
    m = fields_from_list_item(list_it)
    assert m["price_cny"] == "299"


def test_list_item_formats_and_colors():
    list_it = {
        "title": "💰335 短裤",
        "optimaPrice": "335",
        "imgsSrc": ["https://example.com/a.jpg"],
        "formats": [
            {"formatName": "0", "formatType": "1"},
            {"formatName": "1", "formatType": "1"},
        ],
        "colors": [
            {"formatName": "浅灰", "formatType": "2"},
            {"formatName": "黑色", "formatType": "2"},
        ],
    }
    m = fields_from_list_item(list_it)
    assert m["commodity_sizes"] == ["0", "1"]
    assert m["commodity_colors"] == ["浅灰", "黑色"]
    assert list_item_scrape_ready(list_it, m)


def test_list_item_title_fallback():
    list_it = {
        "goods_id": "g1",
        "title": "💰335 TB 短裤",
        "imgsSrc": ["https://example.com/a.jpg"],
    }
    m = fields_from_list_item(list_it)
    assert m["commodity_title"] == "💰335 TB 短裤"
    assert m["price_cny"] == "335"
    assert m["commodity_image_urls"] == ["https://example.com/a.jpg"]


def test_merge_list_when_popups_fails():
    list_it = {"title": "列表标题", "optimaPrice": "100"}
    merged, used = merge_list_item_fallback(None, list_it)
    assert used is True
    assert merged is not None
    assert merged["commodity_title"] == "列表标题"
    assert merged["price_cny"] == "100"


def test_merge_list_fills_missing_title_only():
    popups_fields = {
        "commodity_title": "",
        "price_cny": "200",
        "commodity_image_urls": ["https://x/1.jpg"],
        "commodity_tag_names": [],
        "commodity_sizes": ["M"],
        "commodity_colors": [],
        "first_image_hash": None,
        "commodity_goods_num": None,
    }
    list_it = {"title": "补全标题"}
    merged, used = merge_list_item_fallback(popups_fields, list_it)
    assert used is True
    assert merged["commodity_title"] == "补全标题"
    assert merged["price_cny"] == "200"
    assert merged["commodity_sizes"] == ["M"]
