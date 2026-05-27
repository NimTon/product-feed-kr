"""列表翻页断点（pf_store_info.stats_json）。"""

from __future__ import annotations

from product_feed_kr.wecatalog.wecatalog_scrape_store import (
    LIST_PAGE_NEXT_TS_KEY,
    LIST_PAGE_NUM_KEY,
    clear_list_progress_in_stats,
    list_progress_from_stats,
    update_list_progress_in_stats,
)


def test_list_progress_roundtrip():
    stats: dict = {}
    raw = {
        "result": {
            "pagination": {
                "isLoadMore": True,
                "pageTimestamp": 1779422753729,
            }
        }
    }
    update_list_progress_in_stats(stats, page_num=42, raw_page=raw)
    assert stats[LIST_PAGE_NUM_KEY] == 42
    assert stats[LIST_PAGE_NEXT_TS_KEY] == 1779422753729
    num, ts = list_progress_from_stats(stats)
    assert num == 42
    assert ts == 1779422753729


def test_list_progress_clears_when_no_more():
    stats = {LIST_PAGE_NUM_KEY: 100, LIST_PAGE_NEXT_TS_KEY: 999}
    raw = {"result": {"pagination": {"isLoadMore": False, "pageTimestamp": 888}}}
    update_list_progress_in_stats(stats, page_num=100, raw_page=raw)
    assert stats[LIST_PAGE_NUM_KEY] == 100
    assert LIST_PAGE_NEXT_TS_KEY not in stats
    assert list_progress_from_stats(stats) == (100, None)


def test_clear_list_progress():
    stats = {LIST_PAGE_NUM_KEY: 5, LIST_PAGE_NEXT_TS_KEY: 1}
    clear_list_progress_in_stats(stats)
    assert stats == {}
