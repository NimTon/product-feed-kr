"""LLM token usage 解析与累计。"""

from product_feed_kr.listing_llm_enrich import (
    _chat_usage_from_response,
    llm_token_usage_run_snapshot,
    note_llm_token_usage,
    reset_llm_token_usage_run,
)


class _Usage:
    prompt_tokens = 100
    completion_tokens = 40
    total_tokens = 140


class _Resp:
    usage = _Usage()


def test_chat_usage_from_response() -> None:
    u = _chat_usage_from_response(_Resp())
    assert u["prompt_tokens"] == 100
    assert u["completion_tokens"] == 40
    assert u["total_tokens"] == 140


def test_token_usage_run_accumulates() -> None:
    reset_llm_token_usage_run()
    note_llm_token_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    note_llm_token_usage({"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28})
    snap = llm_token_usage_run_snapshot()
    assert snap["requests"] == 2
    assert snap["prompt_tokens"] == 30
    assert snap["completion_tokens"] == 13
    assert snap["total_tokens"] == 43
    assert float(snap["cost_yuan"]) > 0
