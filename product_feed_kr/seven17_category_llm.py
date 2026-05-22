"""seven17 上架分类：微猫分组/标签 → 韩文路径 → ``path_ca_map`` → ``ca_id``（无 LLM）。"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from product_feed_kr.pf_log import pf_kv, pf_store_row_id_kv
from product_feed_kr.seven17_config import getenv as _cfg_get
from product_feed_kr.wecatalog_tag_mapping import resolve_category_path

_log = logging.getLogger("product_feed_kr.seven17_category_llm")

_CATEGORY_MAP_SUGGEST_ZH = (
    "未在 wecatalog_tag_category_map 配置韩文路径，或 seven17_path_ca_map 无对应 ca_id；"
    "请补全 txt 映射并重新抓取同步 path_ca_map"
)


def category_map_suggest_message(
    record: dict[str, Any],
    *,
    ca_id: str = "",
    source: str,
) -> str | None:
    """未走 path_map 时返回给人看的建议文案。"""
    if source == "path_map":
        return None
    g = str(record.get("wecatalog_group") or "").strip()
    t = str(record.get("wecatalog_tag") or "").strip()
    src_label = {
        "path_map": "路径→ca_id 映射表",
        "cache": "DB 缓存",
        "none": "尚未解析",
    }.get(source, source)
    parts = [
        f"「{g}」+「{t}」未通过 path_ca_map 解析 ca_id（来源：{src_label}",
    ]
    if ca_id:
        parts.append(f"，ca_id={ca_id}")
    parts.append("）；请补全 config/wecatalog_tag_category_map.txt 韩文路径")
    return "".join(parts)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ca_options_path() -> Path:
    raw = (_cfg_get("SEVEN17_CA_OPTIONS_JSON") or "data/seven17_ca_options.json").strip()
    p = Path(raw).expanduser()
    return p.resolve() if p.is_absolute() else (_project_root() / p).resolve()


@lru_cache(maxsize=1)
def load_seven17_ca_catalog() -> tuple[tuple[str, str], ...]:
    """(value, label) 列表，供预览/校验；已去掉空 value 与「선택하세요」。"""
    path = _ca_options_path()
    if not path.is_file():
        return ()
    data = json.loads(path.read_text(encoding="utf-8"))
    selects = data.get("selects") if isinstance(data, dict) else {}
    opts = selects.get("ca_id") if isinstance(selects, dict) else []
    if not isinstance(opts, list):
        return ()
    out: list[tuple[str, str]] = []
    for o in opts:
        if not isinstance(o, dict):
            continue
        val = str(o.get("value") or "").strip()
        lab = str(o.get("label") or "").strip()
        if not val or not lab or lab == "선택하세요":
            continue
        out.append((val, lab))
    return tuple(out)


def _path_tuple_to_label(path: tuple[str, ...] | list[str] | None) -> str:
    if not path:
        return ""
    return " > ".join(str(x).strip() for x in path if str(x).strip())


def _record_shop_path_label(record: dict[str, Any]) -> str:
    scp = record.get("shop_category_path")
    if isinstance(scp, list) and scp:
        return _path_tuple_to_label(scp)
    g = str(record.get("wecatalog_group") or "")
    t = str(record.get("wecatalog_tag") or "")
    seg = resolve_category_path(g, t)
    return _path_tuple_to_label(seg)


def resolve_ca_id_for_store_record(
    record: dict[str, Any],
    *,
    commodity: dict[str, Any] | None = None,
) -> tuple[str | None, str]:
    """
    解析上架用 ``ca_id``。返回 (ca_id, source)，source 为 path_map | cache | none。
    ``commodity`` 保留以兼容旧调用方，当前未使用。
    """
    del commodity
    from product_feed_kr.seven17_path_ca_map import resolve_ca_id_by_path_label

    path_label = _record_shop_path_label(record)
    if path_label:
        by_path_map = resolve_ca_id_by_path_label(path_label)
        if by_path_map:
            record["seven17_ca_id"] = by_path_map
            return by_path_map, "path_map"

    stored = str(record.get("seven17_ca_id") or "").strip()
    if stored:
        return stored, "cache"

    return None, "none"
