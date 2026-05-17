"""上架前：根据货源 ``commodity.title``（可选附带缩略图）调用 OpenAI 兼容接口，抽取结构化字段写入 ``record['listing_llm']``，
并把识别到的人民币价写入 ``commodity.optimaPrice``；未识别时 ``listing_llm.cny_price`` 为 JSON ``null``（Python ``None``），
``optimaPrice`` 置空字符串，上架脚本按 ``null`` 跳过。
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


class ListingLlmApiProfile(TypedDict):
    label: str
    api_key: str
    base_url: str | None
    model: str


# 鞋类语境：用于 attr_map_ko 中欧码→韩版毫米标换算（与 name/desc 合并判断）。
_FOOTWEAR_HINT_RE = re.compile(
    r"鞋|靴|拖|sneaker|boot|loafer|sandal|heel|flip\s*flop|"
    r"运动(?:鞋|靴)|休闲鞋|板鞋|跑鞋|球鞋|帆布|高跟|凉鞋|乐福|穆勒|豆豆|马丁|"
    r"운동화|신발|샌들|부츠|슬리퍼|구두|스니커|워커",
    re.I,
)

# 欧码整数 → 韩国通贩常用脚长毫米标（与多数品牌对照表接近；半码见 _EU_HALF_TO_KR_MM）。
_EU_INT_TO_KR_MM: dict[int, str] = {
    33: "215",
    34: "220",
    35: "225",
    36: "230",
    37: "235",
    38: "240",
    39: "245",
    40: "250",
    41: "255",
    42: "260",
    43: "265",
    44: "270",
    45: "275",
    46: "280",
    47: "285",
    48: "290",
}

_EU_HALF_TO_KR_MM: dict[str, str] = {
    "33.5": "220",
    "34.5": "225",
    "35.5": "230",
    "36.5": "235",
    "37.5": "240",
    "38.5": "245",
    "39.5": "250",
    "40.5": "255",
    "41.5": "260",
    "42.5": "265",
    "43.5": "270",
    "44.5": "275",
    "45.5": "280",
    "46.5": "285",
}


def _text_suggests_footwear(blob: str) -> bool:
    if not blob or not isinstance(blob, str):
        return False
    return bool(_FOOTWEAR_HINT_RE.search(blob))


def _shoe_size_token_to_kr_mm(tok: str) -> str:
    """鞋类语境下：欧码/「EU42」等 → 韩版毫米字符串；已是 3 位 mm 或字母尺码则不变。"""
    t0 = str(tok).strip()
    if not t0:
        return tok
    m_eu = re.fullmatch(r"(?i)EU\s*([0-9]{2})(\.[05])?", t0)
    if m_eu:
        whole = int(m_eu.group(1))
        frac = m_eu.group(2)
        if frac:
            key = f"{whole}{frac}"
            return _EU_HALF_TO_KR_MM.get(key, tok)
        if 33 <= whole <= 48:
            return _EU_INT_TO_KR_MM.get(whole, tok)
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
            return _EU_HALF_TO_KR_MM.get(key, tok)
        if 33 <= whole <= 48:
            return _EU_INT_TO_KR_MM.get(whole, tok)
    return tok


_SYSTEM = """你是电商商品信息抽取助手。只输出一个 JSON 对象，不要 Markdown 围栏，不要多余说明。
总原则（必须遵守）：
- 只允许基于输入做“高置信度提取”，不要脑补、不要扩写背景信息。
- 置信度门槛：只有你对某条信息把握 >=90% 才可输出；低于 90% 则留空（"" / {} / null）。
- 描述只做“轻润色”：允许去 emoji、去重复口号、调顺语序；不允许新增原文没有的卖点。

字段要求：
- cny_price：字符串或 null。能确定人民币售价时填数字字符串（如 "340"）；**完全无法判断时请填 null**（不要猜价）。
- attr_map：对象，**下单必选项**，仅含 **颜色**、**尺码** 两类 key（中文名）：`颜色`、`尺码`。
  - `颜色`：value 为颜色字符串数组（如 ["灰","黑"]）；标题或图中无法佐证的色不要写。
  - `尺码`：value 为尺码字符串数组；尺码值以字母规格为主（S/M/L/XL…）；「012码」「0123码」表示多档（0=S、1=M、2=L、3=XL…），优先直接输出展开后的数组。
  - 无可抽取项时对应 key 可省略或填 []；两者皆无时 attr_map 为 {}。
