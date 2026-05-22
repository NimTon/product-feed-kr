"""上架前 LLM（每条商品 **一次** Chat）：

输入**原始标题** + **当前已整理字段**，由模型**翻译与纠错**。
**尺码**：LLM 只改中文 ``attr_map.尺码``；**不要**输出 ``attr_map_ko.사이즈``。
颜色：``OPENAI_LISTING_COLOR_VISION=false`` 时从原文明确销售色提取；``true`` 时仅从附图九宫格识别（忽略标题）。
LLM 返回后由 ``wecatalog_size_fix`` 根据中文尺码生成韩文 ``사이즈``（鞋类→毫米）。
"""

from __future__ import annotations

import base64
import copy
import io
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from product_feed_kr.pf_time import now_cst8_iso
from typing import Any, TypedDict
from urllib.parse import urlparse

from product_feed_kr.seven17_config import bool_env as _cfg_bool
from product_feed_kr.seven17_config import getenv as _cfg_get
from product_feed_kr.seven17_config import load_seven17_config
from product_feed_kr.pf_log import (
    log_item_separator,
    pf_goods_id,
    pf_kv,
    pf_store_row_id_kv,
    pf_trunc,
)

_log = logging.getLogger(__name__)

_token_usage_lock = threading.Lock()
_token_usage_run: dict[str, float | int] = {
    "requests": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "cost_yuan": 0.0,
}


def reset_llm_token_usage_run() -> None:
    """新一轮 ``seven17_llm`` / 批量 enrich 开始前清零累计。"""
    with _token_usage_lock:
        for k in _token_usage_run:
            _token_usage_run[k] = 0 if k != "cost_yuan" else 0.0


