"""爬取后尺码修复：服装数字档 → S/M/L；鞋类欧码区间展开。

韩文毫米脚长（``sizes_ko_json`` / ``attr_map_ko.사이즈``）仅由 LLM ``size_spec_kind=footwear`` 触发，
见 ``listing_llm_enrich.apply_listing_size_fix_from_zh``。
"""

from __future__ import annotations

import re

# 按位：0=S、1=M、2=L、3=XL…
_DIGIT_SLOT_LETTERS = (
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

# 欧码 → 韩版毫米脚长（与 listing_llm_enrich 对照表一致）
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

# 38-41、0-4、38~41 等区间
_SIZE_RANGE_RE = re.compile(
    r"^\s*(\d+(?:\.[05])?)\s*[-~–—到至]\s*(\d+(?:\.[05])?)\s*$",
    re.I,
)


def dedupe_str_list(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def expand_digit_slot_code(token: str) -> list[str] | None:
    """单段纯数字表示多档时展开为字母尺码；鞋码/年份等返回 None 保持原样。"""
    ds = str(token).strip()
    if not ds.isdigit():
        return None
    n = len(ds)
    if n == 1:
        i = int(ds)
        if i >= len(_DIGIT_SLOT_LETTERS):
            return None
        return [_DIGIT_SLOT_LETTERS[i]]
    if n == 2:
        v = int(ds)
        if 30 <= v <= 52:
            return None
        if v in (44, 55, 66, 77, 88, 99):
            return None
        return [_DIGIT_SLOT_LETTERS[int(c)] for c in ds]
    if n == 3:
        v = int(ds)
        if 210 <= v <= 320:
            return None
        if 100 <= v <= 200:
            return None
        return [_DIGIT_SLOT_LETTERS[int(c)] for c in ds]
    if n == 4 and 1900 <= int(ds) <= 2035:
        return None
    out: list[str] = []
    for c in ds:
        i = int(c)
        if i >= len(_DIGIT_SLOT_LETTERS):
            return None
        out.append(_DIGIT_SLOT_LETTERS[i])
    return out


def _format_eu_size_num(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    whole = int(x)
    if abs(x - whole - 0.5) < 1e-9:
        return f"{whole}.5"
    return str(x).rstrip("0").rstrip(".")


def _expand_shoe_eu_range(lo: float, hi: float) -> list[str]:
    """欧码区间（含 38.5-41）按 0.5 或 1 步长展开。"""
    use_half = (lo % 1 == 0.5) or (hi % 1 == 0.5)
    step = 0.5 if use_half else 1.0
    out: list[str] = []
    x = lo
    while x <= hi + 1e-9:
        out.append(_format_eu_size_num(x))
        x += step
    return out


def expand_size_range_token(token: str) -> list[str] | None:
    """
    展开区间尺码：``0-4`` → 数字档 ``0``…``4``；``38-41`` → 欧码 ``38``…``41``。
    无法识别时返回 None（由调用方保留原 token）。
    """
    t = str(token).strip()
    if not t:
        return None
    m = _SIZE_RANGE_RE.match(t)
    if not m:
        return None
    try:
        lo = float(m.group(1))
        hi = float(m.group(2))
    except ValueError:
        return None
    if lo > hi:
        lo, hi = hi, lo
    if hi - lo > 24:
        return None

    lo_i, hi_i = int(lo), int(hi)
    if lo == lo_i and hi == hi_i and 0 <= lo_i <= 9 and 0 <= hi_i <= 9:
        return [str(i) for i in range(lo_i, hi_i + 1)]

    if (
        _EU_SHOE_SIZE_MIN <= lo <= _EU_SHOE_SIZE_MAX
        and _EU_SHOE_SIZE_MIN <= hi <= _EU_SHOE_SIZE_MAX
    ):
        return _expand_shoe_eu_range(lo, hi)

    return None


def fix_scrape_sizes(sizes: list[str]) -> list[str]:
    """修复 popUps 尺码：区间/数字档 → 单码或 S/M/L…；鞋码欧码保持原样。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in sizes:
        s = str(raw).strip()
        if not s:
            continue
        ranged = expand_size_range_token(s)
        tokens = ranged if ranged else [s]
        for tok in tokens:
            expanded = expand_digit_slot_code(tok)
            if expanded:
                for letter in expanded:
                    if letter in seen:
                        continue
                    seen.add(letter)
                    out.append(letter)
                continue
            tok_norm = tok.upper() if tok.isascii() and tok.replace(".", "", 1).isalnum() else tok
            if tok_norm in seen:
                continue
            seen.add(tok_norm)
            out.append(tok_norm)
    return out


def shoe_size_token_to_kr_mm(tok: str) -> str:
    """鞋类：欧码 / EU42 → 韩版毫米；已是 210–320 毫米或字母码则原样。"""
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
    if re.search(r"[A-Za-z]", t0) and not re.fullmatch(
        r"(?i)EU\s*[0-9]{2}(?:\.[05])?", t0
    ):
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


def shoe_sizes_to_kr_mm(tokens: list[str]) -> list[str]:
    mapped = [shoe_size_token_to_kr_mm(x) for x in tokens]
    return dedupe_str_list(mapped)


def apply_scrape_size_fix(fields: dict) -> None:
    """就地修复 ``commodity_sizes``（区间/数字档）；不写 ``commodity_sizes_ko``（由 LLM 决定）。"""
    raw = fields.get("commodity_sizes")
    if not isinstance(raw, list) or not raw:
        return
    fields["commodity_sizes"] = fix_scrape_sizes(list(raw))
    fields.pop("commodity_sizes_ko", None)
