"""单条商品试跑 listing LLM（用法: python -m test.try_listing_llm_one [--live]）。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from product_feed_kr.listing_llm_enrich import (
    _normalize_llm_payload,
    _text_suggests_footwear,
    enrich_record_listing_llm,
    listing_llm_api_profiles,
    listing_llm_enabled,
    parse_listing_llm_response,
)

TITLE = (
    "本地自取💰650 Balenciaga 巴黎世家 Runner 破坏风 VG版本 巴黎七代半 "
    "手工做旧款复古老爹鞋 尺码：35 36 37 38 39 40 41 42 43 44 45 46 编号：50IFTU6"
)

OLD_LISTING_LLM = {
    "cny_price": "650",
    "attr_map": {
        "颜色": ["白色", "蓝色"],
        "尺码": ["35", "36", "37", "38", "39", "40", "41", "42", "43", "44", "45", "46"],
    },
    "attr_map_ko": {
        "색상": ["흰색", "파란색"],
        "사이즈": ["L", "XXXL", "XL", "S", "XXL", "4XL", "5XL", "6XL", "7XL"],
    },
    "name_zh": "巴黎世家 Runner 复古老爹鞋",
    "name_ko": "발렌시아가 러너 복고풍 스니커즈",
    "desc_zh": (
        "巴黎世家 Balenciaga Runner 复古老爹鞋，巴黎七代半款式，VG 版本。"
        "设计采用破坏风风格，结合手工做旧工艺，呈现独特复古质感。"
        "鞋底与鞋面细节丰富，配色清新。尺码从 35 到 46 齐全，支持本地自取，编号 50IFTU6。"
    ),
    "desc_ko": (
        "발렌시아가 Balenciaga Runner 빈티지 스니커즈로, 파리 7.5 세대 스타일이며 VG 버전입니다. "
        "35 부터 46 까지 사이즈가 다양하며 현지 픽업이 가능하고 번호는 50IFTU6 입니다."
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="调用 LLM API 重新抽取（需 config/seven17.json）")
    args = ap.parse_args()

    print("=== 鞋类判定 ===")
    print("title:", _text_suggests_footwear(TITLE))
    hint = " ".join([TITLE, OLD_LISTING_LLM["name_zh"], OLD_LISTING_LLM["name_ko"], OLD_LISTING_LLM["desc_zh"]])
    print("title+name+desc_zh:", _text_suggests_footwear(hint))

    print("\n=== 仅后处理（不重调 LLM，修复 attr_map_ko.사이즈）===")
    fixed = _normalize_llm_payload(OLD_LISTING_LLM, listing_hint=TITLE)
    print("attr_map.尺码:", fixed.get("attr_map", {}).get("尺码"))
    print("attr_map_ko.사이즈:", fixed.get("attr_map_ko", {}).get("사이즈"))

    if not args.live:
        print("\n（未加 --live，跳过 API。加 --live 可真实调用 LLM）")
        return 0

    if not listing_llm_enabled():
        print("LLM 未启用或未配置 API profiles", file=sys.stderr)
        return 1

    os.environ["OPENAI_LISTING_LLM_FORCE"] = "1"
    record = {
        "id": 5244152,
        "store_row_id": 5244152,
        "goods_id": "_di3qfG2QOAuvWheLgPGKINRqeV252cQY9g9Ofxg",
        "listing_llm": dict(OLD_LISTING_LLM),
        "llm_processed_at": "2026-05-20T22:29:38+08:00",
        "llm_attempt_count": 0,
    }
    commodity = {"title": TITLE, "optimaPrice": "650"}
    profiles = listing_llm_api_profiles()
    print("\n=== 调用 LLM ===")
    print("profiles:", [p.get("label") for p in profiles])
    ok = enrich_record_listing_llm(record, commodity, api_profile=profiles[0])
    ll = record.get("listing_llm") or {}
    print("ok:", ok)
    print(json.dumps(ll, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
