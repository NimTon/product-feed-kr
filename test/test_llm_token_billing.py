"""LLM 输入 token 分档计费。"""

from product_feed_kr.llm_token_billing import (
    llm_input_cost_tier_label,
    llm_input_cost_yuan,
    llm_input_price_per_million,
)


def test_tier_256k_price() -> None:
    assert llm_input_price_per_million(1000) == 2.0
    assert llm_input_price_per_million(256_000) == 2.0
    assert llm_input_cost_tier_label(100_000).startswith("≤256K")


def test_tier_1m_price() -> None:
    assert llm_input_price_per_million(256_001) == 8.0
    assert llm_input_price_per_million(500_000) == 8.0
    assert "256K-1M" in llm_input_cost_tier_label(300_000)


def test_cost_calculation() -> None:
    # 10万 input @ 2元/M = 0.2元
    assert abs(llm_input_cost_yuan(100_000) - 0.2) < 1e-9
    # 30万 @ 8元/M = 2.4元
    assert abs(llm_input_cost_yuan(300_000) - 2.4) < 1e-6
    # 100万 @ 8元/M = 8元
    assert abs(llm_input_cost_yuan(1_000_000) - 8.0) < 1e-9
