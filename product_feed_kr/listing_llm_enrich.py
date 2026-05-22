"""上架前 LLM（每条商品最多 **两次** Chat）：

1. **补缺**（仅当抓取缺价/缺尺码）：只补**中文**——``cny_price``、``attr_map``（颜色/尺码）。
2. **文案+翻译**（必跑）：``name_zh``、``desc_zh``、``desc_ko``、``attr_map_ko`` 等韩文由本阶段统一产出。

抓取阶段已结构化入库 title/图/价/尺码，不依赖 ``detail_response`` 原始包。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from product_feed_kr.pf_time import now_cst8_iso
from typing import Any, TypedDict
from urllib.parse import urlparse

from product_feed_kr.seven17_config import bool_env as _cfg_bool
from product_feed_kr.seven17_config import getenv as _cfg_get
from product_feed_kr.seven17_config import load_seven17_config
from product_feed_kr.pf_log import log_item_separator, pf_goods_id, pf_kv, pf_store_row_id_kv

_log = logging.getLogger(__name__)

_NO_PRICE_ALLOW_DEFAULT = (
    "手表专区",
    "女士包专区",
    "定制皮夹克",
)


class ListingLlmApiProfile(TypedDict):
    label: str
    api_key: str
    base_url: str | None
    model: str
    threads: int


# 鞋类语境：用于提示词与后处理（欧码 → 韩版毫米脚长）。
_FOOTWEAR_HINT_RE = re.compile(
    r"鞋|靴|拖|sneaker|boot|loafer|sandal|heel|flip\s*flop|"
    r"运动(?:鞋|靴)|休闲鞋|板鞋|跑鞋|球鞋|帆布|高跟|凉鞋|乐福|穆勒|豆豆|马丁|"
    r"운동화|신발|샌들|부츠|슬리퍼|구두|스니커|워커",
    re.I,
)

# 欧码 → 韩版毫米脚长（合并各鞋型/男女款对照表；整数码保留原表，半码取邻码上沿）。
_KR_MM_EU_TO_MM: dict[str, str] = {
    "32": "210",
    "32.5": "215",
    "33": "215",
    "33.5": "220",
    "34": "220",
    "34.5": "225",
    "35": "225",
    "35.5": "230",
    "36": "230",
    "36.5": "235",
    "37": "235",
    "37.5": "240",
    "38": "240",
    "38.5": "245",
    "39": "245",
    "39.5": "250",
    "40": "250",
    "40.5": "255",
    "41": "260",
    "41.5": "265",
    "42": "265",
    "42.5": "270",
    "43": "275",
    "43.5": "280",
    "44": "280",
    "44.5": "285",
    "45": "290",
    "45.5": "295",
    "46": "295",
    "46.5": "300",
    "47": "300",
    "47.5": "305",
    "48": "305",
    "48.5": "310",
    "49": "310",
    "49.5": "315",
    "50": "315",
}

_EU_SHOE_SIZE_MIN = 32
_EU_SHOE_SIZE_MAX = 50


def _text_suggests_footwear(blob: str) -> bool:
    if not blob or not isinstance(blob, str):
        return False
    return bool(_FOOTWEAR_HINT_RE.search(blob))


def _shoe_size_token_to_kr_mm(tok: str) -> str:
    """鞋类：欧码 / 「EU42」等 → 韩版毫米脚长字符串；已是 210–320 毫米则原样保留。"""
    t0 = str(tok).strip()
    if not t0:
        return tok
    size_map = _KR_MM_EU_TO_MM
    m_eu = re.fullmatch(r"(?i)EU\s*([0-9]{2})(\.[05])?", t0)
    if m_eu:
        whole = int(m_eu.group(1))
        frac = m_eu.group(2)
        if frac:
            key = f"{whole}{frac}"
            return size_map.get(key, tok)
        if _EU_SHOE_SIZE_MIN <= whole <= _EU_SHOE_SIZE_MAX:
            return size_map.get(str(whole), tok)
        return tok
    # 含字母且非纯 EU 数字：S/M/L/XL、2XL 等保持原样。
    if re.search(r"[A-Za-z]", t0) and not re.fullmatch(r"(?i)EU\s*[0-9]{2}(?:\.[05])?", t0):
        return tok
    t = t0
    if re.fullmatch(r"[0-9]{3}", t):
        n = int(t)
        if 210 <= n <= 320:
            return t
        return tok
    m2 = re.fullmatch(r"([0-9]{2})(\.[05])?", t)
    if m2:
        whole = int(m2.group(1))
        if m2.group(2):
            key = f"{whole}{m2.group(2)}"
            return size_map.get(key, tok)
        if _EU_SHOE_SIZE_MIN <= whole <= _EU_SHOE_SIZE_MAX:
            return size_map.get(str(whole), tok)
    return tok


def _dedupe_str_list(values: list[str]) -> list[str]:
    """按首次出现顺序去重（尺码映射后多个欧码可能落到同一毫米值）。"""
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _shoe_sizes_to_kr_mm(tokens: list[str]) -> list[str]:
    mapped: list[str] = []
    for x in tokens:
        mapped.append(_shoe_size_token_to_kr_mm(x))
    return _dedupe_str_list(mapped)


def _dedupe_attr_map_size_lists(payload: dict[str, Any]) -> None:
    """LLM 后处理末尾：``尺码`` / ``사이즈`` 列表去重（含鞋码欧码→毫米映射之后）。"""
    am = payload.get("attr_map")
    if isinstance(am, dict):
        raw = am.get("尺码")
        if isinstance(raw, list):
            am["尺码"] = _dedupe_str_list(
                [str(x).strip() for x in raw if str(x).strip()]
            )
    ko = payload.get("attr_map_ko")
    if isinstance(ko, dict):
        raw = ko.get("사이즈")
        if isinstance(raw, list):
            ko["사이즈"] = _dedupe_str_list(
                [str(x).strip() for x in raw if str(x).strip()]
            )


_SYSTEM_GAPS = """你是电商规格/价格补全助手（仅中文）。只输出一个 JSON 对象，不要 Markdown。
仅根据商品标题（及用户消息中已给出的已知字段）补全缺失项，不要脑补。

**只输出以下中文字段**（不要输出 name_zh、desc_*、name_ko、attr_map_ko 等）：
- cny_price：字符串或 null。标题含 `Pxxx`/`pxxx` 时优先取 P 后数字为售价；原价/吊牌价等勿当作 cny_price。
- attr_map：仅中文 key `颜色`、`尺码`。鞋类尺码只写欧码数字，不要 S/M/L，不要换算毫米。
未列出的字段不要输出。"""

_SYSTEM_COPY = """你是电商上架文案与韩文翻译助手。只输出一个 JSON 对象，不要 Markdown。
总原则：高置信度（>=90%），不脑补；描述仅轻润色。

**本阶段负责中文文案 + 全部韩文翻译**（第一阶段已补好的价/中文选项见用户消息，勿改）：
- name_zh：精简中文商品名（8~20 字），上架标题用。
- desc_zh：中文描述轻润色（2~5 句）。
- desc_ko：基于 desc_zh 的韩文描述，仅翻译不增信息。
- attr_map_ko：**必须**根据用户消息中的中文 attr_map 翻译：`색상`←`颜色`，`사이즈`←`尺码`（鞋类 사이즈 仍写与尺码相同的欧码数字，勿换算毫米）。
- name_ko：可选，韩文商品名。
- cny_price：若用户消息已给出价格，原样带回。