- attr_map_ko：对象，与 attr_map 对齐，key 仅用韩文 **`색상`**、**`사이즈`**，value 为韩文或通用尺码符号数组（색상 값尽量用韩文色名）。
  - **鞋类**（标题含鞋/靴/运动鞋 등）：`사이즈` 请用韩国通贩 **毫米脚长**（230、235…）；欧码两位数字可先写欧码，后处理会换算毫米。
  - 无对应项时填 {} 或省略 key。
- name_zh：字符串，必须精简为核心中文商品名（尽量 8~20 字），去掉营销词、emoji、口号、重复品牌/型号堆砌。
- name_ko：字符串，韩文精简商品名（尽量 8~24 字），同样去掉营销冗余。**只要输出了非空的 name_zh，就必须同时给出对应的韩文 name_ko（不得留空）**；仅当整段标题无法提炼中文名时才允许 name_ko 为 ""。
- desc_zh：字符串，来自标题原文的中文描述提取与轻润色（建议 2~5 句，约 80~220 字）。
  - 尽量保留原文里的核心信息：款式/设计/搭配/赠品等；颜色若已在 attr_map 中列出，描述里不必重复罗列色表。
  - 不确定或原文缺失的信息不要补写。
- desc_ko：字符串，基于 desc_zh 的韩文等价表达（建议 2~5 句，约 90~260 字），仅翻译与轻润色，不新增信息。
"""

_USER_TMPL = """请根据以下相册/微商商品标题抽取信息：

---
{title}
---
"""

_VISION_COLOR_SUPPLEMENT = """
【已附带商品缩略图】须同时依据**图片中清晰可见的主体颜色/配色**校正颜色选项：
- attr_map「颜色」与 attr_map_ko「색상」：只保留在图中**能明确辨认或强佐证**的颜色；标题列出但图中未见、无法确认的色**不要输出**。
- 若图与标题在颜色上冲突，以**图为准**（仍须 >=90% 把握才写）。
- 尺码、价格、名称与描述规则仍按上文；鞋类毫米规则不变。
"""

_SYSTEM_BATCH = """你是电商商品信息抽取助手。输入是一个 JSON 数组，每项含 idx、goods_id、title。
请输出一个 JSON 对象，格式固定为：
{"items":[{"idx":1,"cny_price":"340","attr_map":{"颜色":["黑"],"尺码":["M","L"]},"attr_map_ko":{"색상":["블랙"],"사이즈":["M","L"]},"name_zh":"...","name_ko":"...","desc_zh":"...","desc_ko":"..."}]}

要求：
- 只允许基于输入 title 做提取；不要脑补。
- 置信度 <90% 的字段不要填（按类型返回空值）。
- items 必须是数组，且每个 idx 必须对应输入的 idx。
- cny_price：字符串或 null。能确定人民币售价时填数字字符串；完全无法判断填 null，不要猜价。
- attr_map：仅 **颜色**、**尺码** 两个中文 key；无则 {} 或省略键。尺码值不要带「码」字后缀；「012码」类可写 ["S","M","L"] 或数字串（后处理按位展开）。
- attr_map_ko：仅 **색상**、**사이즈**；与 attr_map 颜色/尺码一一对应（顺序一致）。鞋类 `사이즈` 用毫米脚长。
- name_zh / name_ko / desc_zh / desc_ko 同单条模式；有 name_zh 时 name_ko 必填韩文译名。
- 只输出 JSON，不要 Markdown、不要额外解释。
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
        shoe_kr_mm: bool = False,
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
            for t in re.findall(r"[A-Za-z0-9]+", text):
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
                if uniq and ko and shoe_kr_mm:
                    conv: list[str] = []
                    seen_mm: set[str] = set()
                    for x in uniq:
                        y = _shoe_size_token_to_kr_mm(x)
                        if y not in seen_mm:
                            seen_mm.add(y)
                            conv.append(y)
                    uniq = conv
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

    out["attr_map"] = _normalize_attr_map(data.get("attr_map"), ko=False, shoe_kr_mm=False)
    out["attr_map_ko"] = _normalize_attr_map(
        data.get("attr_map_ko"), ko=True, key_max=30, val_max=48, shoe_kr_mm=shoe_ctx
    )

    name_zh = data.get("name_zh")
    out["name_zh"] = _norm_text(name_zh, 24)

    name_ko = data.get("name_ko")
    out["name_ko"] = _norm_text(name_ko, 30)

    desc_zh = data.get("desc_zh")
    out["desc_zh"] = _norm_text(desc_zh, 520)

    desc_ko = data.get("desc_ko")
    out["desc_ko"] = _norm_text(desc_ko, 620)

    return out


