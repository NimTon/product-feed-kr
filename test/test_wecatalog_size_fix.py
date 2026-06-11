"""爬取尺码修复单测。"""



from __future__ import annotations



from product_feed_kr.wecatalog.wecatalog_size_fix import (
    apply_scrape_size_fix,
    custom_order_sizes_from_title,
    expand_digit_slot_code,
    expand_size_range_token,
    filter_listing_sizes_for_title,
    fix_scrape_sizes,
    shoe_sizes_to_kr_mm,
    standard_stock_sizes_from_title,
)





def test_expand_single_digit():

    assert expand_digit_slot_code("0") == ["S"]

    assert expand_digit_slot_code("4") == ["XXL"]





def test_expand_digit_string():

    assert expand_digit_slot_code("012") == ["S", "M", "L"]





def test_shoe_eu_unchanged_in_clothing_fix():

    assert expand_digit_slot_code("42") is None

    assert expand_digit_slot_code("35") is None

    assert fix_scrape_sizes(["35", "36", "42"]) == ["35", "36", "42"]





def test_fix_scrape_sizes_list():

    assert fix_scrape_sizes(["0", "1", "2", "3", "4"]) == [

        "S",

        "M",

        "L",

        "XL",

        "XXL",

    ]





def test_expand_size_range_clothing():

    assert expand_size_range_token("0-4") == ["0", "1", "2", "3", "4"]

    assert fix_scrape_sizes(["0-4"]) == ["S", "M", "L", "XL", "XXL"]

    assert fix_scrape_sizes(["0~4"]) == ["S", "M", "L", "XL", "XXL"]





def test_expand_size_range_shoe_eu():

    assert expand_size_range_token("38-41") == ["38", "39", "40", "41"]

    assert fix_scrape_sizes(["38-41"]) == ["38", "39", "40", "41"]





def test_apply_scrape_size_fix_shoe_range_no_ko():

    fields = {

        "commodity_title": "运动鞋 男款",

        "commodity_sizes": ["38-41"],

    }

    apply_scrape_size_fix(fields)

    assert fields["commodity_sizes"] == ["38", "39", "40", "41"]

    assert "commodity_sizes_ko" not in fields





def test_fix_combined_digit_string():

    assert fix_scrape_sizes(["1234"]) == ["M", "L", "XL", "XXL"]

    assert fix_scrape_sizes(["01234"]) == ["S", "M", "L", "XL", "XXL"]





def test_shoe_eu_to_kr_mm():

    assert shoe_sizes_to_kr_mm(["35", "36", "42"]) == ["225", "230", "265"]





def test_apply_scrape_size_fix_shoe_no_ko():

    fields = {

        "commodity_title": "Balenciaga 老爹鞋",

        "commodity_sizes": ["35", "36", "37"],

    }

    apply_scrape_size_fix(fields)

    assert fields["commodity_sizes"] == ["35", "36", "37"]

    assert "commodity_sizes_ko" not in fields





def test_apply_scrape_size_fix_clothing():

    fields = {

        "commodity_title": "TB 短裤",

        "commodity_sizes": ["01234"],

    }

    apply_scrape_size_fix(fields)

    assert fields["commodity_sizes"] == ["S", "M", "L", "XL", "XXL"]

    assert "commodity_sizes_ko" not in fields


BRUNELLO_TITLE = (
    "P650 BC新品板鞋 Brunel*o Cucinell* 新款运动鞋男鞋出货！"
    "标准专柜码数：39～44# （38.45.46# 🉑️定做)"
)


def test_expand_fullwidth_tilde_range():
    assert fix_scrape_sizes(["39～44#"]) == ["39", "40", "41", "42", "43", "44"]


def test_custom_order_sizes_from_title():
    assert custom_order_sizes_from_title(BRUNELLO_TITLE) == frozenset({"38", "45", "46"})


def test_standard_stock_sizes_from_title():
    assert standard_stock_sizes_from_title(BRUNELLO_TITLE) == [
        "39",
        "40",
        "41",
        "42",
        "43",
        "44",
    ]


def test_filter_listing_sizes_strips_custom_and_keeps_standard():
    llm_sizes = ["38", "39", "40", "41", "42", "43", "44", "45", "46"]
    assert filter_listing_sizes_for_title(llm_sizes, BRUNELLO_TITLE) == [
        "39",
        "40",
        "41",
        "42",
        "43",
        "44",
    ]