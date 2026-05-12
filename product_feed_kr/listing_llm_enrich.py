"""上架前：根据货源 ``commodity.title`` 调用 OpenAI 兼容接口，抽取结构化字段写入 ``record['listing_llm']``，
并把识别到的人民币价写入 ``commodity.optimaPrice``；未识别时 ``listing_llm.cny_price`` 为 JSON ``null``（Python ``None``），
``optimaPrice`` 置空字符串，上架脚本按 ``null`` 跳过。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from product_feed_kr.seven17_config import bool_env as _cfg_bool
from product_feed_kr.seven17_config import getenv as _cfg_get
from product_feed_kr.pf_log import pf_kv

_log = logging.getLogger(__name__)

_SYSTEM = """你是电商商品信息抽取助手。只输出一个 JSON 对象，不要 Markdown 围栏，不要多余说明。
总原则（必须遵守）：
- 只允许基于输入标题文本做“高置信度提取”，不要脑补、不要扩写背景信息。
- 置信度门槛：只有你对某条信息把握 >=90% 才可输出；低于 90% 则留空（"" / {} / null）。
- 描述只做“轻润色”：允许去 emoji、去重复口号、调顺语序；不允许新增原文没有的卖点。

字段要求：
- cny_price：字符串或 null。能确定人民币售价时填数字字符串（如 "340"）；**完全无法判断时请填 null**（不要猜价）。
- attr_map：对象（仅保留“下单必选项”），例如 {"尺码":["X","XL"],"颜色":["黑","白"]}。
  - 只允许：颜色、尺码（及其同义表达）。
  - 尺码值只保留数字和字母（如 "012码" -> "012", "XL码" -> "XL"）。
  - 品牌/风格/款式/图案/赠品等“非下单必选”信息不要放入 attr_map。
  - 无可抽取项时填 {}。
- attr_map_ko：对象（key 为韩文属性名，value 为韩文字符串数组），与 attr_map 尽量一一对应；
  - 例如 {"사이즈":["X","XL"],"색상":["화이트"]}，同样仅保留下单必选项。
  - 无可抽取项时填 {}。
- name_zh：字符串，必须精简为核心中文商品名（尽量 8~20 字），去掉营销词、emoji、口号、重复品牌/型号堆砌。
- name_ko：字符串，韩文精简商品名（尽量 8~24 字），同样去掉营销冗余；不确定可留空字符串 ""。
- desc_zh：字符串，来自标题原文的中文描述提取与轻润色（建议 2~5 句，约 80~220 字）。
  - 尽量保留原文里的核心信息：款式/设计/搭配/赠品/颜色/尺码等。
  - 不确定或原文缺失的信息不要补写。
- desc_ko：字符串，基于 desc_zh 的韩文等价表达（建议 2~5 句，约 90~260 字），仅翻译与轻润色，不新增信息。
"""

_USER_TMPL = """请根据以下相册/微商商品标题抽取信息：

---
{title}
---
"""

_SYSTEM_BATCH = """你是电商商品信息抽取助手。输入是一个 JSON 数组，每项含 idx、goods_id、title。
请输出一个 JSON 对象，格式固定为：
{"items":[{"idx":1,"cny_price":"340","attr_map":{"尺码":["M","L"]},"attr_map_ko":{"사이즈":["M","L"]},"name_zh":"...","name_ko":"...","desc_zh":"...","desc_ko":"..."}]}