def apply_listing_llm_price_to_commodity(commodity: dict[str, Any], listing_llm: dict[str, Any]) -> None:
    """把有效的 ``listing_llm['cny_price']`` 写入 ``commodity['optimaPrice']``；否则清空 ``optimaPrice``（表示无 LLM 价）。"""
    cp = listing_llm.get("cny_price")
    if cp is None:
        commodity["optimaPrice"] = ""
        return
    s = str(cp).strip()
    if not s or s == "-1" or s.lower() == "null":
        commodity["optimaPrice"] = ""
        return
    commodity["optimaPrice"] = s


def listing_llm_name_ko_usable(listing_llm: dict[str, Any] | None) -> bool:
    """``name_ko`` 非空且含韩文字符（上架标题用，不用 ``name_zh`` 顶替）。"""
    if not isinstance(listing_llm, dict):
        return False
    nk = str(listing_llm.get("name_ko") or "").strip()
    if not nk:
        return False
    return any("\uac00" <= ch <= "\ud7a3" for ch in nk)


def listing_llm_needs_name_ko(
    listing_llm: dict[str, Any] | None,
    *,
    title: str = "",
) -> bool:
    """已有 LLM 结果但缺可用韩文标题，且仍有中文名或原标题可译。"""
    if listing_llm_name_ko_usable(listing_llm):
        return False
    if not isinstance(listing_llm, dict):
        return bool(str(title or "").strip())
    src = str(listing_llm.get("name_zh") or "").strip() or str(title or "").strip()
    return bool(src)


def _cny_price_field_usable(cp: Any) -> bool:
    if cp is None:
        return False
    s = str(cp).strip()
    return bool(s and s.lower() != "null" and s != "-1")


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
    """单商品累计 LLM 处理次数上限（``LISTING_LLM_MAX_ATTEMPTS``，默认 3）。"""
    raw = (_cfg_get("LISTING_LLM_MAX_ATTEMPTS") or _cfg_get("LISTING_LLM_FAIL_SKIP_AFTER") or "3").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 3
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
    if listing_llm_force_refresh():
        return True
    if listing_llm_attempts_exhausted(rec):
        return False
    if listing_llm_is_gave_up(rec):
        return False
    existing = rec.get("listing_llm")
    if isinstance(existing, dict) and rec.get("llm_processed_at"):
        from product_feed_kr.wecatalog_store_record import commodity_from_wecatalog_record

        com = commodity_from_wecatalog_record(rec)
        title = str(com.get("title") or "").strip() if isinstance(com, dict) else ""
        if listing_llm_needs_name_ko(existing, title=title):
            return True
        return False
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
                    ("name_ko_ok", 1 if listing_llm_name_ko_usable(record["listing_llm"]) else 0),
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
        apply_listing_llm_price_to_commodity(commodity, ll)
    return True


