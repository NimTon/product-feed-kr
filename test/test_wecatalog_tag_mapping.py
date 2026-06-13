"""wecatalog_tag_mapping 宽松匹配。"""

from __future__ import annotations

from product_feed_kr.wecatalog.wecatalog_tag_mapping import (
    normalize_wecatalog_group_name,
    resolve_category_path,
)


def test_resolve_category_path_strips_group_number_prefix() -> None:
    assert resolve_category_path("NIKE 专区", "nike air max 95") is not None
    assert resolve_category_path("14,NIKE 专区", "nike air max 95") is not None


def test_resolve_category_path_tag_case_insensitive() -> None:
    assert resolve_category_path("NIKE 专区", "Nike Air Max 95") is not None
    assert resolve_category_path("NIKE 专区", "NIKE AIR MAX 95") is not None


def test_resolve_category_path_group_space_insensitive() -> None:
    assert resolve_category_path("NIKE专区", "nike air max 95") is not None
    assert resolve_category_path("14,NIKE专区", "Nike Air Max 95") is not None


def test_resolve_category_path_tag_extra_spaces() -> None:
    assert resolve_category_path("NIKE 专区", "nike  air max 95") is not None


def test_normalize_wecatalog_group_name() -> None:
    assert normalize_wecatalog_group_name("14,NIKE 专区") == "NIKE 专区"