要求：
- 只允许基于输入 title 做提取；不要脑补。
- 置信度 <90% 的字段不要填（按类型返回空值）。
- items 必须是数组，且每个 idx 必须对应输入的 idx。
- cny_price：字符串或 null。能确定人民币售价时填数字字符串；完全无法判断填 null，不要猜价。
- attr_map：对象，key 为属性名、value 为字符串数组；仅保留下单必选项（颜色、尺码）；没有填 {}。
- attr_map_ko：对象，key 为韩文属性名、value 为韩文字符串数组；仅保留下单必选项并与 attr_map 对齐；没有填 {}。
- 尺码值必须只含数字/字母（不要“码/码数”等文字后缀）。
- name_zh：核心中文商品名，尽量 8~20 字，去营销冗余。
- name_ko：核心韩文商品名，尽量 8~24 字，去营销冗余；不确定可空字符串。
- desc_zh：从 title 原文提取并轻润色，2~5 句，约 80~220 字，不可新增原文没有的卖点。
- desc_ko：与 desc_zh 信息严格对齐的韩文表达，2~5 句，约 90~260 字。
- 只输出 JSON，不要 Markdown、不要额外解释。
"""


def _strip_json_fence(s: str) -> str:
    t = s.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _normalize_llm_payload(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
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

    def _normalize_attr_map(raw: Any, *, key_max: int = 24, val_max: int = 40) -> dict[str, list[str]]:
        out_map: dict[str, list[str]] = {}
        if not isinstance(raw, dict):
            return out_map
        # 仅保留下单必选项：颜色、尺码（及同义词）。
        alias = {
            "颜色": "颜色",
            "色": "颜色",
            "配色": "颜色",
            "色系": "颜色",
            "색상": "색상",
            "컬러": "색상",
            "칼라": "색상",
            "尺码": "尺码",
            "码数": "尺码",
            "码": "尺码",
            "尺寸": "尺码",
            "size": "尺码",
            "사이즈": "사이즈",
            "치수": "사이즈",
        }

        def _size_tokens(text: str) -> list[str]:
            # 尺码只保留字母/数字片段，例如 "012码" -> ["012"], "XL码" -> ["XL"]。
            toks = re.findall(r"[A-Za-z0-9]+", text)
            out_toks: list[str] = []
            for t in toks:
                v = t.upper()
                if v:
                    out_toks.append(v)
            return out_toks

        for k, vals in raw.items():
            name = _norm_text(k, key_max)
            if not name:
                continue
            key_norm = alias.get(name.lower(), alias.get(name, ""))
            if not key_norm:
                continue
            arr = _as_text_list(vals)
            if not arr:
                continue
            uniq: list[str] = []
            seen: set[str] = set()
            for v in arr:
                if key_norm in ("尺码", "사이즈"):
                    for tok in _size_tokens(v):
                        if tok in seen:
                            continue
                        seen.add(tok)
                        uniq.append(tok)
                else:
                    vv = _norm_text(v, val_max)
                    if not vv or vv in seen:
                        continue
                    seen.add(vv)
                    uniq.append(vv)
            if uniq:
                prev = out_map.get(key_norm, [])
                seen_prev = set(prev)
                out_map[key_norm] = prev + [x for x in uniq if x not in seen_prev]
        return out_map

    out["attr_map"] = _normalize_attr_map(data.get("attr_map"))
    out["attr_map_ko"] = _normalize_attr_map(data.get("attr_map_ko"), key_max=30, val_max=48)

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


def listing_llm_cny_usable(listing_llm: dict[str, Any]) -> bool:
    """是否为 ``source=openai`` 且 ``cny_price`` 非空（价格值以 LLM 原样为准）。"""
    if listing_llm.get("source") != "openai":
        return False
    cp = listing_llm.get("cny_price")
    if cp is None:
        return False
    return bool(str(cp).strip() and str(cp).strip().lower() != "null" and str(cp).strip() != "-1")


def parse_listing_llm_response(text: str) -> dict[str, Any]:
    raw = _strip_json_fence(text)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM 返回根节点须为 JSON 对象")
    return _normalize_llm_payload(data)


def listing_llm_enabled() -> bool:
    if not (_cfg_get("OPENAI_API_KEY") or "").strip():
        return False
    return _cfg_bool("OPENAI_ENRICH_LISTING", True)


def listing_llm_force_refresh() -> bool:
    return _cfg_bool("OPENAI_LISTING_LLM_FORCE", False)


def listing_llm_batch_size() -> int:
    raw = (_cfg_get("OPENAI_LISTING_LLM_BATCH_SIZE") or "12").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 12
    return max(1, min(n, 50))


def _resolve_timeout(timeout: float | None) -> float:
    if timeout is not None:
        return timeout
    try:
        return float((_cfg_get("OPENAI_TIMEOUT") or "60").strip() or "60")
    except ValueError:
        return 60.0


def _openai_client(timeout: float | None):
    api_key = (_cfg_get("OPENAI_API_KEY") or "").strip()
    base_url = (_cfg_get("OPENAI_BASE_URL") or "").strip() or None
    model = (_cfg_get("OPENAI_MODEL") or "").strip() or "gpt-4o-mini"
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("请安装 openai 库：pip install openai") from e
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=_resolve_timeout(timeout))
    bu = (base_url or "").strip()
    host = urlparse(bu).netloc if bu else "default"
    return client, model, host


def _chat_once_json(
    client,
    *,
    model: str,
    messages: list[dict[str, str]],
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


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enrich_records_listing_llm_batch(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    timeout: float | None = None,
    batch_size: int | None = None,
) -> list[dict[str, Any]]:
    """批量 enrich 多条 (record, commodity)，返回发生变更、建议写回存储层的 record 列表。"""
    if not rows:
        return []

    changed: list[dict[str, Any]] = []
    need_call: list[tuple[int, dict[str, Any], dict[str, Any], str]] = []

    for i, (record, commodity) in enumerate(rows):
        title = str(commodity.get("title") or "").strip()
        gid_short = str(record.get("goods_id") or "")[:36]
        if not title:
            _log.warning(
                "%s",
                pf_kv(
                    [("event", "llm.skip"), ("reason", "no_title"), ("goods_id", gid_short)],
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
                    [("event", "llm.cache"), ("goods_id", gid_short)],
                    zh="使用已有 LLM 缓存，未重新请求接口",
                ),
            )
            continue
        need_call.append((i, record, commodity, title))

    if not need_call:
        return changed

    client, model, host = _openai_client(timeout)
    size = batch_size or listing_llm_batch_size()
    use_response_format = "dashscope.aliyuncs.com" not in host.lower()

    for offset in range(0, len(need_call), size):
        chunk = need_call[offset : offset + size]
        payload = [
            {
                "idx": idx,
                "goods_id": str(record.get("goods_id") or "")[:36],
                "title": title,
            }
            for idx, record, _commodity, title in chunk
        ]
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "llm.batch.request"),
                    ("model", model),
                    ("host", host),
                    ("batch", len(payload)),
                ],
                zh="批量调用 OpenAI 解析上架标题",
            ),
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
            for it in items:
                if not isinstance(it, dict):
                    continue
                idx = it.get("idx")
                if not isinstance(idx, int):
                    continue
                by_idx[idx] = _normalize_llm_payload(it)

            for idx, record, commodity, _title in chunk:
                norm = by_idx.get(idx)
                if norm is None:
                    # 该条缺失时回退单条调用，避免整批失败影响上架。
                    if enrich_record_listing_llm(record, commodity, timeout=timeout):
                        changed.append(record)
                    continue
                norm["source"] = "openai"
                norm["model"] = model
                norm["processed_at"] = _now_utc_iso()
                record["listing_llm"] = norm
                record["llm_processed_at"] = norm["processed_at"]
                apply_listing_llm_price_to_commodity(commodity, norm)
                changed.append(record)
                _log.info(
                    "%s",
                    pf_kv(
                        [
                            ("event", "llm.response"),
                            ("goods_id", str(record.get("goods_id") or "")[:36]),
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
                if enrich_record_listing_llm(record, commodity, timeout=timeout):
                    changed.append(record)

    return changed


def enrich_record_listing_llm(
    record: dict[str, Any],
    commodity: dict[str, Any],
    *,
    timeout: float | None = None,
) -> bool:
    """
    将 LLM 结果写入 ``record['listing_llm']``；识别到价则写入 ``commodity['optimaPrice']``，否则 ``cny_price`` 为 ``null`` 并清空 ``optimaPrice``。
    若已有 ``listing_llm`` 且未设置 ``OPENAI_LISTING_LLM_FORCE``，则不调 API，但仍会把缓存价同步到 ``optimaPrice``。
    返回 **True** 表示应写回 store-json（内存中 ``raw`` 已改）；无 title 等放弃时为 **False**。
    """
    title = str(commodity.get("title") or "").strip()
    if not title:
        _log.warning(
            "%s",
            pf_kv(
                [
                    ("event", "llm.skip"),
                    ("reason", "no_title"),
                    ("goods_id", str(record.get("goods_id") or "")[:36]),
                ],
                zh="跳过 LLM：商品无标题",
            ),
        )
        return False

    existing = record.get("listing_llm")
    if isinstance(existing, dict) and record.get("llm_processed_at") and not listing_llm_force_refresh():
        apply_listing_llm_price_to_commodity(commodity, existing)
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "llm.cache"),
                    ("goods_id", str(record.get("goods_id") or "")[:36])
                ],
                zh="使用已有 LLM 缓存，未重新请求接口",
            ),
        )
        return True

    client, model, host = _openai_client(timeout)
    use_response_format = "dashscope.aliyuncs.com" not in host.lower()
    user_msg = _USER_TMPL.format(title=title)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    gid_short = str(record.get("goods_id") or "")[:36]
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.request"),
                ("model", model),
                ("host", host),
                ("title_len", len(title)),
                ("goods_id", gid_short),
            ],
            zh="正在调用 OpenAI 丰富上架字段（标题价尺码等）",
        ),
    )
    content, elapsed_ms = _chat_once_json(
        client,
        model=model,
        messages=messages,
        use_response_format=use_response_format,
    )

    normalized = parse_listing_llm_response(content)
    normalized["source"] = "openai"
    normalized["model"] = model
    normalized["processed_at"] = _now_utc_iso()
    record["listing_llm"] = normalized
    record["llm_processed_at"] = normalized["processed_at"]
    apply_listing_llm_price_to_commodity(commodity, normalized)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "llm.response"),
                ("goods_id", str(record.get("goods_id") or "")[:36]),
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
