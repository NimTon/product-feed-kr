"""wecatalog 分组 + 标签 → 商城分类路径（韩文层级）映射；供爬取/上架查询。

数据文件：`wecatalog_tag_category_map.json`（与模块同目录）。每行：
`[groupName, tagName, [cat1, cat2, ...], optional_meta]`。
`optional_meta` 可为：
- `{"anchor_only": true}`：仅作独立站目录锚点、不挂商品；
- `{"tag_id": 123456}`：微猫侧叶子标签 ID（可选）；爬虫用其与列表里的 `tags[].tagId` 精确对齐，
  **同一文案在不同分组重复时建议在 py/JSON 里写上 tag_id**；
- `{"seven17_ca_id": "85"}`：seven17 后台商品表单 **기본분류** 下拉对应的 **`ca_id`（字符串）**，
  **`seven17_upload` 按 (分组, 标签) 选用**；未写则该商品无法上架（不会读其它配置的默认分类）。

批量维护：编辑 `config/wecatalog_tag_category_map.txt` 后执行
`python -m product_feed_kr.wecatalog_tag_category_map_builder` 或 `build_wecatalog_tag_category_map.bat`
重新生成 `wecatalog_tag_category_map.json`。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_SEP = "\x00"


def _map_path() -> Path:
    return Path(__file__).resolve().with_name("wecatalog_tag_category_map.json")


def _parse_meta(row: list[Any]) -> dict[str, Any]:
    if len(row) < 4:
        return {}
    m = row[3]
    return m if isinstance(m, dict) else {}


def _meta_tag_id(meta: dict[str, Any]) -> int | None:
    v = meta.get("tag_id")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _meta_seven17_ca_id(meta: dict[str, Any]) -> str | None:
    v = meta.get("seven17_ca_id")
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


@lru_cache(maxsize=1)
def _load_rows() -> tuple[tuple[str, str, tuple[str, ...], bool, int | None, str | None], ...]:
    raw = json.loads(_map_path().read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("mapping root must be array")
    out: list[tuple[str, str, tuple[str, ...], bool, int | None, str | None]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 3:
            continue
        g, t, path = row[0], row[1], row[2]
        if not isinstance(g, str) or not isinstance(t, str):
            continue
        if not isinstance(path, list):
            continue
        seg = tuple(str(x) for x in path)
        meta = _parse_meta(row)
        anchor = bool(meta.get("anchor_only"))
        tid = _meta_tag_id(meta)
        ca = _meta_seven17_ca_id(meta)
        out.append((g, t, seg, anchor, tid, ca))
    return tuple(out)


@lru_cache(maxsize=1)
def _lookup_dict() -> dict[str, tuple[tuple[str, ...], bool, int | None, str | None]]:
    d: dict[str, tuple[tuple[str, ...], bool, int | None, str | None]] = {}
    for g, t, path, anchor, tid, ca in _load_rows():
        d[_SEP.join((g, t))] = (path, anchor, tid, ca)
    return d


def mapping_rows() -> tuple[tuple[str, str, tuple[str, ...], bool, int | None, str | None], ...]:
    """全部映射行：(group, tag, path, anchor_only, tag_id, seven17_ca_id_or_none)。"""
    return _load_rows()


def resolve_category_path(group_name: str, tag_name: str) -> tuple[str, ...] | None:
    """精确匹配 group + tag；未命中返回 None。"""
    key = _SEP.join((group_name, tag_name))
    hit = _lookup_dict().get(key)
    return hit[0] if hit else None


def resolve_seven17_ca_id(group_name: str, tag_name: str) -> str | None:
    """按映射 meta.seven17_ca_id；未配置返回 None。"""
    key = _SEP.join((group_name, tag_name))
    hit = _lookup_dict().get(key)
    if not hit:
        return None
    return hit[3]


def is_anchor_only(group_name: str, tag_name: str) -> bool:
    """该映射是否为「仅目录锚点、不挂商品」。"""
    key = _SEP.join((group_name, tag_name))
    hit = _lookup_dict().get(key)
    return bool(hit[1]) if hit else False


def leaf_mapping_rows() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """排除 anchor_only 后的上架用映射：(group, tag, path)。"""
    return tuple((g, t, p) for g, t, p, a, _tid, _ca in _load_rows() if not a)


def leaf_match_specs() -> tuple[tuple[str, str, tuple[str, ...], int | None], ...]:
    """非锚点映射，供爬虫对齐列表标签：(group, tag, shop_path, tag_id_or_none)。"""
    return tuple((g, t, p, tid) for g, t, p, a, tid, _ca in _load_rows() if not a)


def extend_mapping(rows: list[list[Any]]) -> None:
    """运行时追加并写回 JSON（测试或生成脚本用）；按 (group,tag) 去重覆盖。"""
    existing: dict[tuple[str, str], tuple[tuple[str, ...], dict[str, Any]]] = {}
    for g, t, p, a, tid, ca in _load_rows():
        meta: dict[str, Any] = {}
        if a:
            meta["anchor_only"] = True
        if tid is not None:
            meta["tag_id"] = tid
        if ca is not None:
            meta["seven17_ca_id"] = ca
        existing[(g, t)] = (p, meta)
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        g, t, path = row[0], row[1], row[2]
        if not isinstance(g, str) or not isinstance(t, str) or not isinstance(path, list):
            continue
        meta = _parse_meta(row) if len(row) >= 4 else {}
        existing[(g, t)] = (tuple(str(x) for x in path), meta)
    compact: list[list[Any]] = []
    for (g, t), (p, meta) in sorted(existing.items()):
        item: list[Any] = [g, t, list(p)]
        if meta:
            item.append(meta)
        compact.append(item)
    _map_path().write_text(json.dumps(compact, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    _load_rows.cache_clear()
    _lookup_dict.cache_clear()
