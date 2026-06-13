"""无价格白名单：``SEVEN17_NO_PRICE_ALLOW_CATEGORIES`` 读写与 (分组, 标签) 解析。"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from product_feed_kr.common.seven17_config import seven17_config_path
from product_feed_kr.wecatalog.wecatalog_tag_mapping import mapping_rows

CONFIG_KEY = "SEVEN17_NO_PRICE_ALLOW_CATEGORIES"


def load_map_pairs() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for group, tag, _path, _anchor, _tid in mapping_rows():
        g = str(group or "").strip()
        t = str(tag or "").strip()
        if g and t:
            out.append((g, t))
    return out


def split_specs(raw: str) -> list[str]:
    s = str(raw or "").strip()
    if not s:
        return []
    out: list[str] = []
    cur: list[str] = []
    for ch in s:
        if ch in ",，;\n":
            spec = "".join(cur).strip()
            if spec:
                out.append(spec)
            cur = []
            continue
        cur.append(ch)
    spec = "".join(cur).strip()
    if spec:
        out.append(spec)
    return out


def parse_pair_spec(spec: str) -> tuple[str, str] | None:
    s = str(spec or "").strip()
    if not s:
        return None
    for sep in ("->", ">", "｜", "|", "/", "／", "＞"):
        if sep in s:
            left, right = s.split(sep, 1)
            g = left.strip()
            t = right.strip()
            if g and t:
                return g, t
            return None
    return None


def parse_selected_pairs(
    cfg_value: str,
    map_pairs: list[tuple[str, str]] | None = None,
) -> set[tuple[str, str]]:
    pairs = set(map_pairs if map_pairs is not None else load_map_pairs())
    selected: set[tuple[str, str]] = set()
    for spec in split_specs(cfg_value):
        pair = parse_pair_spec(spec)
        if pair is not None:
            if pair in pairs:
                selected.add(pair)
            continue
        tag = spec.strip()
        if not tag:
            continue
        for g, t in pairs:
            if t == tag:
                selected.add((g, t))
    return selected


def format_selected_pairs(selected: list[tuple[str, str]]) -> str:
    return ",".join(f"{g}>{t}" for g, t in selected)


def load_config_data() -> tuple[Path, dict[str, Any]]:
    cfg_path = seven17_config_path()
    data: dict[str, Any] = {}
    if cfg_path.is_file():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
    return cfg_path, data


def build_whitelist_groups(
    map_pairs: list[tuple[str, str]],
    selected: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    by_group: dict[str, list[str]] = defaultdict(list)
    for g, t in map_pairs:
        by_group[g].append(t)
    out: list[dict[str, Any]] = []
    for g in sorted(by_group.keys()):
        tags = sorted(by_group[g])
        tag_rows = [
            {
                "tagName": t,
                "selected": (g, t) in selected,
            }
            for t in tags
        ]
        n_sel = sum(1 for t in tags if (g, t) in selected)
        out.append(
            {
                "groupName": g,
                "tags": tag_rows,
                "selected_count": n_sel,
                "tag_count": len(tags),
                "all_selected": n_sel == len(tags) and len(tags) > 0,
                "some_selected": 0 < n_sel < len(tags),
            },
        )
    return out


def get_no_price_whitelist_state() -> dict[str, Any]:
    map_pairs = load_map_pairs()
    cfg_path, cfg_data = load_config_data()
    raw = str(cfg_data.get(CONFIG_KEY) or "").strip()
    selected = parse_selected_pairs(raw, map_pairs)
    groups = build_whitelist_groups(map_pairs, selected)
    return {
        "ok": True,
        "config_key": CONFIG_KEY,
        "config_path": str(cfg_path),
        "raw": raw,
        "pair_count": len(map_pairs),
        "selected_count": len(selected),
        "selected": [[g, t] for g, t in sorted(selected)],
        "groups": groups,
    }


def save_no_price_whitelist(pairs: list[list[Any]]) -> dict[str, Any]:
    map_pairs = load_map_pairs()
    valid = set(map_pairs)
    cleaned: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in pairs:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        g = str(item[0] or "").strip()
        t = str(item[1] or "").strip()
        if not g or not t:
            continue
        key = (g, t)
        if key not in valid or key in seen:
            continue
        seen.add(key)
        cleaned.append(key)
    cleaned.sort()
    cfg_path, cfg_data = load_config_data()
    cfg_data[CONFIG_KEY] = format_selected_pairs(cleaned)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "config_path": str(cfg_path),
        "selected_count": len(cleaned),
        "raw": cfg_data[CONFIG_KEY],
    }