不要修改用户消息中已固定的 `attr_map` 中文尺码/颜色；`attr_map_ko` 必须与中文选项一一对应。"""

_USER_TMPL = """请根据以下相册/微商商品标题抽取信息：

---
{title}
---
"""

def _scrape_known_fields_hint(record: dict[str, Any]) -> str:
    """抓取已确定的价/尺码/颜色（中文，阶段 2 勿改）。"""
    from product_feed_kr.wecatalog_store_record import (
        record_scrape_colors,
        record_scrape_price_raw,
        record_scrape_sizes,
    )

    parts: list[str] = []
    price = record_scrape_price_raw(record)
    if price:
        parts.append(f"【抓取已确定人民币价】{price}（cny_price 原样带回，勿改）")
    sizes = record_scrape_sizes(record)
    if sizes:
        parts.append(
            f"【抓取已确定尺码】{'、'.join(sizes)}（attr_map.尺码 中文原样，阶段2 译成 attr_map_ko.사이즈）",
        )
    colors = record_scrape_colors(record)
    if colors:
        parts.append(f"【抓取已确定颜色】{'、'.join(colors)}（attr_map.颜色 中文原样，阶段2 译成 attr_map_ko.색상）")
    if not parts:
        return ""
    return "\n\n" + "\n".join(parts) + "\n"


def _listing_llm_chinese_context_hint(ll: dict[str, Any] | None) -> str:
    """阶段 1 已写入的中文字段，供阶段 2 翻译时引用。"""
    if not isinstance(ll, dict):
        return ""
    parts: list[str] = []
    cp = ll.get("cny_price")
    if _cny_price_field_usable(cp):
        parts.append(f"【已定 cny_price】{cp}（勿改）")
    am = ll.get("attr_map")
    if isinstance(am, dict):
        sizes = am.get("尺码") or []
        if isinstance(sizes, list) and sizes:
            parts.append(f"【已定 attr_map.尺码】{'、'.join(str(x) for x in sizes)}（勿改中文；请译 attr_map_ko.사이즈）")
        colors = am.get("颜色") or []
        if isinstance(colors, list) and colors:
            parts.append(f"【已定 attr_map.颜色】{'、'.join(str(x) for x in colors)}（勿改中文；请译 attr_map_ko.색상）")
    nz = str(ll.get("name_zh") or "").strip()
    if nz:
        parts.append(f"【已有 name_zh】{nz}")
    dz = str(ll.get("desc_zh") or "").strip()
    if dz:
        parts.append(f"【已有 desc_zh】{dz[:200]}")
    if not parts:
        return ""
    return "\n\n" + "；".join(parts) + "\n"


def _listing_llm_has_sizes(ll: dict[str, Any] | None) -> bool:
    if not isinstance(ll, dict):
        return False
    am = ll.get("attr_map")
    if not isinstance(am, dict):
        return False
    sizes = am.get("尺码")
    return isinstance(sizes, list) and any(str(x).strip() for x in sizes)


def scrape_gaps_need_llm(record: dict[str, Any], listing_llm: dict[str, Any] | None = None) -> bool:
    """抓取价或尺码缺失时需要补缺 LLM。"""
    from product_feed_kr.wecatalog_store_record import record_scrape_price_raw, record_scrape_sizes

    ll = listing_llm if isinstance(listing_llm, dict) else {}
    need_price = not record_scrape_price_raw(record) and not _cny_price_field_usable(ll.get("cny_price"))
    need_sizes = not record_scrape_sizes(record) and not _listing_llm_has_sizes(ll)
    return need_price or need_sizes


def _patch_gaps_chinese_only(patch: dict[str, Any]) -> dict[str, Any]:
    """阶段 1 LLM 返回值：只保留中文价与 attr_map。"""
    out: dict[str, Any] = {}
    cp = patch.get("cny_price")
    if cp is not None:
        out["cny_price"] = cp
    am = patch.get("attr_map")
    if isinstance(am, dict):
        zh: dict[str, Any] = {}
        for key in ("颜色", "尺码"):
            if key in am:
                zh[key] = am[key]
        if zh:
            out["attr_map"] = zh
    return out


def _apply_scrape_fields_to_listing_llm(
    record: dict[str, Any],
    payload: dict[str, Any],
    *,
    listing_hint: str | None = None,
) -> bool:
    """把抓取入库的价/尺码/颜色写入 ``listing_llm``（仅中文 attr_map，不写 attr_map_ko）。"""
    from product_feed_kr.wecatalog_store_record import (
        record_scrape_colors,
        record_scrape_price_raw,
        record_scrape_sizes,
    )

    sizes = record_scrape_sizes(record)
    colors = record_scrape_colors(record)
    changed = False
    am = dict(payload.get("attr_map") or {})
    if sizes:
        am["尺码"] = list(sizes)
        changed = True
    if colors and not am.get("颜色"):
        am["颜色"] = list(colors)
        changed = True
    if changed:
        payload["attr_map"] = am
        payload["formats_source"] = "scrape"
        _dedupe_attr_map_size_lists(payload)
    pop_price = record_scrape_price_raw(record)
    if pop_price and not _cny_price_field_usable(payload.get("cny_price")):
        payload["cny_price"] = pop_price
        changed = True
    return changed


def _finalize_attr_map_ko_from_chinese(
    payload: dict[str, Any],
    *,
    listing_hint: str | None = None,
) -> None:
    """阶段 2 后：按中文 attr_map 补全/校正 attr_map_ko（鞋码→毫米由后处理完成）。"""
    am = payload.get("attr_map")
    if not isinstance(am, dict):
        return
    ko = dict(payload.get("attr_map_ko") or {})
    zh_sizes = am.get("尺码") or []
    if isinstance(zh_sizes, list) and zh_sizes:
        size_src = [str(x).strip() for x in zh_sizes if str(x).strip()]
        hint_bits: list[str] = []
        if listing_hint:
            hint_bits.append(str(listing_hint).strip())
        if _text_suggests_footwear(" ".join(hint_bits + size_src)):
            mm = _shoe_sizes_to_kr_mm(size_src)
            ko["사이즈"] = mm if mm else size_src
        elif not ko.get("사이즈"):
            ko["사이즈"] = size_src
    if ko:
        payload["attr_map_ko"] = ko
        _dedupe_attr_map_size_lists(payload)


def _merge_listing_llm_payload(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if k in ("attr_map", "attr_map_ko") and isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


def enrich_listing_scrape_gaps(
    record: dict[str, Any],
    commodity: dict[str, Any],
    *,
    timeout: float | None = None,
    api_profile: ListingLlmApiProfile | None = None,
    listing_llm_base: dict[str, Any] | None = None,
) -> bool:
    """阶段 1：仅补全缺失的价/中文尺码颜色（抓取没有时）。"""
    title = str(commodity.get("title") or "").strip()
    if not title:
        return False
    ll = dict(listing_llm_base) if isinstance(listing_llm_base, dict) else {}
    if not scrape_gaps_need_llm(record, ll):
        return True

    client, model, host = _openai_client(timeout, api_profile=api_profile)
    use_response_format = "dashscope.aliyuncs.com" not in host.lower()
    user = _USER_TMPL.format(title=title) + _scrape_known_fields_hint(record)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.gaps.request"),
                *pf_store_row_id_kv(record),
                ("model", model),
            ],
            zh="LLM 补缺：价/中文尺码",
        ),
    )
    content, elapsed_ms = _chat_once_json(
        client,
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_GAPS},
            {"role": "user", "content": user},
        ],
        use_response_format=use_response_format,
    )
    patch = _patch_gaps_chinese_only(
        parse_listing_llm_response(content, listing_hint=title),
    )
    merged = _merge_listing_llm_payload(ll, patch)
    _apply_scrape_fields_to_listing_llm(record, merged, listing_hint=title)
    record["listing_llm"] = merged
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.gaps.response"),
                *pf_store_row_id_kv(record),
                ("elapsed_ms", elapsed_ms),
                ("cny_price", merged.get("cny_price")),
            ],
            zh="LLM 补缺完成",
        ),
    )
    return True


def enrich_listing_copy(
    record: dict[str, Any],
    commodity: dict[str, Any],
    *,
    timeout: float | None = None,
    api_profile: ListingLlmApiProfile | None = None,
    listing_llm_base: dict[str, Any] | None = None,
) -> bool:
    """阶段 2：中文文案 + 韩文翻译（desc_ko、attr_map_ko 等）。"""
    title = str(commodity.get("title") or "").strip()
    if not title:
        return False
    ll = dict(listing_llm_base) if isinstance(listing_llm_base, dict) else {}
    _apply_scrape_fields_to_listing_llm(record, ll, listing_hint=title)

    client, model, host = _openai_client(timeout, api_profile=api_profile)
    use_response_format = "dashscope.aliyuncs.com" not in host.lower()
    want_vision = listing_llm_color_vision_enabled()
    user_msg = (
        _USER_TMPL.format(title=title)
        + _scrape_known_fields_hint(record)
        + _listing_llm_chinese_context_hint(ll)
    )

    data_urls: list[str] = []
    if want_vision:
        from product_feed_kr.wego_commodity import commodity_image_urls

        urls = commodity_image_urls(commodity)
        data_urls = _download_resize_jpeg_data_urls(
            urls,
            max_images=listing_llm_color_vision_max_images(),
            max_px=listing_llm_color_vision_max_px(),
        )
        if not data_urls:
            want_vision = False

    if want_vision and data_urls:
        system_text = _SYSTEM_COPY + _VISION_COLOR_SUPPLEMENT
        user_parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": user_msg.rstrip() + "\n\n（附：商品参考缩略图。）",
            },
        ]
        for durl in data_urls:
            user_parts.append({"type": "image_url", "image_url": {"url": durl}})
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_parts},
        ]
        log_zh = "LLM 文案+翻译（含缩略图）"
        vision = 1
    else:
        messages = [
            {"role": "system", "content": _SYSTEM_COPY + _TITLE_COLOR_SUPPLEMENT},
            {"role": "user", "content": user_msg},
        ]
        log_zh = "LLM 文案+翻译（纯文本）"
        vision = 0

    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.copy.request"),
                *pf_store_row_id_kv(record),
                ("model", model),
                ("vision", vision),
            ],
            zh=log_zh,
        ),
    )
    content, elapsed_ms = _chat_once_json(
        client,
        model=model,
        messages=messages,
        use_response_format=use_response_format,
    )
    patch = parse_listing_llm_response(content, listing_hint=title)
    merged = _merge_listing_llm_payload(ll, patch)
    _apply_scrape_fields_to_listing_llm(record, merged, listing_hint=title)
    _finalize_attr_map_ko_from_chinese(merged, listing_hint=title)
    merged = _normalize_llm_payload(merged, listing_hint=title)
    merged["source"] = "openai"
    merged["model"] = model
    record["listing_llm"] = merged
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.copy.response"),
                *pf_store_row_id_kv(record),
                ("elapsed_ms", elapsed_ms),
                ("name_zh_len", len(merged.get("name_zh") or "")),
                ("desc_ko_len", len(merged.get("desc_ko") or "")),
            ],
            zh="LLM 文案与韩文翻译完成",
        ),
    )
    return True


_TITLE_COLOR_SUPPLEMENT = """
【颜色仅从标题提取】未附带商品图片，attr_map「颜色」与 attr_map_ko「색상」须**只依据上方标题文本**：
- 只写标题中明确出现或可高置信推断的颜色；标题未提及或含糊时置空（[] 或省略键），不要猜。
- 不要为了凑选项而脑补颜色；仍须 >=90% 把握才写。
- 尺码、价格、名称与描述规则仍按上文；鞋类只提取欧码数字，不做毫米换算。
"""

_VISION_COLOR_SUPPLEMENT = """
【已附带商品缩略图】须同时依据**图片中清晰可见的主体颜色/配色**校正颜色选项：
- attr_map「颜色」与 attr_map_ko「색상」：只保留在图中**能明确辨认或强佐证**的颜色；标题列出但图中未见、无法确认的色**不要输出**。
- 严禁“补色”：不要为了凑全标题颜色而补写图片里没有的颜色；宁缺毋滥。
- 若无法从图片确定任何颜色，`颜色` / `색상` 置空（[] 或省略），不要猜。
- 若图与标题在颜色上冲突，以**图为准**（仍须 >=90% 把握才写）。
- 尺码、价格、名称与描述规则仍按上文；鞋类只提取欧码数字，不做毫米换算。
"""

def _strip_json_fence(s: str) -> str:
    t = s.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _normalize_llm_payload(data: dict[str, Any], *, listing_hint: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    hint_bits: list[str] = []
    if listing_hint:
        hint_bits.append(str(listing_hint).strip())
    if isinstance(data, dict):
        for k in ("name_zh", "name_ko", "desc_zh"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                hint_bits.append(v.strip()[:500])
    shoe_ctx = _text_suggests_footwear(" ".join(hint_bits))
    cp = data.get("cny_price")
    if cp is None:
        out["cny_price"] = None
    else:
        s = str(cp).strip()
        if not s or s.lower() == "null" or s == "-1":
            out["cny_price"] = None
        else:
            # 价格以 LLM 原样输出为准，不做本地二次清洗。
            out["cny_price"] = s

    def _norm_text(v: Any, max_len: int) -> str:
        t = str(v).strip() if v is not None else ""
        t = re.sub(r"[\u200b\ufeff]", "", t)
        t = re.sub(r"[#★☆🔥✨💥✅✔️]", " ", t)
        t = " ".join(t.split())
        if len(t) > max_len:
            t = t[:max_len].rstrip(" ,-/|")
        return t

    def _as_text_list(vals: Any) -> list[str]:
        out_vals: list[str] = []
        if isinstance(vals, list):
            for it in vals:
                if isinstance(it, (str, int, float)):
                    x = str(it).strip()
                    if x:
                        out_vals.append(x)
        elif isinstance(vals, (str, int, float)):
            x = str(vals).strip()
            if x:
                out_vals.append(x)
        return out_vals

    def _normalize_attr_map(
        raw: Any,
        *,
        key_max: int = 24,
        val_max: int = 40,
        ko: bool = False,
    ) -> dict[str, list[str]]:
        out_map: dict[str, list[str]] = {}
        if not isinstance(raw, dict):
            return out_map
        canonical_size = "사이즈" if ko else "尺码"
        canonical_color = "색상" if ko else "颜色"
        if ko:
            size_alias = {
                "사이즈": canonical_size,
                "치수": canonical_size,
                "규격": canonical_size,
                "尺码": canonical_size,
                "码数": canonical_size,
                "码": canonical_size,
                "尺寸": canonical_size,
                "size": canonical_size,
            }
            color_alias = {
                "색상": canonical_color,
                "컬러": canonical_color,
                "칼라": canonical_color,
                "颜色": canonical_color,
                "色": canonical_color,
                "配色": canonical_color,
                "色系": canonical_color,
            }
        else:
            size_alias = {
                "尺码": canonical_size,
                "码数": canonical_size,
                "码": canonical_size,
                "尺寸": canonical_size,
                "size": canonical_size,
                "사이즈": canonical_size,
                "치수": canonical_size,
                "규격": canonical_size,
            }
            color_alias = {
                "颜色": canonical_color,
                "色": canonical_color,
                "配色": canonical_color,
                "色系": canonical_color,
                "색상": canonical_color,
                "컬러": canonical_color,
                "칼라": canonical_color,
            }
        alias: dict[str, str] = {}
        for d in (size_alias, color_alias):
            for syn, can in d.items():
                alias[syn] = can
                if syn.isascii():
                    alias[syn.lower()] = can

        # 货源常见：「012码」= S/M/L 三档，数字按位对应 0=S、1=M、2=L、3=XL…（类推）。
        _digit_slot_sizes = (
            "S",
            "M",
            "L",
            "XL",
            "XXL",
            "XXXL",
            "4XL",
            "5XL",
            "6XL",
            "7XL",
        )

        def _expand_digit_slot_code(ds: str) -> list[str] | None:
            """若整段为数字且表示「按位多档」则展开为 S/M/L…；否则返回 None 走原样。"""
            if not ds.isdigit():
                return None
            n = len(ds)
            if n == 1:
                i = int(ds)
                if i >= len(_digit_slot_sizes):
                    return None
                return [_digit_slot_sizes[i]]
            if n == 2:
                v = int(ds)
                if 30 <= v <= 52:
                    return None
                if v in (44, 55, 66, 77, 88, 99):
                    return None
                return [_digit_slot_sizes[int(c)] for c in ds]
            if n == 3:
                v = int(ds)
                if 210 <= v <= 320:
                    return None
                if 100 <= v <= 200:
                    return None
                return [_digit_slot_sizes[int(c)] for c in ds]
            if n == 4 and 1900 <= int(ds) <= 2035:
                return None
            out: list[str] = []
            for c in ds:
                i = int(c)
                if i >= len(_digit_slot_sizes):
                    return None
                out.append(_digit_slot_sizes[i])
            return out

        def _size_tokens(text: str) -> list[str]:
            out_toks: list[str] = []
            for t in re.findall(r"(?i)EU\s*[0-9]{2}(?:\.[05])?|[0-9]{2}(?:\.[05])?|[A-Za-z0-9]+", text):
                exp = _expand_digit_slot_code(t) if t.isdigit() else None
                if exp:
                    out_toks.extend(exp)
                    continue
                v = t.upper()
                if v:
                    out_toks.append(v)
            return out_toks

        for k, vals in raw.items():
            name = _norm_text(k, key_max)
            if not name:
                continue
            key_norm = alias.get(name) or alias.get(name.lower(), "")
            if not key_norm:
                continue
            arr = _as_text_list(vals)
            if not arr:
                continue
            uniq: list[str] = []
            seen: set[str] = set()
            if key_norm == canonical_size:
                for v in arr:
                    for tok in _size_tokens(v):
                        if tok in seen:
                            continue
                        seen.add(tok)
                        uniq.append(tok)
            elif key_norm == canonical_color:
                for v in arr:
                    vv = _norm_text(v, val_max)
                    if not vv or vv in seen:
                        continue
                    seen.add(vv)
                    uniq.append(vv)
            else:
                continue
            if uniq:
                prev = out_map.get(key_norm, [])
                seen_prev = set(prev)
                out_map[key_norm] = prev + [x for x in uniq if x not in seen_prev]
        return out_map

    out["attr_map"] = _normalize_attr_map(data.get("attr_map"), ko=False)
    out["attr_map_ko"] = _normalize_attr_map(
        data.get("attr_map_ko"), ko=True, key_max=30, val_max=48
    )
    if shoe_ctx:
        size_src = out["attr_map"].get("尺码") or []
        if size_src:
            mm_sizes = _shoe_sizes_to_kr_mm(size_src)
            if mm_sizes:
                ko_map = dict(out["attr_map_ko"])
                ko_map["사이즈"] = mm_sizes
                out["attr_map_ko"] = ko_map

    _dedupe_attr_map_size_lists(out)

    name_zh = data.get("name_zh")
    out["name_zh"] = _norm_text(name_zh, 24)

    name_ko = data.get("name_ko")
    out["name_ko"] = _norm_text(name_ko, 30)

    desc_zh = data.get("desc_zh")
    out["desc_zh"] = _norm_text(desc_zh, 520)

    desc_ko = data.get("desc_ko")
    out["desc_ko"] = _norm_text(desc_ko, 620)

    return out


def apply_listing_llm_price_to_commodity(
    commodity: dict[str, Any],
    listing_llm: dict[str, Any],
    *,
    record: dict[str, Any] | None = None,
) -> None:
    """把有效的 ``listing_llm['cny_price']`` 写入 ``commodity['optimaPrice']`` 与 ``record['price_cny']``。"""
    cp = listing_llm.get("cny_price")
    if cp is None:
        commodity["optimaPrice"] = ""
        return
    s = str(cp).strip()
    if not s or s == "-1" or s.lower() == "null":
        commodity["optimaPrice"] = ""
        return
    commodity["optimaPrice"] = s
    if isinstance(record, dict):
        record["price_cny"] = s


def listing_llm_name_zh_usable(listing_llm: dict[str, Any] | None) -> bool:
    """``name_zh`` 非空（上架标题用）。"""
    if not isinstance(listing_llm, dict):
        return False
    return bool(str(listing_llm.get("name_zh") or "").strip())


def listing_llm_meets_upload_requirements(rec: dict[str, Any]) -> bool:
    """按上架关键字段判断当前记录是否“已可上架”（LLM 侧可控字段）。"""
    ll = rec.get("listing_llm")
    if not isinstance(ll, dict):
        return False
    if not str(ll.get("name_zh") or "").strip():
        return False
    if not str(ll.get("desc_ko") or "").strip():
        return False
    # 价格：优先 LLM cny_price；LLM/回退均无价时仅白名单可放行（走默认价）。
    if listing_llm_cny_usable(ll):
        return True
    from product_feed_kr.wecatalog_store_record import commodity_from_wecatalog_record
    from product_feed_kr.wego_commodity import parse_wego_product
    from product_feed_kr.wego_commodity import parse_price_str

    com = commodity_from_wecatalog_record(rec)
    if not isinstance(com, dict):
        return False
    raw = parse_price_str(rec.get("price_cny"), "")
    if raw and raw not in ("0", "0.0"):
        return True
    # 回退 2：commodity 自身可解析价格（含 title 里的 Pxxx / ￥xxx）。
    try:
        prod = parse_wego_product(com, default_price_if_missing="")
        p = str(prod.get("price") or "").strip()
        if p and p not in ("0", "0.0"):
            return True
    except ValueError:
        pass
    # LLM + 回退都无价：仅命中 map 分类白名单且默认价有效时可上架。
    if not _record_is_no_price_allowed_by_map_category(rec):
        return False
    default_price = parse_price_str(_cfg_get("SEVEN17_DEFAULT_PRICE"), "")
    return bool(default_price and default_price not in ("0", "0.0"))


def update_can_upload_flag(rec: dict[str, Any]) -> bool:
    """按当前记录状态计算并写入 ``rec['can_upload']``。"""
    ok = listing_llm_meets_upload_requirements(rec)
    rec["can_upload"] = ok
    return ok


def _cny_price_field_usable(cp: Any) -> bool:
    if cp is None:
        return False
    s = str(cp).strip()
    return bool(s and s.lower() != "null" and s != "-1")


def _no_price_allow_category_specs() -> list[str]:
    raw = str(_cfg_get("SEVEN17_NO_PRICE_ALLOW_CATEGORIES") or "").strip()
    if not raw:
        return list(_NO_PRICE_ALLOW_DEFAULT)
    out: list[str] = []
    for part in re.split(r"[,\n;\|，、]+", raw):
        tok = str(part).strip()
        if tok:
            out.append(tok)
    return out or list(_NO_PRICE_ALLOW_DEFAULT)


def _all_map_group_tag_pairs() -> tuple[tuple[str, str], ...]:
    from product_feed_kr.wecatalog_tag_mapping import mapping_rows

    out: list[tuple[str, str]] = []
    for g, t, _path, _anchor, _tid in mapping_rows():
        gs = str(g or "").strip()
        ts = str(t or "").strip()
        if gs and ts:
            out.append((gs, ts))
    return tuple(out)


def _split_group_tag_spec(spec: str) -> tuple[str, str] | None:
    text = str(spec or "").strip()
    if not text:
        return None
    for sep in ("->", ">", "｜", "|", "/", "／", "＞"):
        if sep in text:
            left, right = text.split(sep, 1)
            g = left.strip()
            t = right.strip()
            if g and t:
                return g, t
            return None
    return None


def _no_price_allow_group_tag_pairs() -> frozenset[tuple[str, str]]:
    all_pairs = _all_map_group_tag_pairs()
    all_pairs_set = set(all_pairs)
    specs = _no_price_allow_category_specs()
    allow: set[tuple[str, str]] = set()
    for spec in specs:
        pair = _split_group_tag_spec(spec)
        if pair is not None:
            if pair in all_pairs_set:
                allow.add(pair)
            continue
        # 单值视为「tag 精确名」，展开到 map 中全部同名 tag（严格等值，不做模糊）。
        tag = str(spec or "").strip()
        if not tag:
            continue
        for g, t in all_pairs:
            if t == tag:
                allow.add((g, t))
    return frozenset(allow)


def _record_is_no_price_allowed_by_map_category(rec: dict[str, Any]) -> bool:
    g = str(rec.get("wecatalog_group") or "").strip()
    t = str(rec.get("wecatalog_tag") or "").strip()
    if not g or not t:
        return False
    return (g, t) in _no_price_allow_group_tag_pairs()


def listing_llm_cny_usable(listing_llm: dict[str, Any]) -> bool:
    """``cny_price`` 可用；达次数上限但已保留价格时仍可上架。"""
    if not isinstance(listing_llm, dict):
        return False
    if not _cny_price_field_usable(listing_llm.get("cny_price")):
        return False
    src = str(listing_llm.get("source") or "")
    reason = str(listing_llm.get("reason") or "")
    if src == "openai":
        return True
    if reason in (LLM_EXHAUSTED_REASON, "max_attempts"):
        return True
    return False


def parse_listing_llm_response(text: str, *, listing_hint: str | None = None) -> dict[str, Any]:
    raw = _strip_json_fence(text)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM 返回根节点须为 JSON 对象")
    return _normalize_llm_payload(data, listing_hint=listing_hint)


def listing_llm_enabled() -> bool:
    if not listing_llm_api_profiles():
        return False
    return _cfg_bool("OPENAI_ENRICH_LISTING", True)


def listing_llm_force_refresh() -> bool:
    return _cfg_bool("OPENAI_LISTING_LLM_FORCE", False)


LLM_SKIP_SOURCE = "llm_skipped"
LLM_EXHAUSTED_REASON = "max_attempts"


def listing_llm_max_attempts() -> int:
    """单商品累计 LLM 处理次数上限（``LISTING_LLM_MAX_ATTEMPTS``，默认 1）。"""
    raw = (_cfg_get("LISTING_LLM_MAX_ATTEMPTS") or _cfg_get("LISTING_LLM_FAIL_SKIP_AFTER") or "1").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 1
    return max(1, n)


def record_llm_attempt_count(rec: dict[str, Any]) -> int:
    try:
        raw = rec.get("llm_attempt_count")
        if raw is None:
            raw = rec.get("llm_fail_count")
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def listing_llm_attempts_exhausted(rec: dict[str, Any]) -> bool:
    """已达 LLM 处理次数上限，不再处理、不可上架。"""
    return record_llm_attempt_count(rec) >= listing_llm_max_attempts()


def listing_llm_is_gave_up(rec: dict[str, Any]) -> bool:
    """已达 LLM 次数上限，或旧版 ``llm_skipped`` 永久跳过。"""
    if listing_llm_attempts_exhausted(rec):
        return True
    ll = rec.get("listing_llm")
    if not isinstance(ll, dict) or str(ll.get("source") or "") != LLM_SKIP_SOURCE:
        return False
    return bool(rec.get("llm_processed_at"))


def listing_llm_needs_api(rec: dict[str, Any]) -> bool:
    if rec.get("can_process") is False:
        return False
    if listing_llm_force_refresh():
        return True
    if listing_llm_attempts_exhausted(rec):
        return False
    if listing_llm_is_gave_up(rec):
        return False
    existing = rec.get("listing_llm")
    if isinstance(existing, dict) and rec.get("llm_processed_at"):
        return not listing_llm_meets_upload_requirements(rec)
    return True


def note_llm_attempt_consumed(record: dict[str, Any], *, error: str | None = None) -> bool:
    """
    本条计为 1 次 LLM 处理（成功/失败均计数，不重置）。
    达上限后写入 ``listing_llm``（source=llm_skipped）并标记 ``llm_processed_at``。
    返回 True 表示已达上限。
    """
    cap = listing_llm_max_attempts()
    n = record_llm_attempt_count(record) + 1
    record["llm_attempt_count"] = n
    record.pop("llm_fail_count", None)

    last_errors: list[str] = []
    prev = record.get("listing_llm")
    if isinstance(prev, dict) and isinstance(prev.get("last_errors"), list):
        last_errors = [str(x) for x in prev["last_errors"] if str(x).strip()]
    if error:
        last_errors.append(str(error)[:500])
    last_errors = last_errors[-3:]

    if n >= cap:
        now = now_cst8_iso()
        prev_ll = record.get("listing_llm") if isinstance(record.get("listing_llm"), dict) else {}
        record["listing_llm"] = {
            **prev_ll,
            "source": prev_ll.get("source") or LLM_SKIP_SOURCE,
            "reason": LLM_EXHAUSTED_REASON,
            "attempt_count": n,
            "max_attempts": cap,
            "last_errors": last_errors,
            "processed_at": now,
        }
        record["llm_processed_at"] = now
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "llm.attempts.exhausted"),
                    *pf_store_row_id_kv(record),
                    ("attempt_count", n),
                    ("max_attempts", cap),
                    ("cny_ok", 1 if listing_llm_cny_usable(record["listing_llm"]) else 0),
                    ("name_zh_ok", 1 if listing_llm_name_zh_usable(record["listing_llm"]) else 0),
                ],
                zh="LLM 处理次数已达上限，不再处理；字段齐全时仍可上架",
            ),
        )
        return True

    if error:
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "llm.attempt"),
                    *pf_store_row_id_kv(record),
                    ("attempt_count", n),
                    ("max_attempts", cap),
                    ("err", error),
                ],
                zh="LLM 本条已计入处理次数",
            ),
        )
    return False


def record_after_llm_attempt(
    record: dict[str, Any],
    commodity: dict[str, Any],
    *,
    ok: bool,
    error: str | None = None,
) -> bool:
    """处理单次 LLM 结果；返回是否应将 record 写回 SQLite。"""
    note_llm_attempt_consumed(record, error=None if ok else error)
    ll = record.get("listing_llm")
    if isinstance(ll, dict):
        apply_listing_llm_price_to_commodity(commodity, ll, record=record)
    update_can_upload_flag(record)
    return True


def _cfg_raw(key: str) -> Any:
    """读配置原始值（保留 JSON 数组/对象）；环境变量为 JSON 字符串时解析。"""
    ev = os.environ.get(key)
    if ev is not None and str(ev).strip():
        t = str(ev).strip()
        if t.startswith("[") or t.startswith("{"):
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return t
        return t
    cfg = load_seven17_config()
    return cfg.get(key)


def _coerce_json_array(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    if not isinstance(raw, str):
        return []
    t = raw.strip()
    if not t:
        return []
    if not t.startswith("["):
        return []
    try:
        parsed = json.loads(t)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []


def _api_keys_from_profile_item(item: dict[str, Any]) -> list[str]:
    """单项内 ``api_key`` 或 ``api_keys`` / ``keys`` 数组。"""
    keys: list[str] = []
    raw_list = item.get("api_keys")
    if raw_list is None:
        raw_list = item.get("keys")
    if isinstance(raw_list, list):
        for entry in raw_list:
            k = str(entry).strip()
            if k:
                keys.append(k)
    if keys:
        return keys
    single = str(item.get("api_key") or item.get("key") or "").strip()
    if single:
        keys.append(single)
    return keys


def _profiles_from_mapping(
    item: dict[str, Any],
    *,
    index: int,
    multi_groups: bool,
) -> list[ListingLlmApiProfile]:
    """同一 ``base_url`` 下可写多个 ``api_keys``，展开为多个工作线程。"""
    api_keys = _api_keys_from_profile_item(item)
    if not api_keys:
        return []
    base_raw = item.get("base_url") if item.get("base_url") is not None else item.get("baseURL")
    base = str(base_raw).strip() if base_raw is not None else ""
    if not base:
        _log.warning(
            "%s",
            pf_kv(
                [("event", "llm.profile.skip"), ("reason", "missing_base_url"), ("index", index)],
                zh="OPENAI_PROFILES 某项缺少 base_url，已跳过",
            ),
        )
        return []
    model_raw = item.get("model")
    model = str(model_raw).strip() if model_raw is not None and str(model_raw).strip() else "gpt-4o-mini"
    label_raw = item.get("label")
    if label_raw is not None and str(label_raw).strip():
        label_base = str(label_raw).strip()
    elif multi_groups or len(api_keys) > 1:
        host = urlparse(base or "").netloc if base else "default"
        label_base = f"{host or 'default'}-{index}"
    else:
        label_base = "default"
    threads_raw = item.get("threads")
    try:
        threads_per_key = int(threads_raw) if threads_raw is not None else 1
    except (TypeError, ValueError):
        threads_per_key = 1
    # 约束范围：0=禁用该 profile，1~3=有效并发，>3 按 3 处理。
    threads_per_key = min(3, max(0, threads_per_key))
    if threads_per_key == 0:
        return []
    multi_keys = len(api_keys) > 1
    profiles: list[ListingLlmApiProfile] = []
    for ki, api_key in enumerate(api_keys):
        if multi_keys:
            label = f"{label_base}-{ki}"
        else:
            label = label_base
        profiles.append(
            ListingLlmApiProfile(label=label, api_key=api_key, base_url=base, model=model, threads=threads_per_key),
        )
    return profiles


def _openai_profiles_from_openai_profiles_key() -> list[ListingLlmApiProfile]:
    """``OPENAI_PROFILES``：对象数组；每项可含 api_key 或 api_keys[]、base_url、model、label。"""
    raw = _cfg_raw("OPENAI_PROFILES")
    items = _coerce_json_array(raw)
    if not items:
        return []
    profiles: list[ListingLlmApiProfile] = []
    dict_items = [x for x in items if isinstance(x, dict)]
    multi_groups = len(dict_items) > 1 or any(
        len(_api_keys_from_profile_item(x)) > 1 for x in dict_items
    )
    group_i = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        profiles.extend(
            _profiles_from_mapping(item, index=group_i, multi_groups=multi_groups),
        )
        group_i += 1
    return profiles


def listing_llm_api_profiles() -> list[ListingLlmApiProfile]:
    """从 ``OPENAI_PROFILES`` 解析；每项可 ``api_keys: [..]`` 共享同一 base_url/model。``threads`` 控制每个 api_key 的并发线程数（默认 1）。"""
    return _openai_profiles_from_openai_profiles_key()


def listing_llm_all_profile_slots() -> list[ListingLlmApiProfile]:
    """配置中的全部厂商槽位（含未填 api_key 的项，每项至少一条），供对比脚本列出计划。"""
    raw = _cfg_raw("OPENAI_PROFILES")
    items = _coerce_json_array(raw)
    if not items:
        return []
    slots: list[ListingLlmApiProfile] = []
    dict_items = [x for x in items if isinstance(x, dict)]
    multi_groups = len(dict_items) > 1 or any(
        len(_api_keys_from_profile_item(x)) > 1 for x in dict_items
    )
    group_i = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        expanded = _profiles_from_mapping(item, index=group_i, multi_groups=multi_groups)
        if expanded:
            slots.extend(expanded)
        else:
            base_raw = item.get("base_url") if item.get("base_url") is not None else item.get("baseURL")
            base = str(base_raw).strip() if base_raw is not None else ""
            model_raw = item.get("model")
            model = str(model_raw).strip() if model_raw is not None and str(model_raw).strip() else "gpt-4o-mini"
            label_raw = item.get("label")
            label = str(label_raw).strip() if label_raw is not None and str(label_raw).strip() else f"slot-{group_i}"
            slots.append(
                ListingLlmApiProfile(label=label, api_key="", base_url=base or None, model=model, threads=1),
            )
        group_i += 1
    return slots


def _resolve_api_profile(api_profile: ListingLlmApiProfile | None) -> ListingLlmApiProfile:
    if api_profile is not None:
        return api_profile
    profiles = listing_llm_api_profiles()
    if not profiles:
        raise RuntimeError("OPENAI_PROFILES 未配置或没有有效的 api_key/base_url")
    return profiles[0]


def listing_llm_thread_count() -> int:
    return max(1, sum(p.get("threads", 1) for p in listing_llm_api_profiles()))


def listing_llm_color_vision_enabled() -> bool:
    """是否允许从商品缩略图提取/校正颜色（``OPENAI_LISTING_COLOR_VISION``）。

    - **false**（默认）：API 探测仅文生文；逐条请求；颜色由 LLM 仅从标题推断。
    - **true**：探测含图生文；逐条附带缩略图，颜色以图中可见色为准。
    """
    return _cfg_bool("OPENAI_LISTING_COLOR_VISION", False)


def listing_llm_color_vision_max_images() -> int:
    try:
        n = int(str(_cfg_get("OPENAI_LISTING_COLOR_VISION_MAX_IMAGES") or "4").strip())
    except ValueError:
        n = 4
    return max(1, min(n, 10))


def listing_llm_color_vision_max_px() -> int:
    try:
        n = int(str(_cfg_get("OPENAI_LISTING_COLOR_VISION_MAX_PX") or "256").strip())
    except ValueError:
        n = 256
    return max(64, min(n, 1024))


def _download_resize_jpeg_data_urls(urls: list[str], *, max_images: int, max_px: int) -> list[str]:
    """将远程图缩小为 JPEG，返回 data:image/jpeg;base64,... 供多模态 API。"""
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("颜色修正需 Pillow：pip install Pillow") from e
    out: list[str] = []
    for url in urls[:max_images]:
        u = str(url).strip()
        if not u.startswith(("http://", "https://")):
            continue
        try:
            req = urllib.request.Request(
                u,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
            if len(raw) > 8_000_000:
                continue
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82, optimize=True)
            b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
            out.append(f"data:image/jpeg;base64,{b64}")
        except (OSError, ValueError, TypeError, urllib.error.URLError, urllib.error.HTTPError) as e:
            _log.debug("listing_llm vision image skip: %s", str(e)[:200])
            continue
    return out


def _resolve_timeout(timeout: float | None) -> float:
    if timeout is not None:
        return timeout
    try:
        return float((_cfg_get("OPENAI_TIMEOUT") or "60").strip() or "60")
    except ValueError:
        return 60.0


def _openai_client(
    timeout: float | None,
    *,
    api_profile: ListingLlmApiProfile | None = None,
):
    prof = _resolve_api_profile(api_profile)
    api_key = prof["api_key"].strip()
    base_url = prof.get("base_url")
    model = prof.get("model") or "gpt-4o-mini"
    if not api_key:
        raise RuntimeError("OPENAI_PROFILES 某项 api_key 为空")
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("请安装 openai 库：pip install openai") from e
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=_resolve_timeout(timeout))
    bu = (base_url or "").strip()
    host = urlparse(bu).netloc if bu else "default"
    return client, model, host


def _make_probe_image_data_url() -> str:
    """生成一张 16x16 红色 JPEG 小图的 data URL，用于探测多模态能力。"""
    try:
        from PIL import Image
        buf = io.BytesIO()
        im = Image.new("RGB", (16, 16), color=(255, 0, 0))
        im.save(buf, format="JPEG", quality=80)
        b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except ImportError:
        return ""


def probe_profile(
    profile: ListingLlmApiProfile,
    *,
    timeout: float | None = None,
    test_vision: bool = True,
) -> dict[str, bool | str]:
    """对单个 API profile 做连通性探测（文生文 + 图生文），返回结果 dict。

    返回格式::

        {"text_ok": True/False, "text_error": "...",
         "vision_ok": True/False, "vision_error": "..."}
    """
    result: dict[str, bool | str] = {
        "text_ok": False, "text_error": "",
        "vision_ok": False, "vision_error": "",
    }
    label = profile.get("label", "?")
    try:
        client, model, host = _openai_client(timeout or 15, api_profile=profile)
    except Exception as e:
        err = f"客户端初始化失败: {e!s}"[:300]
        _log.warning(
            "%s",
            pf_kv(
                [("event", "llm.probe.init_fail"), ("label", label), ("err", err)],
                zh=f"探测失败（{label}）：{err}",
            ),
        )
        result["text_error"] = err
        result["vision_error"] = err
        return result

    # --- 文生文 ---
    try:
        content, elapsed = _chat_once_json(
            client,
            model=model,
            messages=[
                {"role": "system", "content": "只输出 JSON：{\"status\":\"ok\"}"},
                {"role": "user", "content": "ping"},
            ],
            use_response_format=False,
        )
        if "ok" in content.lower():
            result["text_ok"] = True
        else:
            result["text_ok"] = True
        _log.info(
            "%s",
            pf_kv(
                [("event", "llm.probe.text_ok"), ("label", label), ("model", model),
                 ("host", host), ("elapsed_ms", elapsed)],
                zh=f"文生文探测通过（{label}）",
            ),
        )
    except Exception as e:
        err = str(e)[:300]
        result["text_error"] = err
        _log.warning(
            "%s",
            pf_kv(
                [("event", "llm.probe.text_fail"), ("label", label), ("model", model),
                 ("host", host), ("err", err)],
                zh=f"文生文探测失败（{label}）：{err}",
            ),
        )

    # --- 图生文 ---
    if test_vision and result["text_ok"]:
        probe_img = _make_probe_image_data_url()
        if not probe_img:
            result["vision_error"] = "Pillow 未安装，无法生成探测图片"
            _log.warning(
                "%s",
                pf_kv(
                    [("event", "llm.probe.vision_skip"), ("label", label)],
                    zh=f"图生文探测跳过（{label}）：Pillow 未安装",
                ),
            )
        else:
            try:
                t0 = time.monotonic()
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "描述图片内容，一句话。"},
                        {"role": "user", "content": [
                            {"type": "text", "text": "这是什么颜色？"},
                            {"type": "image_url", "image_url": {"url": probe_img}},
                        ]},
                    ],
                    temperature=0.1,
                    )
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                msg_content = (getattr(resp.choices[0].message, "content", None) or "").strip()
                if msg_content:
                    result["vision_ok"] = True
                _log.info(
                    "%s",
                    pf_kv(
                        [("event", "llm.probe.vision_ok"), ("label", label), ("model", model),
                         ("host", host), ("elapsed_ms", elapsed_ms)],
                        zh=f"图生文探测通过（{label}）",
                    ),
                )
            except Exception as e:
                err = str(e)[:300]
                result["vision_error"] = err
                _log.warning(
                    "%s",
                    pf_kv(
                        [("event", "llm.probe.vision_fail"), ("label", label), ("model", model),
                         ("host", host), ("err", err)],
                        zh=f"图生文探测失败（{label}）：{err}",
                    ),
                )
    elif not result["text_ok"]:
        result["vision_error"] = "文生文未通过，跳过图生文"
    elif not test_vision:
        result["vision_ok"] = True
        _log.info(
            "%s",
            pf_kv(
                [("event", "llm.probe.vision_skip"), ("label", label),
                 ("reason", "OPENAI_LISTING_COLOR_VISION=false")],
                zh=f"图生文探测已跳过（{label}）：未开启图片颜色提取",
            ),
        )

    return result


def probe_all_profiles(
    profiles: list[ListingLlmApiProfile],
    *,
    test_vision: bool = True,
) -> list[ListingLlmApiProfile]:
    """对所有 profile 做探测，返回通过探测的 profile 列表（不通过的已 WARNING 并过滤）。

    同一 api_key + base_url 只探测一次；不同 key 并行探测。
    """
    import concurrent.futures

    if not profiles:
        return []

    unique_map: dict[tuple[str, str], ListingLlmApiProfile] = {}
    for p in profiles:
        ck = (p.get("api_key", ""), p.get("base_url") or "")
        if ck not in unique_map:
            unique_map[ck] = p

    probed: dict[tuple[str, str], dict[str, bool | str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(unique_map)) as pool:
        futures = {
            pool.submit(probe_profile, p, test_vision=test_vision): ck
            for ck, p in unique_map.items()
        }
        for fut in concurrent.futures.as_completed(futures):
            ck = futures[fut]
            probed[ck] = fut.result()

    passed: list[ListingLlmApiProfile] = []
    for p in profiles:
        label = p.get("label", "?")
        cache_key = (p.get("api_key", ""), p.get("base_url") or "")
        r = probed[cache_key]
        ok = bool(r["text_ok"]) and (not test_vision or bool(r["vision_ok"]))
        if ok:
            passed.append(p)
        else:
            _log.warning(
                "%s",
                pf_kv(
                    [("event", "llm.probe.disabled"), ("label", label),
                     ("text_error", r.get("text_error", "")),
                     ("vision_error", r.get("vision_error", ""))],
                    zh=f"API 探测未通过，本次运行已屏蔽：{label}",
                ),
            )
    unique_total = len(probed)
    unique_passed = sum(
        1
        for r in probed.values()
        if bool(r["text_ok"]) and (not test_vision or bool(r["vision_ok"]))
    )
    probe_mode_zh = "文生文+图生文" if test_vision else "仅文生文（颜色从标题提取）"
    _log.info(
        "%s",
        pf_kv(
            [("event", "llm.probe.summary"),
             ("profiles", len(profiles)),
             ("unique_apis", unique_total),
             ("apis_passed", unique_passed),
             ("threads_passed", len(passed)),
             ("color_vision", test_vision),
             ("passed_labels", ",".join(p.get("label", "?") for p in passed))],
            zh=f"API 探测完成（{probe_mode_zh}）：{unique_passed}/{unique_total} 个 API 通过，{len(passed)} 个线程可用",
        ),
    )
    return passed


def _chat_once_json(
    client,
    *,
    model: str,
    messages: list[dict[str, Any]],
    use_response_format: bool = True,
) -> tuple[str, int]:
    t0 = time.monotonic()
    if use_response_format:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
        except Exception:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.1,
            )
    else:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
        )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    msg = resp.choices[0].message
    content = (getattr(msg, "content", None) or "").strip()
    if not content:
        raise RuntimeError("LLM 返回空 content")
    return content, elapsed_ms


def enrich_record_listing_llm(
    record: dict[str, Any],
    commodity: dict[str, Any],
    *,
    timeout: float | None = None,
    api_profile: ListingLlmApiProfile | None = None,
    register_attempt: bool = True,
) -> bool:
    """
    将 LLM 结果写入 ``record['listing_llm']``；识别到价则写入 ``commodity['optimaPrice']``，否则 ``cny_price`` 为 ``null`` 并清空 ``optimaPrice``。
    若已有 ``listing_llm`` 且未设置 ``OPENAI_LISTING_LLM_FORCE``，则不调 API，但仍会把缓存价同步到 ``optimaPrice``。
    返回 **True** 表示应写回 store-json（内存中 ``raw`` 已改）；无 title 等放弃时为 **False**。
    """
    title = str(commodity.get("title") or "").strip()
    log_item_separator(_log)
    if not title:
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "llm.skip"),
                    ("reason", "no_title"),
                    *pf_store_row_id_kv(record),
                ],
                zh="跳过 LLM：商品无标题",
            ),
        )
        return False

    if listing_llm_attempts_exhausted(record):
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "llm.skip"),
                    ("reason", "attempts_exhausted"),
                    *pf_store_row_id_kv(record),
                    ("attempt_count", record_llm_attempt_count(record)),
                    ("max_attempts", listing_llm_max_attempts()),
                ],
                zh="跳过 LLM：该商品处理次数已达上限",
            ),
        )
        return False

    existing = record.get("listing_llm")
    ll_base: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    _apply_scrape_fields_to_listing_llm(record, ll_base, listing_hint=title)

    if isinstance(existing, dict) and record.get("llm_processed_at") and not listing_llm_force_refresh():
        record["listing_llm"] = ll_base
        apply_listing_llm_price_to_commodity(commodity, ll_base, record=record)
        update_can_upload_flag(record)
        if listing_llm_meets_upload_requirements(record):
            _log.info(
                "%s",
                pf_kv(
                    [("event", "llm.cache"), *pf_store_row_id_kv(record)],
                    zh="使用已有 LLM 缓存，未重新请求接口",
                ),
            )
            return True
        _log.warning(
            "%s",
            pf_kv(
                [("event", "llm.cache.incomplete"), *pf_store_row_id_kv(record)],
                zh="已有 LLM 缓存但未达上架条件，将重新走 LLM",
            ),
        )
        ll_base = dict(record.get("listing_llm") or {})

    log_item_separator(_log)
    if not enrich_listing_scrape_gaps(
        record,
        commodity,
        timeout=timeout,
        api_profile=api_profile,
        listing_llm_base=ll_base,
    ):
        return False
    ll_mid = record.get("listing_llm")
    if not isinstance(ll_mid, dict):
        ll_mid = ll_base
    if not enrich_listing_copy(
        record,
        commodity,
        timeout=timeout,
        api_profile=api_profile,
        listing_llm_base=ll_mid,
    ):
        return False

    ll_final = record.get("listing_llm")
    if not isinstance(ll_final, dict):
        return False
    ll_final["processed_at"] = now_cst8_iso()
    record["listing_llm"] = ll_final
    record["llm_processed_at"] = ll_final["processed_at"]
    if register_attempt:
        note_llm_attempt_consumed(record)
    apply_listing_llm_price_to_commodity(commodity, ll_final, record=record)
    update_can_upload_flag(record)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.done"),
                *pf_store_row_id_kv(record),
                ("cny_price", ll_final.get("cny_price")),
                ("name_zh_len", len(ll_final.get("name_zh") or "")),
                ("desc_ko_len", len(ll_final.get("desc_ko") or "")),
            ],
            zh="LLM 两阶段处理完成",
        ),
    )
    return True