def note_llm_token_usage(usage: dict[str, int] | None) -> None:
    if not usage:
        return
    from product_feed_kr.llm_token_billing import llm_input_cost_yuan

    pt = int(usage.get("prompt_tokens") or 0)
    cost = llm_input_cost_yuan(pt)
    with _token_usage_lock:
        _token_usage_run["requests"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            _token_usage_run[k] = int(_token_usage_run[k]) + int(usage.get(k) or 0)
        _token_usage_run["cost_yuan"] = float(_token_usage_run["cost_yuan"]) + cost


def _log_llm_call_usage(
    usage: dict[str, int] | None,
    *,
    model: str,
    elapsed_ms: int,
    call_kind: str = "listing",
    record: dict[str, Any] | None = None,
    track_usage: bool = True,
    extra_kv: list[tuple[str, Any]] | None = None,
) -> None:
    """每次 Chat API 调用后记录 token 与费用（``event=llm.usage.call``）。"""
    from product_feed_kr.llm_token_billing import (
        format_cost_yuan,
        llm_input_cost_tier_label,
        llm_input_cost_yuan,
        llm_input_price_per_million,
    )

    pt = int((usage or {}).get("prompt_tokens") or 0)
    ct = int((usage or {}).get("completion_tokens") or 0)
    tt = int((usage or {}).get("total_tokens") or 0)
    if not usage or (pt == 0 and ct == 0 and tt == 0):
        pairs: list[tuple[str, Any]] = [
            ("event", "llm.usage.call"),
            ("call", call_kind),
            ("model", model),
            ("elapsed_ms", elapsed_ms),
            ("billing", "no_usage"),
        ]
        if record is not None:
            pairs.extend(pf_store_row_id_kv(record))
        if extra_kv:
            pairs.extend(extra_kv)
        _log.warning(
            "%s",
            pf_kv(pairs, zh="LLM 调用完成但 API 未返回 usage，无法计费"),
        )
        return

    req_cost = llm_input_cost_yuan(pt)
    pairs = [
        ("event", "llm.usage.call"),
        ("call", call_kind),
        ("model", model),
        ("elapsed_ms", elapsed_ms),
        ("prompt_tokens", pt),
        ("completion_tokens", ct),
        ("total_tokens", tt or (pt + ct)),
        ("input_tier", llm_input_cost_tier_label(pt)),
        ("input_price_per_m", llm_input_price_per_million(pt)),
        ("cost_yuan", format_cost_yuan(req_cost)),
    ]
    if track_usage:
        snap = llm_token_usage_run_snapshot()
        if snap.get("requests"):
            pairs.append(("run_cost_yuan", format_cost_yuan(float(snap.get("cost_yuan") or 0))))
    else:
        pairs.append(("run_total", "0"))
    if record is not None:
        pairs.extend(pf_store_row_id_kv(record))
    if extra_kv:
        pairs.extend(extra_kv)
    _log.info("%s", pf_kv(pairs, zh="LLM 本次调用消费"))


def llm_token_usage_run_snapshot() -> dict[str, float | int]:
    with _token_usage_lock:
        return dict(_token_usage_run)


def llm_token_usage_kv_pairs() -> list[tuple[str, Any]]:
    from product_feed_kr.llm_token_billing import format_cost_yuan

    snap = llm_token_usage_run_snapshot()
    if not snap.get("requests"):
        return []
    cost = float(snap.get("cost_yuan") or 0)
    return [
        ("token_requests", snap["requests"]),
        ("prompt_tokens", snap.get("prompt_tokens")),
        ("completion_tokens", snap.get("completion_tokens")),
        ("total_tokens", snap.get("total_tokens")),
        ("cost_yuan", format_cost_yuan(cost)),
    ]

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


from product_feed_kr.wecatalog_size_fix import (
    dedupe_str_list as _dedupe_str_list,
    shoe_sizes_to_kr_mm,
)


_SIZE_SPEC_KIND_FOOTWEAR = "footwear"
_SIZE_SPEC_KIND_APPAREL = "apparel"


def normalize_size_spec_kind(raw: Any) -> str | None:
    """LLM ``size_spec_kind`` → ``footwear`` | ``apparel`` | None。"""
    s = str(raw or "").strip().lower()
    if not s:
        return None
    if s in (
        "footwear",
        "shoe",
        "shoes",
        "footwear_shoe",
        "鞋",
        "鞋靴",
        "鞋类",
        "运动鞋",
        "靴子",
    ):
        return _SIZE_SPEC_KIND_FOOTWEAR
    if s in (
        "apparel",
        "clothing",
        "garment",
        "服装",
        "衣服",
        "服饰",
    ):
        return _SIZE_SPEC_KIND_APPAREL
    return None


def listing_llm_wants_shoe_size_mm(payload: dict[str, Any]) -> bool:
    """仅当 LLM 标注 ``size_spec_kind=footwear`` 时转韩版毫米脚长。"""
    return normalize_size_spec_kind(payload.get("size_spec_kind")) == _SIZE_SPEC_KIND_FOOTWEAR


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


_SYSTEM_LISTING_BASE = """你是电商上架信息翻译与纠错助手。只输出一个 JSON 对象，不要 Markdown。
用户消息含：**原始商品描述** + **当前已整理字段**（爬取/规则预处理结果）。

任务：在「当前字段」基础上完成中韩文上架信息——翻译、纠错、补全空缺；高置信度（>=90%），不脑补。

**输出完整 JSON**（字段可省略仅当确实无内容）：
- **不要**输出 ``price_krw``（韩元售价由抓取阶段按实时汇率写入，本任务不处理）
- cny_price：可选，人民币售价备查（优先沿用已整理字段中的 ``price_cny``，勿从标题猜价）
- name_zh：精简中文商品名（8~20 字，备查）
- name_ko：韩文商品名（上架标题用，8~30 字）
- desc_zh：中文描述（2~5 句，可轻润色）
- desc_ko：韩文描述（基于 desc_zh 翻译，不增信息）
- size_spec_kind：有 ``attr_map.尺码`` 时**必填**：``footwear``（鞋靴/运动鞋等，中文尺码用欧码 32–50，系统将把韩文尺码转为毫米脚长）或 ``apparel``（服装等，中文尺码用 S/M/L/XL）
{attr_spec_lines}
**禁止**输出 `attr_map_ko.사이즈`（韩文尺码由系统根据 ``size_spec_kind`` 与中文尺码自动生成）

**中文尺码纠错（只写入 attr_map.尺码）**：结合 ``size_spec_kind`` 判断**服装**还是**鞋靴**。常见错误与纠正：

| 类型 | 常见错误 | 应纠正为（仅 attr_map.尺码） |
|------|----------|------------------------------|
| 服装 | 数字档 ``0``~``4`` 未展开 | ``S`` ``M`` ``L`` ``XL`` ``XXL``（0=S,1=M,2=L,3=XL,4=XXL） |
| 服装 | 连写 ``01234``、``1234`` | 拆成 ``["S","M","L","XL","XXL"]`` 等 |
| 服装 | 区间 ``0-4``、``0~4`` | 展开为多个字母码，勿保留区间字符串 |
| 鞋靴 | 写成 S/M/L 或数字档 ``0`` ``1`` | 改为欧码 ``35`` ``36`` ``40`` ``40.5`` 等（32–50） |
| 鞋靴 | 区间 ``38-41`` | 展开为 ``38,39,40,41`` |
| 通用 | 重复、乱序、与标题不符 | 去重、从小到大；不确定的删除 |

鞋靴中文尺码用**欧码数字**；服装用 **S/M/L/XL…**。不要输出毫米、不要改韩文 사이즈。

{color_policy_block}
对已整理且合理的中文尺码优先保留；{size_fill_rule}"""

_ATTR_SPEC_WITH_COLORS = (
    "- attr_map：仅中文 `颜色`、`尺码`（**只改这里的中文尺码**）\n"
    "- attr_map_ko：仅韩文 `색상`（仅翻译 attr_map 中**从附图确认**的中文颜色）；"
)
_COLOR_POLICY_VISION_ON = """**颜色（仅从附图九宫格识别，完全忽略标题中的颜色）**
- 用户消息附**一张**商品参考图九宫格（最多 9 张原图合成）
- `attr_map.颜色` / `attr_map_ko.색상` **只能**依据附图中**实际可见**的商品本体配色填写
- **完全忽略**标题、描述、商品名及当前 JSON 中的颜色相关文字（材料/里料/五金/文案配色等**均不可**作为颜色依据）
- 图中未见或无法高置信确认的颜色**不要**输出；宁缺毋滥
- 尺码/价格/名称/描述仍按上文处理标题与已整理字段"""
_ATTR_SPEC_TEXT_COLORS = (
    "- attr_map：中文 `颜色`、`尺码`（**只改这里的中文尺码**）\n"
    "- attr_map_ko：仅韩文 `색상`（翻译 attr_map 中已确认的中文颜色）；"
)
_COLOR_POLICY_OFF = """**颜色（文生文：仅从原始描述明确写出的销售色）**
- 无附图；仅依据上方「原始描述（标题正文）」中**明确写出**的商品销售颜色、可选颜色或配色方案，填写 `attr_map.颜色` 与 `attr_map_ko.색상`
- **禁止**从材料、里料、五金、工艺、图案、商品名暗示等推断颜色；**材料/配件颜色 ≠ 商品销售颜色**
- 原始描述未明确写销售色/可选色时：**不要**输出颜色字段；宁缺毋滥
- 尺码/价格/名称/描述仍按上文与已整理字段处理"""

_SIZE_FILL_RULE_WITH_COLORS = (
    "尺码空项可从原文中与尺码相关的描述补全。"
    "颜色**只能**来自附图九宫格可见配色，**禁止**从标题/描述/材料文案填写或补全颜色。"
)
_SIZE_FILL_RULE_TEXT_COLORS = (
    "尺码空项可从原文中与尺码相关的描述补全。"
    "颜色：仅当「原始描述」**明确写出**商品销售色/可选色时可写入；禁止从材料/里料/五金等猜色。"
)


def _system_listing_prompt() -> str:
    if listing_llm_color_vision_enabled():
        lines = _ATTR_SPEC_WITH_COLORS
        size_fill = _SIZE_FILL_RULE_WITH_COLORS
        color_policy_block = _COLOR_POLICY_VISION_ON
    else:
        lines = _ATTR_SPEC_TEXT_COLORS
        size_fill = _SIZE_FILL_RULE_TEXT_COLORS
        color_policy_block = _COLOR_POLICY_OFF
    return _SYSTEM_LISTING_BASE.format(
        attr_spec_lines=lines,
        size_fill_rule=size_fill,
        color_policy_block=color_policy_block,
    )


def strip_listing_llm_colors(payload: dict[str, Any], *, allow_colors: bool = False) -> None:
    """从 ``listing_llm`` 移除中韩文颜色键。

    ``allow_colors=True`` 时保留。
    关闭颜色识别（文生文）时始终保留 LLM 从原文明确提取的颜色；
    仅「开启识别但未生成九宫格附图」时剔除颜色。
    """
    if allow_colors:
        return
    if not listing_llm_color_vision_enabled():
        return
    am = payload.get("attr_map")
    if isinstance(am, dict):
        am = dict(am)
        am.pop("颜色", None)
        if am:
            payload["attr_map"] = am
        else:
            payload.pop("attr_map", None)
    ko = payload.get("attr_map_ko")
    if isinstance(ko, dict):
        ko = dict(ko)
        ko.pop("색상", None)
        if ko:
            payload["attr_map_ko"] = ko
        else:
            payload.pop("attr_map_ko", None)


_USER_LISTING_TMPL = """## 原始描述（标题正文）

---
{title}
---

## 当前已整理字段（请在此基础上翻译与纠错，输出完整 JSON）

```json
{draft_json}
```
"""


def _listing_llm_draft_snapshot(
    record: dict[str, Any],
    listing_llm: dict[str, Any],
) -> dict[str, Any]:
    """供 LLM 参考的当前字段快照（爬取 + 已有 listing_llm）。"""
    draft: dict[str, Any] = {}
    cp = listing_llm.get("cny_price")
    if not _cny_price_field_usable(cp):
        from product_feed_kr.wecatalog_store_record import record_scrape_price_raw

        cp = record_scrape_price_raw(record)
    if _cny_price_field_usable(cp):
        draft["cny_price"] = str(cp).strip()

    for key in ("name_zh", "name_ko", "desc_zh", "desc_ko"):
        v = str(listing_llm.get(key) or "").strip()
        if v:
            draft[key] = v

    am = listing_llm.get("attr_map")
    if isinstance(am, dict) and am:
        am_draft = dict(am)
        am_draft.pop("颜色", None)
        if am_draft:
            draft["attr_map"] = am_draft
    amk = listing_llm.get("attr_map_ko")
    if isinstance(amk, dict) and amk:
        ko_draft = dict(amk)
        ko_draft.pop("사이즈", None)
        ko_draft.pop("색상", None)
        if ko_draft:
            draft["attr_map_ko"] = ko_draft

    g = str(record.get("wecatalog_group") or "").strip()
    t = str(record.get("wecatalog_tag") or "").strip()
    if g or t:
        draft["category_hint"] = {"group": g, "tag": t}
    if listing_llm_color_vision_enabled():
        draft["_note"] = (
            "只需纠错 attr_map 中文尺码；有尺码时必须输出 size_spec_kind（footwear 或 apparel）；"
            "颜色/색상仅能从附图九宫格识别，勿从标题取色；勿输出 attr_map_ko.사이즈"
        )
    else:
        draft["_note"] = (
            "纠错 attr_map 中文尺码；有尺码时必须输出 size_spec_kind（footwear=鞋靴欧码，apparel=服装字母码）；"
            "颜色/색상仅从「原始描述」中明确写出的销售色提取；勿输出 attr_map_ko.사이즈"
        )
    return draft


def _apply_scrape_fields_to_listing_llm(
    record: dict[str, Any],
    payload: dict[str, Any],
    *,
    listing_hint: str | None = None,
) -> bool:
    """把抓取入库的价/尺码/颜色写入 ``listing_llm``（仅中文 attr_map，不写 attr_map_ko）。"""
    from product_feed_kr.wecatalog_store_record import (
        record_scrape_price_raw,
        record_scrape_sizes,
    )

    sizes = record_scrape_sizes(record)
    changed = False
    am = dict(payload.get("attr_map") or {})
    if sizes:
        am["尺码"] = list(sizes)
        changed = True
    # 开启颜色识别时颜色仅由附图九宫格 LLM 写入，不预填爬取/标题色
    if changed:
        payload["attr_map"] = am
        payload["formats_source"] = "scrape"
        _dedupe_attr_map_size_lists(payload)
    pop_price = record_scrape_price_raw(record)
    if pop_price and not _cny_price_field_usable(payload.get("cny_price")):
        payload["cny_price"] = pop_price
        changed = True
    return changed


def apply_listing_size_fix_from_zh(
    payload: dict[str, Any],
    *,
    listing_hint: str | None = None,
    record: dict[str, Any] | None = None,
) -> None:
    """修正中文 ``attr_map.尺码``；``size_spec_kind=footwear`` 时生成毫米 ``attr_map_ko.사이즈``。"""
    _ = listing_hint, record
    from product_feed_kr.wecatalog_size_fix import fix_scrape_sizes

    am = dict(payload.get("attr_map") or {}) if isinstance(payload.get("attr_map"), dict) else {}
    ko = dict(payload.get("attr_map_ko") or {}) if isinstance(payload.get("attr_map_ko"), dict) else {}
    zh_raw = am.get("尺码")
    if isinstance(zh_raw, list) and zh_raw:
        zh_fixed = fix_scrape_sizes(
            [str(x).strip() for x in zh_raw if str(x).strip()],
        )
        am["尺码"] = zh_fixed
        payload["attr_map"] = am
        if listing_llm_wants_shoe_size_mm(payload):
            ko["사이즈"] = _dedupe_str_list(shoe_sizes_to_kr_mm(zh_fixed))
        else:
            ko["사이즈"] = list(zh_fixed)
    else:
        ko.pop("사이즈", None)
    payload["attr_map_ko"] = ko
    _dedupe_attr_map_size_lists(payload)


def hydrate_listing_llm_from_scrape(
    record: dict[str, Any],
    payload: dict[str, Any],
    *,
    listing_hint: str | None = None,
) -> None:
    """调 LLM 前：爬取价/规格写入 attr_map，并按中文尺码生成韩文 사이즈（供 draft 参考）。"""
    _apply_scrape_fields_to_listing_llm(record, payload, listing_hint=listing_hint)
    apply_listing_size_fix_from_zh(payload, listing_hint=listing_hint, record=record)
    strip_listing_llm_colors(payload)


def finalize_listing_llm_specs(
    record: dict[str, Any],
    payload: dict[str, Any],
    *,
    listing_hint: str | None = None,
) -> None:
    """兼容旧名；LLM 后请用 ``apply_listing_size_fix_from_zh``。"""
    _ = record
    apply_listing_size_fix_from_zh(payload, listing_hint=listing_hint)


_LISTING_LLM_DIFF_SCALAR_KEYS = (
    "cny_price",
    "name_zh",
    "name_ko",
    "desc_zh",
    "desc_ko",
    "size_spec_kind",
)
_LISTING_LLM_DIFF_ATTR_ZH = ("尺码", "颜色")
_LISTING_LLM_DIFF_ATTR_KO = ("색상", "사이즈")


def _diff_val_repr(v: Any, *, max_len: int = 72) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        s = json.dumps(v, ensure_ascii=False)
    elif isinstance(v, dict):
        s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    else:
        s = str(v).strip()
    return pf_trunc(s, max_len) if s else ""


def _diff_arrow(old_s: str, new_s: str, *, max_each: int = 72) -> str:
    if old_s == new_s:
        return ""
    if not old_s:
        return f"∅→{new_s}"
    if not new_s:
        return f"{old_s}→∅"
    return f"{old_s}→{new_s}"


def listing_llm_field_changes(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    val_max_each: int = 72,
) -> str:
    """对比 LLM 前后 ``listing_llm`` 主要字段，供日志 ``changes=`` 使用。"""
    b = before if isinstance(before, dict) else {}
    a = after if isinstance(after, dict) else {}
    parts: list[str] = []

    for k in _LISTING_LLM_DIFF_SCALAR_KEYS:
        ob = _diff_val_repr(b.get(k), max_len=val_max_each)
        oa = _diff_val_repr(a.get(k), max_len=val_max_each)
        seg = _diff_arrow(ob, oa, max_each=val_max_each)
        if seg:
            parts.append(f"{k}:{seg}")

    b_am = b.get("attr_map") if isinstance(b.get("attr_map"), dict) else {}
    a_am = a.get("attr_map") if isinstance(a.get("attr_map"), dict) else {}
    for k in _LISTING_LLM_DIFF_ATTR_ZH:
        ob = _diff_val_repr(b_am.get(k), max_len=val_max_each)
        oa = _diff_val_repr(a_am.get(k), max_len=val_max_each)
        seg = _diff_arrow(ob, oa, max_each=val_max_each)
        if seg:
            parts.append(f"attr_map.{k}:{seg}")

    b_ko = b.get("attr_map_ko") if isinstance(b.get("attr_map_ko"), dict) else {}
    a_ko = a.get("attr_map_ko") if isinstance(a.get("attr_map_ko"), dict) else {}
    for k in _LISTING_LLM_DIFF_ATTR_KO:
        ob = _diff_val_repr(b_ko.get(k), max_len=val_max_each)
        oa = _diff_val_repr(a_ko.get(k), max_len=val_max_each)
        seg = _diff_arrow(ob, oa, max_each=val_max_each)
        if seg:
            parts.append(f"attr_map_ko.{k}:{seg}")

    return "; ".join(parts) if parts else "(无变化)"


def _merge_listing_llm_payload(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    patch = dict(patch)
    out = dict(base)
    for k, v in patch.items():
        if k == "attr_map_ko" and isinstance(v, dict):
            patch_ko = dict(v)
            patch_ko.pop("사이즈", None)
            merged = dict(out.get("attr_map_ko") or {})
            merged.update(patch_ko)
            out["attr_map_ko"] = merged
        elif k == "attr_map" and isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        elif k == "attr_map" and isinstance(v, dict):
            out[k] = dict(v)
        else:
            out[k] = v
    return out


def enrich_listing(
    record: dict[str, Any],
    commodity: dict[str, Any],
    *,
    timeout: float | None = None,
    api_profile: ListingLlmApiProfile | None = None,
    listing_llm_base: dict[str, Any] | None = None,
) -> bool:
    """单次 LLM：原始标题 + 当前已整理字段 → 翻译与纠错。"""
    title = str(commodity.get("title") or "").strip()
    if not title:
        return False
    ll = dict(listing_llm_base) if isinstance(listing_llm_base, dict) else {}
    hydrate_listing_llm_from_scrape(record, ll, listing_hint=title)
    ll_before = copy.deepcopy(ll)

    draft = _listing_llm_draft_snapshot(record, ll)
    draft_json = json.dumps(draft, ensure_ascii=False, indent=2) if draft else "{}"
    user_msg = _USER_LISTING_TMPL.format(title=title, draft_json=draft_json)

    client, model, host = _openai_client(timeout, api_profile=api_profile)
    use_response_format = "dashscope.aliyuncs.com" not in host.lower()

    want_vision = listing_llm_color_vision_enabled()
    grid_url: str | None = None
    if want_vision:
        from product_feed_kr.wego_commodity import commodity_image_urls

        urls = commodity_image_urls(commodity)
        grid_url = _build_color_vision_grid_data_url(
            urls,
            max_images=listing_llm_color_vision_max_images(),
            max_grid_px=listing_llm_color_vision_max_px(),
        )
        if not grid_url:
            _log.warning(
                "%s",
                pf_kv(
                    [
                        ("event", "llm.vision_no_images"),
                        *pf_store_row_id_kv(record),
                        ("url_candidates", len(urls)),
                    ],
                    zh="颜色识别已开启但未生成九宫格附图，本条不输出颜色字段",
                ),
            )

    log_kv: list[tuple[str, Any]] = [
        ("event", "llm.request"),
        *pf_store_row_id_kv(record),
        ("model", model),
    ]
    if want_vision and grid_url:
        user_text = (
            user_msg.rstrip()
            + "\n\n（附图：商品参考图九宫格。**颜色仅能从该图识别**，勿从标题/描述取色。）"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_listing_prompt()},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": grid_url}},
                ],
            },
        ]
        log_kv.extend([("vision", 1), ("vision_grid", 1)])
        log_zh = "LLM 翻译与纠错（含颜色九宫格附图）"
    else:
        messages = [
            {"role": "system", "content": _system_listing_prompt()},
            {"role": "user", "content": user_msg},
        ]
        if want_vision:
            log_kv.append(("vision", 0))
        log_zh = "LLM 翻译与纠错"
    _log.info("%s", pf_kv(log_kv, zh=log_zh))

    content, elapsed_ms, usage = _chat_once_json(
        client,
        model=model,
        messages=messages,
        use_response_format=use_response_format,
        usage_log_record=record,
        call_kind="listing",
    )
    patch = parse_listing_llm_response(content, listing_hint=title)
    patch.pop("price_krw", None)
    merged = _merge_listing_llm_payload(ll, patch)
    merged.pop("price_krw", None)
    apply_listing_size_fix_from_zh(merged, listing_hint=title, record=record)
    strip_listing_llm_colors(
        merged,
        allow_colors=bool(want_vision and grid_url),
    )
    sync_listing_prices_to_record(record, merged)
    merged["source"] = "openai"
    merged["model"] = model
    record["listing_llm"] = merged
    changes = listing_llm_field_changes(ll_before, merged)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.response"),
                *pf_store_row_id_kv(record),
                ("elapsed_ms", elapsed_ms),
                ("size_spec_kind", merged.get("size_spec_kind")),
                ("shoe_size_mm", 1 if listing_llm_wants_shoe_size_mm(merged) else 0),
                ("cny_price", merged.get("cny_price")),
                ("name_zh_len", len(merged.get("name_zh") or "")),
                ("desc_ko_len", len(merged.get("desc_ko") or "")),
                ("changes", changes),
            ],
            zh="LLM 翻译与纠错完成",
            val_max=1200,
        ),
    )
    return True


