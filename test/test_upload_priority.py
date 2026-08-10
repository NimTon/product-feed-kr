"""上架优先级：独立 JSON 配置功能测试。"""

from __future__ import annotations

import json

from product_feed_kr.pf_browser import upload_priority as up


def test_upload_priority_path_uses_default() -> None:
    path = up.upload_priority_path()
    assert path.name == "wecatalog_upload_priority.json"
    assert path.parent.name in ("data", "product-feed-kr")


def test_load_empty_priority(tmp_path, monkeypatch) -> None:
    wl = tmp_path / "up.json"
    wl.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(up, "upload_priority_path", lambda: wl)
    up.invalidate_upload_priority_cache()
    assert up.load_upload_priority_rows() == []
    assert up.upload_priority_rank_map() == {}


def test_save_and_load_priority(tmp_path, monkeypatch) -> None:
    wl = tmp_path / "up.json"
    monkeypatch.setattr(up, "upload_priority_path", lambda: wl)
    up.invalidate_upload_priority_cache()

    payload = up.save_upload_priority([["G1", "T2"], ["G1", "T1"]])
    assert "updated_at" in payload
    assert len(payload["priority_pairs"]) == 2

    rows = up.load_upload_priority_rows()
    assert rows == [["G1", "T2"], ["G1", "T1"]]

    ranks = up.upload_priority_rank_map()
    assert ranks == {("G1", "T2"): 0, ("G1", "T1"): 1}


def test_dedup_and_strip_priority(tmp_path, monkeypatch) -> None:
    wl = tmp_path / "up.json"
    monkeypatch.setattr(up, "upload_priority_path", lambda: wl)
    up.invalidate_upload_priority_cache()

    up.save_upload_priority([
        ["  G1  ", "  T1  "],
        ["G1", "T1"],
        ["G2", "T2"],
        [""],
        ["", "T3"],
    ])
    rows = up.load_upload_priority_rows()
    assert rows == [["G1", "T1"], ["G2", "T2"]]


def test_priority_rank_map_empty_if_no_file(tmp_path, monkeypatch) -> None:
    wl = tmp_path / "nonexistent.json"
    monkeypatch.setattr(up, "upload_priority_path", lambda: wl)
    up.invalidate_upload_priority_cache()
    assert up.upload_priority_rank_map() == {}


def test_save_upload_priority_from_body_validates(tmp_path, monkeypatch) -> None:
    wl = tmp_path / "up.json"
    monkeypatch.setattr(up, "upload_priority_path", lambda: wl)
    up.invalidate_upload_priority_cache()

    monkeypatch.setattr(up, "_distinct_db_pairs", lambda: [
        {"group": "G1", "tag": "T1", "item_count": 3},
        {"group": "G2", "tag": "T2", "item_count": 5},
    ])

    result = up.save_upload_priority_from_body({
        "priority_pairs": [["G1", "T1"], ["G2", "T2"]],
    })
    assert result["ok"] is True
    assert result["priority_count"] == 2

    rows = up.load_upload_priority_rows()
    assert rows == [["G1", "T1"], ["G2", "T2"]]


def test_save_upload_priority_from_body_rejects_invalid(tmp_path, monkeypatch) -> None:
    wl = tmp_path / "up.json"
    monkeypatch.setattr(up, "upload_priority_path", lambda: wl)
    up.invalidate_upload_priority_cache()

    monkeypatch.setattr(up, "_distinct_db_pairs", lambda: [
        {"group": "G1", "tag": "T1", "item_count": 1},
    ])

    result = up.save_upload_priority_from_body({
        "priority_pairs": [["G1", "T2"]],
    })
    assert result["priority_count"] == 0  # rejected: not in DB


def test_get_state_shows_db_pairs_with_priority(tmp_path, monkeypatch) -> None:
    wl = tmp_path / "up.json"
    monkeypatch.setattr(up, "upload_priority_path", lambda: wl)
    up.invalidate_upload_priority_cache()

    up.save_upload_priority([["G1", "T1"]])

    monkeypatch.setattr(up, "_distinct_db_pairs", lambda: [
        {"group": "G1", "tag": "T1", "item_count": 10},
        {"group": "G2", "tag": "T2", "item_count": 3},
    ])

    state = up.get_upload_priority_state()
    assert state["ok"] is True
    assert state["priority_count"] == 1
    assert len(state["pairs"]) == 2
    p1 = next(p for p in state["pairs"] if p["group"] == "G1")
    assert p1["priority_index"] == 0
    p2 = next(p for p in state["pairs"] if p["group"] == "G2")
    assert p2["priority_index"] is None
