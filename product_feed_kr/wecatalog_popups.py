"""微猫 ``popUpsInfoV2``：加购弹窗规格（尺码/颜色）与售价。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

POPUPS_INFO_V2_PATH = "/newOrder/api/v1/shoppingCart/popUpsInfoV2"


def popups_info_v2_url(*, seller_album_id: str, commodity_id: str) -> str:
    qs = urlencode(
        {
            "sellerAlbumId": seller_album_id.strip(),
            "commodityId": commodity_id.strip(),
            "popUpsType": "individualShopping",
        }
    )
    return f"https://www.wecatalog.cn{POPUPS_INFO_V2_PATH}?{qs}"


def popups_response_ready(resp: Any) -> bool:
    return (
        isinstance(resp, dict)
        and resp.get("success") is True
        and resp.get("errcode") in (0, None)
    )


def popups_commodity(resp: dict[str, Any] | None) -> dict[str, Any] | None:
    if not popups_response_ready(resp):
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    com = result.get("commodity")
    return com if isinstance(com, dict) else None


def _dedupe_str_list(vals: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in vals:
        s = str(v).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _color_name_from_entry(c: Any) -> str:
    if isinstance(c, str):
        return c.strip()
    if not isinstance(c, dict):
        return ""
    for key in ("colorName", "formatName", "name", "colourName"):
        v = c.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def extract_format_options(resp: dict[str, Any] | None) -> dict[str, list[str]]:
    """从 popUps 响应提取尺码/颜色列表（``formatType`` 1=尺码、2=颜色；单维度时全部视为尺码）。"""
    com = popups_commodity(resp)
    if not com:
        return {"sizes": [], "colors": []}

    colors: list[str] = []
    for c in com.get("colors") or []:
        name = _color_name_from_entry(c)
        if name:
            colors.append(name)

    formats = [f for f in (com.get("formats") or []) if isinstance(f, dict)]
    format_types = {
        f.get("formatType")
        for f in formats
        if f.get("formatType") is not None
    }
    multi_dim = len(format_types) > 1

    sizes: list[str] = []
    for f in formats:
        name = str(f.get("formatName") or "").strip()
        if not name:
            continue
        ft = f.get("formatType")
        if ft == 2:
            colors.append(name)
        elif ft == 1 or not multi_dim:
            sizes.append(name)
        else:
            sizes.append(name)

    if not sizes:
        for sku in com.get("skus") or []:
            if not isinstance(sku, dict):
                continue
            sn = str(sku.get("skuName") or "").strip()
            if sn:
                sizes.append(sn)

    return {
        "sizes": _dedupe_str_list(sizes),
        "colors": _dedupe_str_list(colors),
    }


def popups_optima_price_cny(resp: dict[str, Any] | None) -> str | None:
    """``result.commodity.optimaPrice`` → 人民币售价字符串（无则 None）。"""
    com = popups_commodity(resp)
    if not com:
        return None
    raw = com.get("optimaPrice")
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("0", "0.0", "-1"):
        return None
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or None
