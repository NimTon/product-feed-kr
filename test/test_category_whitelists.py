"""分类白名单：抓取与无价独立配置。"""

from __future__ import annotations

import json

from product_feed_kr.pf_browser import category_whitelists as cw


def test_build_scrape_targets_from_whitelist_order(tmp_path, monkeypatch) -> None:
    wl = tmp_path / "scrape.json"
    wc = tmp_path / "wc.json"
    wl.write_text(
        json.dumps(
            {
                "max_parallel": 2,
                "scrape_pairs": [
                    ["G1", "T2"],
                    ["G1", "T1"],
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    wc.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "groupName": "G1",
                        "tags": [
                            {"tagName": "T1", "tagId": 101},
                            {"tagName": "T2", "tagId": 102},
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cw, "scrape_whitelist_path", lambda: wl)
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.category_maps.wecatalog_categories_path",
        lambda: wc,
    )
    cw.invalidate_scrape_whitelist_cache()

    groups = [
        {
            "groupName": "G1",
            "tags": [
                {"tagName": "T1", "tagId": 101},
                {"tagName": "T2", "tagId": 102},
            ],
        },
    ]
    targets = cw.build_scrape_tag_targets_from_whitelist(groups)
    assert [t.tag_id for t in targets] == [102, 101]
    cw.invalidate_scrape_whitelist_cache()


def test_clamp_scrape_max_parallel() -> None:
    assert cw.clamp_scrape_max_parallel(8, 3) == 3
    assert cw.clamp_scrape_max_parallel(2, 5) == 2
    assert cw.clamp_scrape_max_parallel(0, 4) == 1
    assert cw.clamp_scrape_max_parallel(4, 0) == 4


def test_save_caps_max_parallel_to_scrape_count(tmp_path, monkeypatch) -> None:
    wc = tmp_path / "wc.json"
    wl = tmp_path / "wl.json"
    cfg = tmp_path / "seven17.json"
    wc.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "groupName": "G1",
                        "tags": [
                            {"tagName": "T1", "tagId": 101},
                            {"tagName": "T2", "tagId": 102},
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cw, "scrape_whitelist_path", lambda: wl)
    monkeypatch.setattr(
        "product_feed_kr.pf_browser.category_maps.wecatalog_categories_path",
        lambda: wc,
    )
    monkeypatch.setattr(
        "product_feed_kr.seven17.no_price_whitelist.load_config_data",
        lambda: (cfg, {}),
    )
    cw.invalidate_scrape_whitelist_cache()

    out = cw.save_category_whitelists(
        {
            "scrape_pairs": [["G1", "T1"], ["G1", "T2"]],
            "no_price_pairs": [],
            "max_parallel": 5,
        },
    )
    assert out["scrape_count"] == 2
    assert out["max_parallel"] == 2
    saved = json.loads(wl.read_text(encoding="utf-8"))
    assert saved["max_parallel"] == 2
    assert cw.load_scrape_max_parallel() == 2
    cw.invalidate_scrape_whitelist_cache()


def test_no_price_whitelist_uses_wecatalog_categories(tmp_path, monkeypatch) -> None:
    from product_feed_kr.seven17 import no_price_whitelist as npw

    wc = tmp_path / "wc.json"
    wc.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "groupName": "手表专区",
                        "tags": [{"tagName": "劳力士", "tagId": 1}],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(npw, "load_all_wecatalog_tag_pairs", lambda: [("手表专区", "劳力士")])
    selected = npw.parse_selected_pairs("手表专区>劳力士")
    assert selected == {("手表专区", "劳力士")}
