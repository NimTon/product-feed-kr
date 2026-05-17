"""map 未配置 ``seven17_ca_id`` 时，用 LLM 从后台分类下拉（``seven17_ca_options.json``）中选 ``ca_id``。"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from product_feed_kr.listing_llm_enrich import _chat_once_json, _openai_client, listing_llm_api_profiles
from product_feed_kr.pf_log import pf_kv, pf_store_row_id_kv
from product_feed_kr.seven17_config import bool_env as _cfg_bool
from product_feed_kr.seven17_config import getenv as _cfg_get
from product_feed_kr.wecatalog_tag_mapping import resolve_category_path, resolve_seven17_ca_id

_log = logging.getLogger("product_feed_kr.seven17_category_llm")

_CATEGORY_MAP_SUGGEST_ZH = (
    "未在 wecatalog_tag_category_map.json 配置分类映射；"
    "建议为对应 (wecatalog_group, wecatalog_tag) 在 meta 中手动填写 seven17_ca_id，"
    "避免依赖韩文路径匹配或 LLM 兜底"
)


def category_map_suggest_message(
    record: dict[str, Any],
    *,
    ca_id: str = "",
    source: str,
) -> str | None:
    """未走 map 时返回给人看的建议文案；走 map 返回 None。"""
    if source == "map":
        return None
    g = str(record.get("wecatalog_group") or "").strip()
    t = str(record.get("wecatalog_tag") or "").strip()
    src_label = {
        "path_match": "韩文路径精确匹配",
        "llm": "LLM 选定",
        "cache": "DB 缓存",
        "none": "尚未解析（上架时可能走 LLM）",
    }.get(source, source)
    parts = [
        f"未在 wecatalog_tag_category_map.json 为「{g}」+「{t}」配置 meta.seven17_ca_id",
        f"（当前来源：{src_label}",
    ]
    if ca_id:
        parts.append(f"，ca_id={ca_id}")
    parts.append("）；建议在 map 中手动补全映射关系")
    return "".join(parts)


def warn_suggest_category_map(
    record: dict[str, Any],
    *,
    ca_id: str,
    source: str,
) -> None:
    """map 未配置且分类来自路径匹配 / LLM / 缓存时打 WARNING。"""
    if source == "map":
        return
    g = str(record.get("wecatalog_group") or "").strip()
    t = str(record.get("wecatalog_tag") or "").strip()
    _log.warning(
        "%s",
        pf_kv(
            [
                ("event", "category.map.suggest"),
                *pf_store_row_id_kv(record),
                ("group", g),
                ("tag", t),
                ("ca_id", ca_id or "—"),
                ("source", source),
            ],
            zh=_CATEGORY_MAP_SUGGEST_ZH,
        ),
    )


def category_llm_fallback_enabled() -> bool:
    if not _cfg_bool("OPENAI_CATEGORY_FALLBACK", True):
        return False
    return bool(listing_llm_api_profiles())


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ca_options_path() -> Path:
    raw = (_cfg_get("SEVEN17_CA_OPTIONS_JSON") or "data/seven17_ca_options.json").strip()
    p = Path(raw).expanduser()
    return p.resolve() if p.is_absolute() else (_project_root() / p).resolve()


def _max_options_for_llm() -> int:
    raw = (_cfg_get("OPENAI_CATEGORY_LLM_MAX_OPTIONS") or "80").strip()
    try:
        return max(20, min(200, int(raw)))
    except ValueError:
        return 80


@lru_cache(maxsize=1)
def load_seven17_ca_catalog() -> tuple[tuple[str, str], ...]:
    """(value, label) 列表，已去掉空 value 与「선택하세요」。"""
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


def match_ca_id_by_korean_path(path_label: str) -> str | None:
    """韩文路径与后台 option label 完全一致时直接返回 value（不调 LLM）。"""
    lab = path_label.strip()
    if not lab:
        return None
    for val, label in load_seven17_ca_catalog():
        if label == lab:
            return val
    return None


def _tokenize_hint(text: str) -> set[str]:
    t = str(text or "").strip().lower()
    if not t:
        return set()
    parts = re.split(r"[\s>·|,/，、；;]+", t)
    return {p for p in parts if len(p) >= 2}


def _score_option(
    val: str,
    label: str,
    *,
    path_label: str,
    group: str,
    tag: str,
    title: str,
    name_ko: str,
) -> int:
    score = 0
    if path_label:
        if label == path_label:
            score += 200
        elif path_label in label or label in path_label:
            score += 80
        for seg in path_label.split(" > "):
            seg = seg.strip()
            if seg and seg in label:
                score += 25
    hints = " ".join(x for x in (group, tag, title, name_ko) if x)
    for tok in _tokenize_hint(hints):
        if tok in label.lower():
            score += 3
    # 叶子类（路径更深）略加分
    score += min(15, label.count(">") * 3)
    return score


def _candidate_options_for_record(
    record: dict[str, Any],
    *,
    commodity: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    catalog = list(load_seven17_ca_catalog())
    if not catalog:
        return []
    path_label = _record_shop_path_label(record)
    group = str(record.get("wecatalog_group") or "")
    tag = str(record.get("wecatalog_tag") or "")
    title = ""
    name_ko = ""
    if isinstance(commodity, dict):
        title = str(commodity.get("title") or "")
    ll = record.get("listing_llm")
    if isinstance(ll, dict):
        name_ko = str(ll.get("name_ko") or "")

    scored: list[tuple[int, str, str]] = []
    for val, label in catalog:
        s = _score_option(
            val,
            label,
            path_label=path_label,
            group=group,
            tag=tag,
            title=title,
            name_ko=name_ko,
        )
        if s > 0:
            scored.append((s, val, label))
    scored.sort(key=lambda x: (-x[0], len(x[2])))
    cap = _max_options_for_llm()
    top = [(v, lab) for _s, v, lab in scored[:cap]]
    if len(top) >= 15:
        return top
    # 候选过少：补一些顶层大类 option（value 较短）
    have = {v for v, _ in top}
    for val, label in catalog:
        if val in have:
            continue
        if len(val) <= 4:
            top.append((val, label))
            have.add(val)
        if len(top) >= cap:
            break
    return top[:cap]


_SYSTEM_CATEGORY = """你是 seven17 商城商品分类助手。只输出一个 JSON 对象，不要 Markdown。
格式：{"ca_id":"后台option的value字符串","label":"所选韩文分类文案"}
只能从用户给出的「可选分类」列表中选择，ca_id 必须与列表中某一项 value 完全一致。
选最具体、最匹配商品的一条（优先叶子分类）；不确定时选最接近的上级分类。"""


def _parse_category_response(content: str, allowed: dict[str, str]) -> tuple[str | None, str | None]:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```\s*$", "", raw)
    data = json.loads(raw.strip())
    if not isinstance(data, dict):
        return None, None
    ca = str(data.get("ca_id") or data.get("value") or "").strip()
    lab = str(data.get("label") or "").strip()
    if ca and ca in allowed:
        return ca, allowed[ca]
    if lab:
        for val, lbl in allowed.items():
            if lbl == lab:
                return val, lbl
    return None, None


def suggest_seven17_ca_id_llm(
    record: dict[str, Any],
    *,
    commodity: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> tuple[str | None, str | None]:
    """
    返回 (ca_id, ca_label)；失败为 (None, None)。
    调用方负责写入 ``record['seven17_ca_id']``。
    """
    if not category_llm_fallback_enabled():
        return None, None

    path_label = _record_shop_path_label(record)
    hit = match_ca_id_by_korean_path(path_label)
    if hit:
        catalog = dict(load_seven17_ca_catalog())
        return hit, catalog.get(hit)

    candidates = _candidate_options_for_record(record, commodity=commodity)
    if not candidates:
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "category.llm.skip"),
                    ("reason", "no_catalog"),
                    *pf_store_row_id_kv(record),
                ],
                zh="无 seven17 分类候选（请先生成 data/seven17_ca_options.json）",
            ),
        )
        return None, None

    allowed = {v: lab for v, lab in candidates}
    group = str(record.get("wecatalog_group") or "")
    tag = str(record.get("wecatalog_tag") or "")
    title = str((commodity or {}).get("title") or "")
    ll = record.get("listing_llm")
    name_ko = str(ll.get("name_ko") or "") if isinstance(ll, dict) else ""

    lines = [f"- value={v} | {lab}" for v, lab in candidates]
    user = (
        f"微猫分组：{group}\n"
        f"微猫标签：{tag}\n"
        f"映射韩文路径：{path_label or '（无）'}\n"
        f"商品标题：{title}\n"
        f"韩文商品名：{name_ko}\n\n"
        f"可选分类（共 {len(candidates)} 条）：\n"
        + "\n".join(lines)
    )

    profiles = listing_llm_api_profiles()
    profile = profiles[0] if profiles else None
    client, model, host = _openai_client(timeout, api_profile=profile)
    use_rf = "dashscope.aliyuncs.com" not in host.lower()

    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "category.llm.request"),
                *pf_store_row_id_kv(record),
                ("candidates", len(candidates)),
                ("path", path_label or "—"),
            ],
            zh="LLM 选择 seven17 分类",
        ),
    )

    try:
        content, elapsed_ms = _chat_once_json(
            client,
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_CATEGORY},
                {"role": "user", "content": user},
            ],
            use_response_format=use_rf,
        )
        ca_id, ca_label = _parse_category_response(content, allowed)
    except Exception as e:
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "category.llm.err"),
                    *pf_store_row_id_kv(record),
                    ("err", str(e)),
                ],
                zh="LLM 分类失败",
            ),
        )
        return None, None

    if not ca_id:
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "category.llm.invalid"),
                    *pf_store_row_id_kv(record),
                    ("elapsed_ms", elapsed_ms),
                ],
                zh="LLM 返回的分类不在候选列表中",
            ),
        )
        return None, None

    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "category.llm.ok"),
                *pf_store_row_id_kv(record),
                ("ca_id", ca_id),
                ("label", ca_label or ""),
                ("elapsed_ms", elapsed_ms),
            ],
            zh="LLM 已选定 seven17 分类",
        ),
    )
    return ca_id, ca_label


def resolve_ca_id_for_store_record(
    record: dict[str, Any],
    *,
    commodity: dict[str, Any] | None = None,
    allow_llm: bool = True,
) -> tuple[str | None, str]:
    """
    解析上架用 ``ca_id``。返回 (ca_id, source)，source 为 map | path_match | cache | llm | none。
    """
    g = str(record.get("wecatalog_group") or "")
    t = str(record.get("wecatalog_tag") or "")
    from_map = resolve_seven17_ca_id(g, t)
    if from_map:
        return from_map, "map"

    path_label = _record_shop_path_label(record)
    by_path = match_ca_id_by_korean_path(path_label)
    if by_path:
        record["seven17_ca_id"] = by_path
        warn_suggest_category_map(record, ca_id=by_path, source="path_match")
        return by_path, "path_match"

    stored = str(record.get("seven17_ca_id") or "").strip()
    if stored:
        return stored, "cache"

    if not allow_llm:
        return None, "none"

    ca_id, _lab = suggest_seven17_ca_id_llm(record, commodity=commodity)
    if ca_id:
        record["seven17_ca_id"] = ca_id
        warn_suggest_category_map(record, ca_id=ca_id, source="llm")
        return ca_id, "llm"
    return None, "none"