def listing_llm_batch_size() -> int:
    raw = (_cfg_get("OPENAI_LISTING_LLM_BATCH_SIZE") or "12").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 12
    return max(1, min(n, 50))


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
    multi_keys = len(api_keys) > 1
    profiles: list[ListingLlmApiProfile] = []
    for ki, api_key in enumerate(api_keys):
        if multi_keys:
            label = f"{label_base}-{ki}"
        else:
            label = label_base
        profiles.append(
            ListingLlmApiProfile(label=label, api_key=api_key, base_url=base, model=model),
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
    """从 ``OPENAI_PROFILES`` 解析；每项可 ``api_keys: [..]`` 共享同一 base_url/model。每个 api_key 一线程。"""
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
                ListingLlmApiProfile(label=label, api_key="", base_url=base or None, model=model),
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
    return max(1, len(listing_llm_api_profiles()))


def listing_llm_color_vision_enabled() -> bool:
    """为 true 时 LLM 单条请求附带商品缩略图，用于校正 attr_map 颜色（关闭 JSON 批量接口）。"""
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


_SYSTEM_NAME_KO_ONLY = """你是电商标题翻译助手。只输出一个 JSON 对象，不要 Markdown。
格式：{"name_ko":"韩文精简商品名"}
将输入的中文商品名译为韩文标题（约 8~24 字），去掉营销词与 emoji，不新增原文没有的信息。"""


def _parse_name_ko_only_response(content: str) -> str:
    raw = _strip_json_fence(content)
    data = json.loads(raw)
    if not isinstance(data, dict):
        return ""
    nk = data.get("name_ko")
    t = str(nk).strip() if nk is not None else ""
    t = re.sub(r"[\u200b\ufeff]", "", t)
    t = " ".join(t.split())
    if len(t) > 30:
        t = t[:30].rstrip(" ,-/|")
    return t


def _translate_name_to_ko(
    source_text: str,
    *,
    timeout: float | None = None,
    api_profile: ListingLlmApiProfile | None = None,
) -> str | None:
    """将中文商品名译为韩文 ``name_ko``；失败返回 None。"""
    src = str(source_text or "").strip()
    if not src:
        return None
    client, model, host = _openai_client(timeout, api_profile=api_profile)
    use_response_format = "dashscope.aliyuncs.com" not in host.lower()
    messages = [
        {"role": "system", "content": _SYSTEM_NAME_KO_ONLY},
        {"role": "user", "content": f"中文商品名：\n{src}"},
    ]
    content, _elapsed_ms = _chat_once_json(
        client,
        model=model,
        messages=messages,
        use_response_format=use_response_format,
    )
    nk = _parse_name_ko_only_response(content)
    if nk and listing_llm_name_ko_usable({"name_ko": nk}):
        return nk
    return None


def ensure_listing_llm_name_ko(
    record: dict[str, Any],
    commodity: dict[str, Any],
    *,
    timeout: float | None = None,
    api_profile: ListingLlmApiProfile | None = None,
    count_attempt: bool = True,
) -> bool:
    """若 ``listing_llm`` 缺可用 ``name_ko``，用 LLM 从 ``name_zh`` 或原标题补译；成功返回 True。"""
    if listing_llm_attempts_exhausted(record):
        return False
    ll = record.get("listing_llm")
    if not isinstance(ll, dict):
        ll = {}
        record["listing_llm"] = ll
    if listing_llm_name_ko_usable(ll):
        return True
    title = str(commodity.get("title") or "").strip()
    src = str(ll.get("name_zh") or "").strip() or title
    if not src:
        return False
    nk = _translate_name_to_ko(src, timeout=timeout, api_profile=api_profile)
    if not nk:
        return False
    ll["name_ko"] = nk
    record["listing_llm"] = ll
    record["llm_processed_at"] = now_cst8_iso()
    ll["processed_at"] = record["llm_processed_at"]
    if count_attempt:
        note_llm_attempt_consumed(record)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.name_ko.fill"),
                *pf_store_row_id_kv(record),
                ("name_ko_len", len(nk)),
                ("source", "name_zh" if str(ll.get("name_zh") or "").strip() else "title"),
            ],
            zh="已用 LLM 补译韩文商品名（未用中文名直接上架）",
        ),
    )
    return True


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


