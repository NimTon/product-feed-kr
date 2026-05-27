"""列表翻页上限 normalize。"""

from __future__ import annotations

from product_feed_kr.wecatalog.wecatalog_scrape_store import normalize_max_list_pages


def test_normalize_max_list_pages_unlimited():
    assert normalize_max_list_pages(-1) == -1
    assert normalize_max_list_pages(0) == -1
    assert normalize_max_list_pages(None) == -1


def test_normalize_max_list_pages_capped():
    assert normalize_max_list_pages(500) == 500
    assert normalize_max_list_pages(1) == 1
