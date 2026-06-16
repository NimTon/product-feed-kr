"""实时诊断分析单测（不启动浏览器）。"""

from __future__ import annotations

from product_feed_kr.pf_browser.diagnose_probe import analyze_live_scrape_blockers
from product_feed_kr.wecatalog.wecatalog_tag_mapping import ScrapeTagTarget


def _groups() -> list[dict]:
    return [
        {
            "groupName": "1,男性服装",
            "tags": [{"tagId": 90066248, "tagName": "톰브라운Thom Browne"}],
        },
    ]


def _view_resp(com: dict) -> dict:
    return {"success": True, "errcode": 0, "result": {"commodity": com}}


def test_analyze_no_price_not_whitelist(monkeypatch) -> None:
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.diagnose_probe.build_scrape_tag_targets",
        lambda _groups: (
            ScrapeTagTarget("1,男性服装", "톰브라운Thom Browne", 90066248, ("의류", "남성의류", "톰 브라운")),
        ),
    )
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.diagnose_probe.record_is_no_price_allowed_by_map_category",
        lambda _rec: False,
    )
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.diagnose_probe.resolve_category_path",
        lambda _g, _t: ("의류", "남성의류", "톰 브라운"),
    )
    com = {
        "goods_id": "_gid",
        "title": "无价格短裤",
        "imgsSrc": ["https://example.com/a.jpg"],
        "tags": [{"tagId": 90066248, "tagName": "톰브라운Thom Browne"}],
        "optimaPrice": "",
    }
    out = analyze_live_scrape_blockers(
        album_id="_album",
        goods_id="_gid",
        groups=_groups(),
        view_resp=_view_resp(com),
        scrape_progress=None,
    )
    codes = {b["code"] for b in out["blockers"]}
    assert "list_no_price" in codes
    assert "无价" in out["primary_blocker_zh"]


def test_analyze_video_only() -> None:
    com = {
        "goods_id": "_gid",
        "title": "视频商品",
        "imgsSrc": ["https://xcimg.szwego.com/foo/bar.mp4"],
        "optimaPrice": "100",
        "tags": [{"tagId": 90066248, "tagName": "톰브라운Thom Browne"}],
    }
    out = analyze_live_scrape_blockers(
        album_id="_album",
        goods_id="_gid",
        groups=_groups(),
        view_resp=_view_resp(com),
        scrape_progress=None,
    )
    assert any(b["code"] == "video_media" for b in out["blockers"])


def test_analyze_unmapped_category(monkeypatch) -> None:
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.diagnose_probe.build_scrape_tag_targets",
        lambda _groups: (),
    )
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.diagnose_probe.resolve_category_path",
        lambda _g, _t: None,
    )
    com = {
        "goods_id": "_gid",
        "title": "💰335 短裤",
        "imgsSrc": ["https://example.com/a.jpg"],
        "optimaPrice": "335",
        "tags": [{"tagId": 99999, "tagName": "未映射标签"}],
    }
    groups = [{"groupName": "测试组", "tags": [{"tagId": 99999, "tagName": "未映射标签"}]}]
    out = analyze_live_scrape_blockers(
        album_id="_album",
        goods_id="_gid",
        groups=groups,
        view_resp=_view_resp(com),
        scrape_progress=None,
    )
    assert any(b["code"] == "unmapped_category" for b in out["blockers"])