def enrich_records_listing_llm_batch(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    timeout: float | None = None,
    batch_size: int | None = None,
    api_profile: ListingLlmApiProfile | None = None,
) -> list[dict[str, Any]]:
    """批量 enrich 多条 (record, commodity)，返回发生变更、建议写回存储层的 record 列表。"""
    if not rows:
        return []

    changed: list[dict[str, Any]] = []
    need_call: list[tuple[int, dict[str, Any], dict[str, Any], str]] = []

    for i, (record, commodity) in enumerate(rows):
        title = str(commodity.get("title") or "").strip()
        if not title:
            _log.warning(
                "%s",
                pf_kv(
                    [("event", "llm.skip"), ("reason", "no_title"), *pf_store_row_id_kv(record)],
                    zh="跳过 LLM：商品无标题",
                ),
            )
            continue
        existing = record.get("listing_llm")
        if isinstance(existing, dict) and record.get("llm_processed_at") and not listing_llm_force_refresh():
            apply_listing_llm_price_to_commodity(commodity, existing)
            changed.append(record)
            _log.info(
                "%s",
                pf_kv(
                    [("event", "llm.cache"), *pf_store_row_id_kv(record)],
                    zh="使用已有 LLM 缓存，未重新请求接口",
                ),
            )
            continue
        need_call.append((i, record, commodity, title))

    if not need_call:
        return changed

    if listing_llm_color_vision_enabled():
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "llm.batch.bypass"),
                    ("reason", "OPENAI_LISTING_COLOR_VISION"),
                    ("n", len(need_call)),
                ],
                zh="已开启颜色缩略图修正，改为逐条调用（不走 JSON 批量）",
            ),
        )
        for _idx, record, commodity, _title in need_call:
            try:
                ok = enrich_record_listing_llm(record, commodity, timeout=timeout, api_profile=api_profile)
            except Exception as e:
                if record_after_llm_attempt(record, commodity, ok=False, error=str(e)):
                    changed.append(record)
                continue
            if record_after_llm_attempt(
                record,
                commodity,
                ok=ok,
                error=None if ok else "enrich_returned_false",
            ):
                changed.append(record)
        return changed

    client, model, host = _openai_client(timeout, api_profile=api_profile)
    size = batch_size or listing_llm_batch_size()
    use_response_format = "dashscope.aliyuncs.com" not in host.lower()

    for offset in range(0, len(need_call), size):
        chunk = need_call[offset : offset + size]
        log_item_separator(_log)
        payload = [
            {
                "idx": idx,
                "goods_id": pf_goods_id(record),
                "title": title,
            }
            for idx, record, _commodity, title in chunk
        ]
        batch_log: list[tuple[str, Any]] = [
            ("event", "llm.batch.request"),
            ("model", model),
            ("host", host),
            ("batch", len(payload)),
        ]
        if api_profile is not None:
            batch_log.append(("thread", api_profile.get("label", "")))
        _log.info(
            "%s",
            pf_kv(batch_log, zh="批量调用 OpenAI 解析上架标题"),
        )
        messages = [
            {"role": "system", "content": _SYSTEM_BATCH},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            content, elapsed_ms = _chat_once_json(
                client,
                model=model,
                messages=messages,
                use_response_format=use_response_format,
            )
            data = json.loads(_strip_json_fence(content))
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("批量返回缺少 items 数组")
            by_idx: dict[int, dict[str, Any]] = {}
            titles_by_idx = {idx: title for idx, _r, _c, title in chunk}
            for it in items:
                if not isinstance(it, dict):
                    continue
                idx = it.get("idx")
                if not isinstance(idx, int):
                    continue
                by_idx[idx] = _normalize_llm_payload(it, listing_hint=titles_by_idx.get(idx))

            for idx, record, commodity, _title in chunk:
                norm = by_idx.get(idx)
                if norm is None:
                    try:
                        ok = enrich_record_listing_llm(
                            record,
                            commodity,
                            timeout=timeout,
                            api_profile=api_profile,
                        )
                    except Exception as e:
                        if record_after_llm_attempt(record, commodity, ok=False, error=str(e)):
                            changed.append(record)
                        continue
                    if record_after_llm_attempt(
                        record,
                        commodity,
                        ok=ok,
                        error=None if ok else "batch_item_missing",
                    ):
                        changed.append(record)
                    continue
                norm["source"] = "openai"
                norm["model"] = model
                record["listing_llm"] = norm
                ensure_listing_llm_name_ko(
                    record,
                    commodity,
                    timeout=timeout,
                    api_profile=api_profile,
                    count_attempt=False,
                )
                ll_final = record.get("listing_llm")
                if not isinstance(ll_final, dict):
                    ll_final = norm
                ll_final["processed_at"] = now_cst8_iso()
                record["listing_llm"] = ll_final
                record["llm_processed_at"] = ll_final["processed_at"]
                note_llm_attempt_consumed(record)
                apply_listing_llm_price_to_commodity(commodity, ll_final)
                changed.append(record)
                _log.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "llm.response"),
                            *pf_store_row_id_kv(record),
                            ("elapsed_ms", elapsed_ms),
                            ("cny_price", norm.get("cny_price")),
                            ("optimaPrice", commodity.get("optimaPrice")),
                            ("attr_keys", ",".join((norm.get("attr_map") or {}).keys())),
                            ("attr_ko_keys", ",".join((norm.get("attr_map_ko") or {}).keys())),
                            ("name_ko_len", len(norm.get("name_ko") or "")),
                            ("desc_ko_len", len(norm.get("desc_ko") or "")),
                        ],
                        zh="LLM 批量返回已解析并写回记录",
                    ),
                )
        except Exception as e:
            _log.warning(
                "%s",
                pf_kv(
                    [("event", "llm.batch.fallback"), ("err", str(e)), ("batch", len(payload))],
                    zh="批量解析失败，回退为单条调用",
                ),
            )
            for _idx, record, commodity, _title in chunk:
                try:
                    ok = enrich_record_listing_llm(record, commodity, timeout=timeout, api_profile=api_profile)
                except Exception as e:
                    if record_after_llm_attempt(record, commodity, ok=False, error=str(e)):
                        changed.append(record)
                    continue
                if record_after_llm_attempt(
                    record,
                    commodity,
                    ok=ok,
                    error=None if ok else "batch_fallback_failed",
                ):
                    changed.append(record)

    return changed


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
    if isinstance(existing, dict) and record.get("llm_processed_at") and not listing_llm_force_refresh():
        apply_listing_llm_price_to_commodity(commodity, existing)
        if listing_llm_name_ko_usable(existing):
            _log.info(
                "%s",
                pf_kv(
                    [
                        ("event", "llm.cache"),
                        *pf_store_row_id_kv(record),
                    ],
                    zh="使用已有 LLM 缓存，未重新请求接口",
                ),
            )
            return True
        if ensure_listing_llm_name_ko(
            record,
            commodity,
            timeout=timeout,
            api_profile=api_profile,
            count_attempt=True,
        ):
            apply_listing_llm_price_to_commodity(commodity, record["listing_llm"])
            return True
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "llm.name_ko.missing"),
                    *pf_store_row_id_kv(record),
                ],
                zh="缓存无韩文标题，将重新走完整 LLM",
            ),
        )

    client, model, host = _openai_client(timeout, api_profile=api_profile)
    use_response_format = "dashscope.aliyuncs.com" not in host.lower()
    want_vision = listing_llm_color_vision_enabled()
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
            _log.warning(
                "%s",
                pf_kv(
                    [
                        ("event", "llm.vision_no_images"),
                        *pf_store_row_id_kv(record),
                        ("url_candidates", len(urls)),
                    ],
                    zh="颜色修正已开启但未得到有效缩略图，退回纯文本请求",
                ),
            )
            want_vision = False

    if want_vision and data_urls:
        system_text = _SYSTEM + _VISION_COLOR_SUPPLEMENT
        user_msg = _USER_TMPL.format(title=title)
        user_parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": user_msg.rstrip() + "\n\n（附：商品参考缩略图，已缩小分辨率。）",
            },
        ]
        for durl in data_urls:
            user_parts.append({"type": "image_url", "image_url": {"url": durl}})
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_parts},
        ]
        log_kv: list[tuple[str, Any]] = [
            ("event", "llm.request"),
            *pf_store_row_id_kv(record),
            ("model", model),
            ("host", host),
            ("title_len", len(title)),
            ("vision", 1),
            ("vision_images", len(data_urls)),
        ]
        if api_profile is not None:
            log_kv.append(("thread", api_profile.get("label", "")))
        log_zh = "正在调用 OpenAI 丰富上架字段（含颜色缩略图）"
    else:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER_TMPL.format(title=title)},
        ]
        log_kv = [
            ("event", "llm.request"),
            *pf_store_row_id_kv(record),
            ("model", model),
            ("host", host),
            ("title_len", len(title)),
        ]
        if api_profile is not None:
            log_kv.append(("thread", api_profile.get("label", "")))
        log_zh = "正在调用 OpenAI 丰富上架字段（标题价颜色尺码等）"
    _log.info("%s", pf_kv(log_kv, zh=log_zh))
    content, elapsed_ms = _chat_once_json(
        client,
        model=model,
        messages=messages,
        use_response_format=use_response_format,
    )

    normalized = parse_listing_llm_response(content, listing_hint=title)
    normalized["source"] = "openai"
    normalized["model"] = model
    record["listing_llm"] = normalized
    ensure_listing_llm_name_ko(
        record,
        commodity,
        timeout=timeout,
        api_profile=api_profile,
        count_attempt=False,
    )
    ll_final = record.get("listing_llm")
    if not isinstance(ll_final, dict):
        ll_final = normalized
    ll_final["processed_at"] = now_cst8_iso()
    record["listing_llm"] = ll_final
    record["llm_processed_at"] = ll_final["processed_at"]
    if register_attempt:
        note_llm_attempt_consumed(record)
    apply_listing_llm_price_to_commodity(commodity, ll_final)
    normalized = ll_final
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.response"),
                *pf_store_row_id_kv(record),
                ("elapsed_ms", elapsed_ms),
                ("cny_price", normalized.get("cny_price")),
                ("optimaPrice", commodity.get("optimaPrice")),
                ("attr_keys", ",".join((normalized.get("attr_map") or {}).keys())),
                ("attr_ko_keys", ",".join((normalized.get("attr_map_ko") or {}).keys())),
                ("name_ko_len", len(normalized.get("name_ko") or "")),
                ("desc_ko_len", len(normalized.get("desc_ko") or "")),
            ],
            zh="LLM 返回已解析并写回记录",
        ),
    )
    return True

