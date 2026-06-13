"""wecatalog 分组 + 标签 → 商城分类路径（韩文层级）映射；供爬取/上架查询。

数据文件：``data/wecatalog_category_pairs.json``（由 05 商品库浏览「分类配对」维护）。每行：
``[groupName, tagName, [cat1, cat2, ...], optional_meta]``。
``optional_meta`` 可为：

- ``{"anchor_only": true}``：仅作独立站目录锚点、不挂商品；
- ``{"tag_id": 123456}``：微猫 commodity/tags 的 ``tagId``（05 启动同步微猫分类时写入）。

上架 ``ca_id``：韩文路径 → ``data/seven17_path_ca_map.json``（05 启动时从 itemform 同步）。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_SEP = "\x00"
_GROUP_PREFIX = re.compile(r"^\d+\s*,\s*(.+)$")
_WS_COLLAPSE = re.compile(r"\s+")


def _collapse_ws(text: str) -> str:
    return _WS_COLLAPSE.sub(" ", str(text or "").strip())


def _group_loose_key(group_name: str) -> str:
    """分组名宽松键：去编号前缀、合并空白后去掉全部空格再 casefold。"""
    return re.sub(r"\s+", "", normalize_wecatalog_group_name(_collapse_ws(group_name))).casefold()


def _tag_loose_key(tag_name: str) -> str:
    return _collapse_ws(tag_name).casefold()


def _map_path() -> Path:
    from product_feed_kr.pf_browser.category_maps import category_pairs_path

    return category_pairs_path()


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


def _parse_rows_payload(raw: Any) -> list[list[Any]]:
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, list)]
    if isinstance(raw, dict) and isinstance(raw.get("rows"), list):
        return [r for r in raw["rows"] if isinstance(r, list)]
    raise ValueError("mapping root must be array or {rows: [...]}")


@lru_cache(maxsize=1)
def _load_rows() -> tuple[tuple[str, str, tuple[str, ...], bool, int | None], ...]:
    path = _map_path()
    if not path.is_file():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = _parse_rows_payload(raw)
    out: list[tuple[str, str, tuple[str, ...], bool, int | None]] = []
    for row in rows:
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
        out.append((g, t, seg, anchor, tid))
    return tuple(out)


@lru_cache(maxsize=1)
def _lookup_dict() -> dict[str, tuple[tuple[str, ...], bool, int | None]]:
    d: dict[str, tuple[tuple[str, ...], bool, int | None]] = {}
    for g, t, path, anchor, tid in _load_rows():
        d[_SEP.join((g, t))] = (path, anchor, tid)
    return d


def normalize_wecatalog_group_name(group_name: str) -> str:
    """微猫 API 分组名可能带 ``14,`` 前缀；映射表分组名为逗号后部分。"""
    g = str(group_name or "").strip()
    m = _GROUP_PREFIX.match(g)
    return m.group(1).strip() if m else g


def _group_keys(group_name: str) -> tuple[str, ...]:
    g = str(group_name or "").strip()
    if not g:
        return ()
    norm = normalize_wecatalog_group_name(g)
    out: list[str] = [g]
    if norm and norm not in out:
        out.append(norm)
    return tuple(out)


def mapping_rows() -> tuple[tuple[str, str, tuple[str, ...], bool, int | None], ...]:
    """全部映射行：(group, tag, path, anchor_only, tag_id)。"""
    return _load_rows()


def resolve_category_path(group_name: str, tag_name: str) -> tuple[str, ...] | None:
    """匹配 group + tag；支持分组 ``N,`` 前缀、空白合并、大小写与空格差异回退。"""
    t = _collapse_ws(tag_name)
    if not t:
        return None
    keys = _group_keys(group_name)
    if not keys:
        return None
    lookup = _lookup_dict()
    t_fold = _tag_loose_key(t)
    query_group_loose = _group_loose_key(group_name)
    key_set = set(keys) | {_collapse_ws(g) for g in keys}
    for g in keys:
        for gg in (g, _collapse_ws(g)):
            hit = lookup.get(_SEP.join((gg, t)))
            if hit:
                return hit[0]
    for g, mt, path, anchor, _tid in _load_rows():
        if anchor:
            continue
        g_hit = g in key_set or _collapse_ws(g) in key_set or _group_loose_key(g) == query_group_loose
        if not g_hit:
            continue
        if _tag_loose_key(mt) == t_fold:
            return path
    return None


def resolve_seven17_ca_id(group_name: str, tag_name: str) -> str | None:
    """按韩文路径查 ``seven17_path_ca_map.json``。"""
    path = resolve_category_path(group_name, tag_name)
    if not path:
        return None
    from product_feed_kr.seven17.seven17_path_ca_map import resolve_ca_id_by_path_tuple

    return resolve_ca_id_by_path_tuple(path)


def is_anchor_only(group_name: str, tag_name: str) -> bool:
    """该映射是否为「仅目录锚点、不挂商品」。"""
    key = _SEP.join((group_name, tag_name))
    hit = _lookup_dict().get(key)
    return bool(hit[1]) if hit else False


def leaf_mapping_rows() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """排除 anchor_only 后的上架用映射：(group, tag, path)。"""
    return tuple((g, t, p) for g, t, p, a, _tid in _load_rows() if not a)


def leaf_match_specs() -> tuple[tuple[str, str, tuple[str, ...], int | None], ...]:
    """非锚点映射，供爬虫对齐列表标签：(group, tag, shop_path, tag_id_or_none)。"""
    return tuple((g, t, p, tid) for g, t, p, a, tid in _load_rows() if not a)


def invalidate_mapping_cache() -> None:
    """配对 JSON 更新后清除 ``lru_cache``，使爬取/上架读到最新映射。"""
    _load_rows.cache_clear()
    _lookup_dict.cache_clear()


def extend_mapping(rows: list[list[Any]]) -> None:
    """运行时追加并写回配对 JSON（测试用）；按 (group,tag) 去重覆盖。"""
    existing: dict[tuple[str, str], tuple[tuple[str, ...], dict[str, Any]]] = {}
    for g, t, p, a, tid in _load_rows():
        meta: dict[str, Any] = {}
        if a:
            meta["anchor_only"] = True
        if tid is not None:
            meta["tag_id"] = tid
        existing[(g, t)] = (p, meta)
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        g, t, path = row[0], row[1], row[2]
        if not isinstance(g, str) or not isinstance(t, str) or not isinstance(path, list):
            continue
        meta = _parse_meta(row) if len(row) >= 4 else {}
        meta.pop("seven17_ca_id", None)
        existing[(g, t)] = (tuple(str(x) for x in path), meta)
    compact: list[list[Any]] = []
    for (g, t), (p, meta) in sorted(existing.items()):
        item: list[Any] = [g, t, list(p)]
        if meta:
            item.append(meta)
        compact.append(item)
    from product_feed_kr.pf_browser.category_maps import save_category_pairs

    save_category_pairs(compact, merge_tag_ids=False)
