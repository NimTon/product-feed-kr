"""查询 1 CNY 兑多少 KRW（韩元），用于抓取写库与 seven17 上架填价。"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from product_feed_kr.common.pf_log import pf_kv
from product_feed_kr.common.seven17_config import getenv as _cfg_get

_log = logging.getLogger("product_feed_kr.common.cny_krw_rate")

# fawazahmed0/currency-api（jsDelivr）；失败时由 seven17 配置 SEVEN17_CNY_KRW_FALLBACK 兜底
_CNY_KRW_URLS: tuple[str, ...] = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/cny.json",
)


def fetch_krw_per_cny(*, timeout: float = 20.0) -> float:
    """返回 1 元人民币对应的韩元数量（正浮点数）。"""
    last_err: BaseException | None = None
    for url in _CNY_KRW_URLS:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) product-feed-kr",
                    "Accept": "application/json",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
            data = json.loads(raw)
            cny = data.get("cny")
            if not isinstance(cny, dict):
                raise ValueError("响应缺少 cny 对象")
            krw = cny.get("krw")
            if krw is None:
                raise ValueError("响应缺少 cny.krw")
            rate = float(krw)
            if rate <= 0:
                raise ValueError(f"无效汇率 {rate!r}")
            return rate
        except (urllib.error.URLError, OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            last_err = e
            continue
    raise RuntimeError(f"无法拉取 CNY→KRW 汇率（已尝试 {len(_CNY_KRW_URLS)} 个地址）：{last_err!s}") from last_err


def resolve_krw_per_cny() -> tuple[float, str]:
    """1 CNY 兑 KRW：优先 ``SEVEN17_CNY_KRW_RATE``，其次实时接口，失败用 ``SEVEN17_CNY_KRW_FALLBACK``。

    返回 ``(汇率, 来源标记)``，来源为 ``manual`` / ``live`` / ``fallback``。
    """
    manual = _cfg_get("SEVEN17_CNY_KRW_RATE")
    if manual and str(manual).strip():
        v = float(str(manual).strip().replace(",", ""))
        if v <= 0:
            raise RuntimeError("SEVEN17_CNY_KRW_RATE 须为正数")
        return v, "manual"
    try:
        return fetch_krw_per_cny(), "live"
    except Exception as e:
        fb = _cfg_get("SEVEN17_CNY_KRW_FALLBACK")
        if fb and str(fb).strip():
            v = float(str(fb).strip().replace(",", ""))
            if v <= 0:
                raise RuntimeError("SEVEN17_CNY_KRW_FALLBACK 须为正数")
            _log.warning(
                "%s",
                pf_kv(
                    [("event", "fx.warn"), ("reason", "live_failed_use_fallback"), ("err", str(e))],
                    zh="实时 CNY→KRW 失败，已改用配置兜底汇率",
                ),
            )
            return v, "fallback"
        raise RuntimeError(
            "CNY→KRW 实时汇率不可用：请设置 SEVEN17_CNY_KRW_FALLBACK 或 SEVEN17_CNY_KRW_RATE"
        ) from e


def cny_amount_to_krw_won_str(amount_cny: str, krw_per_cny: float) -> str | None:
    """人民币售价 → 韩元整数（千韩元取整）；无效输入返回 None。"""
    s = str(amount_cny).strip().replace(",", "")
    if not s or s in ("0", "-1"):
        return None
    krw = cny_listing_amount_to_krw_won_str(s, krw_per_cny)
    return krw if krw and krw != "0" else None


def cny_listing_amount_to_krw_won_str(amount_cny: str, krw_per_cny: float) -> str:
    """将货源侧人民币售价字符串转为韩元整数（원），写入 shop it_price。

    换算后按 **千韩元** 四舍五入，使金额末三位为 ``000``（与韩国电商常见标价习惯一致）。
    """
    s = str(amount_cny).strip().replace(",", "")
    if not s or s == "0":
        return "0"
    try:
        cny = float(s)
    except ValueError:
        return "0"
    if cny <= 0:
        return "0"
    won = int(round(cny * krw_per_cny))
    if won <= 0:
        return "0"
    # 千韩元四舍五入（正数）：末三位固定为 000；非零且不足半千时至少 1000。
    thousand = (won + 500) // 1000 * 1000
    if thousand == 0:
        thousand = 1000
    return str(thousand)
