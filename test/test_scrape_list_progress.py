"""按标签列表翻页断点（pf_store_info.stats_json.tag_progress）。"""

from __future__ import annotations

from product_feed_kr.wecatalog.wecatalog_scrape_store import (
    TAG_DONE_KEY,
    TAG_PAGE_NEXT_TS_KEY,
    TAG_PAGE_NUM_KEY,
    TAG_PROGRESS_KEY,
    clear_all_tag_progress,
    tag_is_done,
    tag_progress_from_stats,
    update_tag_progress_in_stats,
)


def test_tag_progress_roundtrip():
    stats: dict = {}
    raw = {
        "result": {
            "pagination": {
                "isLoadMore": True,
                "pageTimestamp": 1779422753729,
            }
        }
    }
    update_tag_progress_in_stats(stats, tag_id=90066248, page_num=3, raw_page=raw)
    tp = stats[TAG_PROGRESS_KEY]["90066248"]
    assert tp[TAG_PAGE_NUM_KEY] == 3
    assert tp[TAG_PAGE_NEXT_TS_KEY] == 1779422753729
    num, ts = tag_progress_from_stats(stats, 90066248)
    assert num == 3
    assert ts == 1779422753729
    assert not tag_is_done(stats, 90066248)


def test_tag_progress_marks_done_when_no_more():
    stats: dict = {}
    raw = {"result": {"pagination": {"isLoadMore": False, "pageTimestamp": 888}}}
    update_tag_progress_in_stats(stats, tag_id=123, page_num=10, raw_page=raw)
    entry = stats[TAG_PROGRESS_KEY]["123"]
    assert entry[TAG_PAGE_NUM_KEY] == 10
    assert TAG_PAGE_NEXT_TS_KEY not in entry
    assert entry[TAG_DONE_KEY] is True
    assert tag_is_done(stats, 123)
    assert tag_progress_from_stats(stats, 123) == (10, None)


def test_clear_all_tag_progress():
    stats = {TAG_PROGRESS_KEY: {"1": {TAG_PAGE_NUM_KEY: 5}}}
    clear_all_tag_progress(stats)
    assert stats == {}