def enrich_listing_upload_gaps(
    record: dict[str, Any],
    commodity: dict[str, Any],
    **kwargs: Any,
) -> bool:
    """兼容旧名；同 ``enrich_listing``。"""
    return enrich_listing(record, commodity, **kwargs)


def enrich_listing_scrape_gaps(
    record: dict[str, Any],
    commodity: dict[str, Any],
    **kwargs: Any,
) -> bool:
    """兼容旧名；同 ``enrich_listing``。"""
    return enrich_listing(record, commodity, **kwargs)


def enrich_listing_copy(
    record: dict[str, Any],
    commodity: dict[str, Any],
    **kwargs: Any,
) -> bool:
    """兼容旧名；同 ``enrich_listing``。"""
    return enrich_listing(record, commodity, **kwargs)


def _strip_json_fence(s: str) -> str:
    t = s.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _norm_text_field(v: Any, max_len: int) -> str:
    t = str(v).strip() if v is not None else ""
    t = re.sub(r"[\u200b\ufeff]", "", t)
    t = re.sub(r"[#★☆🔥✨💥✅✔️]", " ", t)
    t = " ".join(t.split())
    if len(t) > max_len:
        t = t[:max_len].rstrip(" ,-/|")
    return t


def _normalize_copy_payload(data: dict[str, Any], *, listing_hint: str | None = None) -> dict[str, Any]:
    """文案阶段：仅名称与描述。"""
    _ = listing_hint
    out: dict[str, Any] = {}
    for key, max_len in (
        ("name_zh", 24),
        ("name_ko", 30),
        ("desc_zh", 520),
        ("desc_ko", 620),
    ):
        t = _norm_text_field(data.get(key), max_len)
        if t:
            out[key] = t
    cp = data.get("cny_price")
    if _cny_price_field_usable(cp):
        out["cny_price"] = str(cp).strip()
    return out


