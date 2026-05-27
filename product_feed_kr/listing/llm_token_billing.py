"""LLM 输入 token 分档计费（元 / 百万 prompt tokens，按单次请求档位）。"""

from __future__ import annotations

from product_feed_kr.common.seven17_config import getenv as _cfg_get

# 单次请求输入 token 档位（与通义等价表一致时可改环境变量）
TIER_INPUT_MAX_256K = 256_000
TIER_INPUT_MAX_1M = 1_000_000


def _price_per_million_small() -> float:
    try:
        return float((_cfg_get("LISTING_LLM_INPUT_PRICE_PER_M_256K") or "2").strip())
    except ValueError:
        return 2.0


def _price_per_million_large() -> float:
    try:
        return float((_cfg_get("LISTING_LLM_INPUT_PRICE_PER_M_1M") or "8").strip())
    except ValueError:
        return 8.0


def llm_input_price_per_million(prompt_tokens: int) -> float:
    """单次请求输入 token 数 → 适用单价（元/百万 token）。"""
    pt = max(0, int(prompt_tokens))
    if pt <= 0:
        return 0.0
    if pt <= TIER_INPUT_MAX_256K:
        return _price_per_million_small()
    if pt <= TIER_INPUT_MAX_1M:
        return _price_per_million_large()
    return _price_per_million_large()


def llm_input_cost_tier_label(prompt_tokens: int) -> str:
    pt = max(0, int(prompt_tokens))
    if pt <= 0:
        return "-"
    if pt <= TIER_INPUT_MAX_256K:
        return f"≤256K@{_price_per_million_small():g}元/M"
    if pt <= TIER_INPUT_MAX_1M:
        return f"256K-1M@{_price_per_million_large():g}元/M"
    return f">1M@{_price_per_million_large():g}元/M(估)"


def llm_input_cost_yuan(prompt_tokens: int) -> float:
    """单次请求输入费用（元），按该次 ``prompt_tokens`` 所在档位单价计费。"""
    pt = max(0, int(prompt_tokens))
    if pt <= 0:
        return 0.0
    rate = llm_input_price_per_million(pt)
    return pt * rate / 1_000_000.0


def format_cost_yuan(yuan: float) -> str:
    if yuan <= 0:
        return "0"
    if yuan < 0.01:
        return f"{yuan:.6f}"
    if yuan < 1:
        return f"{yuan:.4f}"
    return f"{yuan:.2f}"