def parse_listing_copy_response(text: str, *, listing_hint: str | None = None) -> dict[str, Any]:
    """兼容旧名；同 ``parse_listing_llm_response``。"""
    return parse_listing_llm_response(text, listing_hint=listing_hint)


def _normalize_llm_payload(data: dict[str, Any], *, listing_hint: str | None = None) -> dict[str, Any]:
    allow_color_fields = True
    out: dict[str, Any] = {}
    hint_bits: list[str] = []
    if listing_hint:
        hint_bits.append(str(listing_hint).strip())
    if isinstance(data, dict):
        for k in ("name_zh", "name_ko", "desc_zh"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                hint_bits.append(v.strip()[:500])
    cp = data.get("cny_price")
    if cp is None:
        out["cny_price"] = None
    else:
        s = str(cp).strip()
        if not s or s.lower() == "null" or s == "-1":
            out["cny_price"] = None
        else:
            out["cny_price"] = s

    def _norm_text(v: Any, max_len: int) -> str:
        return _norm_text_field(v, max_len)

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

        def _size_tokens(text: str) -> list[str]:
            out_toks: list[str] = []
            for t in re.findall(
                r"(?i)EU\s*[0-9]{2}(?:\.[05])?|[0-9]{2}(?:\.[05])?|[A-Za-z0-9]+",
                text,
            ):
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
                if not allow_color_fields:
                    continue
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
    amk_raw = data.get("attr_map_ko")
    if allow_color_fields and isinstance(amk_raw, dict):
        colors_only: dict[str, Any] = {}
        if "색상" in amk_raw:
            colors_only["색상"] = amk_raw["색상"]
        if colors_only:
            out["attr_map_ko"] = _normalize_attr_map(
                colors_only, ko=True, key_max=30, val_max=48
            )

    _dedupe_attr_map_size_lists(out)
    strip_listing_llm_colors(out, allow_colors=True)

    name_zh = data.get("name_zh")
    out["name_zh"] = _norm_text(name_zh, 24)

    name_ko = data.get("name_ko")
    out["name_ko"] = _norm_text(name_ko, 30)

    desc_zh = data.get("desc_zh")
    out["desc_zh"] = _norm_text(desc_zh, 520)

    desc_ko = data.get("desc_ko")
    out["desc_ko"] = _norm_text(desc_ko, 620)

    kind = normalize_size_spec_kind(data.get("size_spec_kind"))
    if kind:
        out["size_spec_kind"] = kind

    return out


def apply_listing_llm_price_to_commodity(
    commodity: dict[str, Any],
    listing_llm: dict[str, Any],
    *,
    record: dict[str, Any] | None = None,
) -> None:
    """同步 ``price_cny`` 到 record/commodity；韩元价仅来自抓取，不由 LLM 写入。"""
    if isinstance(record, dict) and isinstance(listing_llm, dict):
        cp = listing_llm.get("cny_price")
        if _cny_price_field_usable(cp):
            record["price_cny"] = str(cp).strip()
    cp = listing_llm.get("cny_price") if isinstance(listing_llm, dict) else None
    if cp is None and isinstance(record, dict):
        cp = record.get("price_cny")
    if not _cny_price_field_usable(cp):
        commodity["optimaPrice"] = ""
        return
    s = str(cp).strip()
    commodity["optimaPrice"] = s
    if isinstance(record, dict):
        record["price_cny"] = s


def listing_llm_name_zh_usable(listing_llm: dict[str, Any] | None) -> bool:
    """``name_zh`` 非空（备查；上架标题用 ``name_ko``）。"""
    if not isinstance(listing_llm, dict):
        return False
    return bool(str(listing_llm.get("name_zh") or "").strip())


def listing_llm_name_ko_usable(listing_llm: dict[str, Any] | None) -> bool:
    """``name_ko`` 非空（``can_upload`` 韩文商品名）。"""
    if not isinstance(listing_llm, dict):
        return False
    return bool(str(listing_llm.get("name_ko") or "").strip())


def listing_llm_content_meets_upload_requirements(rec: dict[str, Any]) -> bool:
    """韩文名/描述非空（不含价格、白名单）。"""
    ll = rec.get("listing_llm")
    if not isinstance(ll, dict):
        return False
    if not listing_llm_name_ko_usable(ll):
        return False
    return bool(str(ll.get("desc_ko") or "").strip())


def listing_llm_meets_upload_requirements(rec: dict[str, Any]) -> bool:
    """``can_upload``：韩文名/描述非空且有有效韩元价（不含白名单）。"""
    ll = rec.get("listing_llm")
    if not listing_llm_content_meets_upload_requirements(rec):
        return False
    return listing_llm_price_krw_usable(rec, ll if isinstance(ll, dict) else {})


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


def _krw_price_field_usable(kp: Any) -> bool:
    if kp is None:
        return False
    s = str(kp).strip().replace(",", "")
    if not s or s.lower() == "null" or s == "-1":
        return False
    try:
        return float(s) > 0
    except ValueError:
        return False


def _listing_krw_per_cny() -> float | None:
    """1 CNY 兑 KRW；失败返回 None（不抛错，供 can_upload 换算）。"""
    from product_feed_kr.cny_krw_rate import fetch_krw_per_cny

    manual = _cfg_get("SEVEN17_CNY_KRW_RATE")
    if manual and str(manual).strip():
        try:
            v = float(str(manual).strip().replace(",", ""))
            return v if v > 0 else None
        except ValueError:
            return None
    try:
        return fetch_krw_per_cny()
    except Exception:
        fb = _cfg_get("SEVEN17_CNY_KRW_FALLBACK")
        if not fb or not str(fb).strip():
            return None
        try:
            v = float(str(fb).strip().replace(",", ""))
            return v if v > 0 else None
        except ValueError:
            return None


def cny_amount_to_price_krw(cny: str) -> str | None:
    """人民币售价 → 韩元整数（与上架填表同一套千韩元取整）。"""
    rate = _listing_krw_per_cny()
    if rate is None or not _cny_price_field_usable(cny):
        return None
    from product_feed_kr.cny_krw_rate import cny_listing_amount_to_krw_won_str

    krw = cny_listing_amount_to_krw_won_str(str(cny).strip(), rate)
    return krw if _krw_price_field_usable(krw) else None


def effective_price_krw(
    rec: dict[str, Any],
    listing_llm: dict[str, Any] | None,
) -> str | None:
    """有效韩元售价：以库内 ``price_krw``（抓取时汇率换算）为准；缺省时可用人民币兜底换算。"""
    _ = listing_llm
    if _krw_price_field_usable(rec.get("price_krw")):
        return str(rec.get("price_krw")).strip().replace(",", "")
    for cny in (rec.get("price_cny"),):
        if _cny_price_field_usable(cny):
            krw = cny_amount_to_price_krw(str(cny).strip())
            if krw:
                return krw
    from product_feed_kr.wecatalog_store_record import commodity_from_wecatalog_record
    from product_feed_kr.wego_commodity import parse_price_str, parse_wego_product

    com = commodity_from_wecatalog_record(rec)
    if not isinstance(com, dict):
        return None
    try:
        prod = parse_wego_product(com, default_price_if_missing="")
        p = str(prod.get("price") or "").strip()
        if p and p not in ("0", "0.0"):
            return cny_amount_to_price_krw(p)
    except ValueError:
        pass
    raw = parse_price_str(rec.get("price_cny"), "")
    if raw and raw not in ("0", "0.0"):
        return cny_amount_to_price_krw(raw)
    return None


def sync_listing_prices_to_record(
    record: dict[str, Any] | None,
    listing_llm: dict[str, Any],
) -> None:
    """把 ``listing_llm`` 的 ``cny_price`` 同步到 record（不写韩元）。"""
    if not isinstance(record, dict) or not isinstance(listing_llm, dict):
        return
    cp = listing_llm.get("cny_price")
    if _cny_price_field_usable(cp):
        record["price_cny"] = str(cp).strip()


def listing_llm_price_krw_usable(
    rec: dict[str, Any],
    listing_llm: dict[str, Any] | None,
) -> bool:
    """有有效韩元售价（含由人民币换算）。"""
    return bool(effective_price_krw(rec, listing_llm or {}))


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


def record_is_no_price_allowed_by_map_category(rec: dict[str, Any]) -> bool:
    """无价格上传白名单（读取当前配置，上传前可变更）。"""
    return _record_is_no_price_allowed_by_map_category(rec)


def record_llm_uploadable_at_upload(rec: dict[str, Any]) -> bool:
    """上传阶段：内容齐全且（``can_upload`` 有价 或 白名单无价放行）。"""
    if not listing_llm_content_meets_upload_requirements(rec):
        return False
    if bool(rec.get("can_upload")):
        return True
    return record_is_no_price_allowed_by_map_category(rec)


def listing_llm_cny_usable(listing_llm: dict[str, Any]) -> bool:
    """兼容旧名；请用 ``listing_llm_price_krw_usable``。"""
    if not isinstance(listing_llm, dict):
        return False
    return _krw_price_field_usable(listing_llm.get("price_krw")) or _cny_price_field_usable(
        listing_llm.get("cny_price"),
    )


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
        return not listing_llm_content_meets_upload_requirements(rec)
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
                    ("krw_ok", 1 if listing_llm_price_krw_usable(record, record.get("listing_llm")) else 0),
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
    """从 ``OPENAI_PROFILES`` 解析；每项可 ``api_keys: [..]`` 共享同一 base_url/model。``threads`` 控制每个 api_key 的并发线程数（0=禁用，1~3）。"""
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
    """是否启用颜色识别（``OPENAI_LISTING_COLOR_VISION``）。

    - **false**（默认）：文生文；颜色仅从「原始描述」**明确写出的销售色**提取，禁止材料色猜色；不写附图色。
    - **true**：附图九宫格识别颜色，**完全忽略**标题中的颜色词；探测含图生文。
    """
    return _cfg_bool("OPENAI_LISTING_COLOR_VISION", False)


def listing_llm_color_vision_max_images() -> int:
    """九宫格最多使用的原图张数（1–9）。"""
    try:
        n = int(str(_cfg_get("OPENAI_LISTING_COLOR_VISION_MAX_IMAGES") or "9").strip())
    except ValueError:
        n = 9
    return max(1, min(n, 9))


def listing_llm_color_vision_max_px() -> int:
    """合成九宫格 JPEG 的长边像素上限（``OPENAI_LISTING_COLOR_VISION_MAX_PX``）。"""
    try:
        n = int(str(_cfg_get("OPENAI_LISTING_COLOR_VISION_MAX_PX") or "512").strip())
    except ValueError:
        n = 512
    return max(128, min(n, 2048))


_VISION_GRID_COLS = 3
_VISION_GRID_ROWS = 3
_VISION_GRID_GAP = 4
_VISION_GRID_BG = (240, 240, 240)


def _fetch_rgb_image_from_url(url: str) -> Any | None:
    """下载单张商品图为 RGB PIL Image；失败返回 None。"""
    from PIL import Image

    u = str(url).strip()
    if not u.startswith(("http://", "https://")):
        return None
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
            return None
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except (OSError, ValueError, TypeError, urllib.error.URLError, urllib.error.HTTPError) as e:
        _log.debug("listing_llm vision image skip: %s", str(e)[:200])
        return None


def _paste_thumbnail_centered(canvas: Any, thumb: Any, x0: int, y0: int, cell: int) -> None:
    from PIL import Image

    tw, th = thumb.size
    if tw <= 0 or th <= 0:
        return
    scale = min(cell / tw, cell / th, 1.0)
    nw = max(1, int(tw * scale))
    nh = max(1, int(th * scale))
    if (nw, nh) != (tw, th):
        thumb = thumb.resize((nw, nh), Image.Resampling.LANCZOS)
    px = x0 + (cell - nw) // 2
    py = y0 + (cell - nh) // 2
    canvas.paste(thumb, (px, py))


def _build_color_vision_grid_data_url(
    urls: list[str],
    *,
    max_images: int,
    max_grid_px: int,
) -> str | None:
    """将最多 9 张商品图合成 3×3 九宫格，输出单张 JPEG data URL 供多模态 API。"""
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("颜色识别需 Pillow：pip install Pillow") from e

    slots = max(1, min(int(max_images), _VISION_GRID_COLS * _VISION_GRID_ROWS))
    gap = _VISION_GRID_GAP
    cell = max(32, (int(max_grid_px) - gap * (_VISION_GRID_COLS + 1)) // _VISION_GRID_COLS)
    canvas_w = _VISION_GRID_COLS * cell + gap * (_VISION_GRID_COLS + 1)
    canvas_h = _VISION_GRID_ROWS * cell + gap * (_VISION_GRID_ROWS + 1)
    canvas = Image.new("RGB", (canvas_w, canvas_h), _VISION_GRID_BG)

    loaded = 0
    for url in urls:
        if loaded >= slots:
            break
        im = _fetch_rgb_image_from_url(url)
        if im is None:
            continue
        idx = loaded
        loaded += 1
        row, col = divmod(idx, _VISION_GRID_COLS)
        x0 = gap + col * (cell + gap)
        y0 = gap + row * (cell + gap)
        thumb = im.copy()
        thumb.thumbnail((cell, cell), Image.Resampling.LANCZOS)
        _paste_thumbnail_centered(canvas, thumb, x0, y0, cell)

    if loaded == 0:
        return None

    canvas.thumbnail((int(max_grid_px), int(max_grid_px)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=85, optimize=True)
    b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _download_resize_jpeg_data_urls(urls: list[str], *, max_images: int, max_px: int) -> list[str]:
    """兼容旧接口：返回单元素列表（九宫格合成图）。"""
    one = _build_color_vision_grid_data_url(
        urls,
        max_images=max_images,
        max_grid_px=max_px,
    )
    return [one] if one else []


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
        content, elapsed, _usage = _chat_once_json(
            client,
            model=model,
            messages=[
                {"role": "system", "content": "只输出 JSON：{\"status\":\"ok\"}"},
                {"role": "user", "content": "ping"},
            ],
            use_response_format=False,
            track_usage=False,
            call_kind="probe_text",
            extra_kv=[("label", label), ("host", host)],
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
                _log_llm_call_usage(
                    _chat_usage_from_response(resp),
                    model=model,
                    elapsed_ms=elapsed_ms,
                    call_kind="probe_vision",
                    track_usage=False,
                    extra_kv=[("label", label), ("host", host)],
                )
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
    probe_mode_zh = "文生文+图生文" if test_vision else "仅文生文（不含颜色）"
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


def _chat_usage_from_response(resp: Any) -> dict[str, int]:
    """OpenAI 兼容 ``usage`` → ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``。"""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {}
    out: dict[str, int] = {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = getattr(usage, k, None)
        if v is None and isinstance(usage, dict):
            v = usage.get(k)
        if v is not None:
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                pass
    if "total_tokens" not in out:
        pt = out.get("prompt_tokens", 0)
        ct = out.get("completion_tokens", 0)
        if pt or ct:
            out["total_tokens"] = pt + ct
    return out


def _chat_once_json(
    client,
    *,
    model: str,
    messages: list[dict[str, Any]],
    use_response_format: bool = True,
    track_usage: bool = True,
    usage_log_record: dict[str, Any] | None = None,
    call_kind: str = "listing",
    extra_kv: list[tuple[str, Any]] | None = None,
) -> tuple[str, int, dict[str, int]]:
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
    usage = _chat_usage_from_response(resp)
    if track_usage:
        note_llm_token_usage(usage)
    _log_llm_call_usage(
        usage,
        model=model,
        elapsed_ms=elapsed_ms,
        call_kind=call_kind,
        record=usage_log_record,
        track_usage=track_usage,
        extra_kv=extra_kv,
    )
    return content, elapsed_ms, usage


def enrich_record_listing_llm(
    record: dict[str, Any],
    commodity: dict[str, Any],
    *,
    timeout: float | None = None,
    api_profile: ListingLlmApiProfile | None = None,
    register_attempt: bool = True,
) -> bool:
    """
    将 LLM 结果写入 ``record['listing_llm']``（不含韩元；``price_krw`` 由 01 采集写库）。
    若已有 ``listing_llm`` 且未设置 ``OPENAI_LISTING_LLM_FORCE``，则不调 API，但仍会同步人民币价到 ``optimaPrice``。
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
    hydrate_listing_llm_from_scrape(record, ll_base, listing_hint=title)

    if isinstance(existing, dict) and record.get("llm_processed_at") and not listing_llm_force_refresh():
        record["listing_llm"] = ll_base
        apply_listing_llm_price_to_commodity(commodity, ll_base, record=record)
        update_can_upload_flag(record)
        if listing_llm_content_meets_upload_requirements(record):
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
    if not enrich_listing(
        record,
        commodity,
        timeout=timeout,
        api_profile=api_profile,
        listing_llm_base=ll_base,
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
    changes = listing_llm_field_changes(ll_base if isinstance(ll_base, dict) else {}, ll_final)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.done"),
                *pf_store_row_id_kv(record),
                ("cny_price", ll_final.get("cny_price")),
                ("name_zh_len", len(ll_final.get("name_zh") or "")),
                ("desc_ko_len", len(ll_final.get("desc_ko") or "")),
                ("changes", changes),
            ],
            zh="LLM 翻译与纠错处理完成",
            val_max=1200,
        ),
    )
    return True

