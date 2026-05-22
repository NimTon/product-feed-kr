"""对比 ``OPENAI_PROFILES`` 各厂商对**同一条**商品的 LLM 结果，输出 JSON 报告便于肉眼对比。

默认 **dry-run**：只加载商品、列出将调用的 profile，**不请求 API**。
配置填好后执行::

  python -m product_feed_kr.llm_providers_compare --index 0 --run
  python -m product_feed_kr.llm_providers_compare --goods-id YOUR_GOODS_ID --run -o data/llm_compare/out.json

报告写入 ``data/llm_compare/``（可用 ``-o`` 指定路径）。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from product_feed_kr.listing_llm_enrich import (
    ListingLlmApiProfile,
    enrich_record_listing_llm,
    listing_llm_all_profile_slots,
    listing_llm_color_vision_enabled,
)
from product_feed_kr.pf_log import configure_llm_logging
from product_feed_kr.seven17_config import getenv as _cfg_get
from product_feed_kr.seven17_config import reload_seven17_config
from product_feed_kr.wego_commodity import commodity_image_urls


def _commodity_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    from product_feed_kr.wecatalog_store_record import commodity_from_wecatalog_record

    return commodity_from_wecatalog_record(record)


def _load_store_record(
    album_id: str,
    *,
    index: int | None,
    goods_id: str | None,
) -> dict[str, Any]:
    from product_feed_kr.store_sqlite import connect_sqlite, ensure_sqlite_schema, sqlite_load_products_for_upload

    aid = album_id.strip()
    if not aid:
        raise ValueError("album_id 不能为空")
    conn = connect_sqlite()
    try:
        ensure_sqlite_schema(conn)
        items = sqlite_load_products_for_upload(conn, aid, skip_uploaded=False)
        if goods_id:
            gid = goods_id.strip()
            for rec in items:
                if isinstance(rec, dict) and str(rec.get("goods_id") or "").strip() == gid:
                    return rec
            raise ValueError(f"未找到 goods_id={gid!r}（album_id={aid}）")
        idx = 0 if index is None else index
        if idx < 0 or idx >= len(items):
            raise IndexError(f"index={idx} 超出范围（共 {len(items)} 条）")
        rec = items[idx]
        if not isinstance(rec, dict):
            raise ValueError(f"index={idx} 对应记录不是对象")
        return rec
    finally:
        conn.close()


def _input_summary(record: dict[str, Any], commodity: dict[str, Any]) -> dict[str, Any]:
    title = str(commodity.get("title") or "").strip()
    urls = commodity_image_urls(commodity)
    return {
        "album_id": str(record.get("album_id") or _cfg_get("WECATALOG_ALBUM_ID") or ""),
        "goods_id": str(record.get("goods_id") or ""),
        "tag_id": record.get("tag_id"),
        "wecatalog_group": record.get("wecatalog_group"),
        "wecatalog_tag": record.get("wecatalog_tag"),
        "title": title,
        "title_len": len(title),
        "image_url_count": len(urls),
        "image_urls_sample": urls[:4],
        "had_listing_llm_in_db": isinstance(record.get("listing_llm"), dict),
        "color_vision_enabled": listing_llm_color_vision_enabled(),
    }


def _profile_plan(profiles: list[ListingLlmApiProfile]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in profiles:
        base = (p.get("base_url") or "").strip()
        out.append(
            {
                "label": p.get("label") or "",
                "model": p.get("model") or "",
                "base_url": base,
                "host": urlparse(base).netloc if base else "",
                "api_key_configured": bool(str(p.get("api_key") or "").strip()),
            },
        )
    return out


def _prepare_record_for_fresh_llm(record: dict[str, Any]) -> dict[str, Any]:
    """每条厂商单独跑：去掉库内 LLM 缓存，避免走 cache 分支。"""
    rec = copy.deepcopy(record)
    rec.pop("listing_llm", None)
    rec.pop("llm_processed_at", None)
    return rec


def _listing_llm_snapshot(record: dict[str, Any], commodity: dict[str, Any]) -> dict[str, Any]:
    llm = record.get("listing_llm")
    if not isinstance(llm, dict):
        return {}
    snap = copy.deepcopy(llm)
    snap["optimaPrice_after"] = commodity.get("optimaPrice")
    return snap


def run_compare(
    record: dict[str, Any],
    *,
    dry_run: bool,
    profiles: list[ListingLlmApiProfile] | None = None,
) -> dict[str, Any]:
    profiles = profiles if profiles is not None else listing_llm_all_profile_slots()
    commodity = _commodity_from_record(record)
    if commodity is None:
        raise ValueError("该条无 commodity_title（结构化抓取字段），无法做 LLM 对比")

    base_input = _input_summary(record, commodity)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "profile_count": len(profiles),
        "input": base_input,
        "profiles": _profile_plan(profiles),
        "results": {},
    }

    if dry_run:
        for p in profiles:
            label = str(p.get("label") or p.get("model") or "unknown")
            report["results"][label] = {
                "status": "skipped",
                "reason": "dry_run",
                "api_key_configured": bool(str(p.get("api_key") or "").strip()),
            }
        return report

    for profile in profiles:
        label = str(profile.get("label") or profile.get("model") or "unknown")
        if not str(profile.get("api_key") or "").strip():
            report["results"][label] = {
                "status": "skipped",
                "reason": "api_key_empty",
                "model": profile.get("model"),
                "base_url": profile.get("base_url"),
            }
            continue

        rec = _prepare_record_for_fresh_llm(record)
        com = copy.deepcopy(commodity)
        t0 = time.monotonic()
        try:
            ok = enrich_record_listing_llm(rec, com, api_profile=profile)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if not ok:
                report["results"][label] = {
                    "status": "fail",
                    "reason": "enrich_returned_false",
                    "elapsed_ms": elapsed_ms,
                    "model": profile.get("model"),
                    "base_url": profile.get("base_url"),
                }
                continue
            report["results"][label] = {
                "status": "ok",
                "elapsed_ms": elapsed_ms,
                "model": profile.get("model"),
                "base_url": profile.get("base_url"),
                "listing_llm": _listing_llm_snapshot(rec, com),
            }
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            report["results"][label] = {
                "status": "error",
                "error": str(e),
                "elapsed_ms": elapsed_ms,
                "model": profile.get("model"),
                "base_url": profile.get("base_url"),
            }

    return report


def _default_output_path(record: dict[str, Any]) -> Path:
    gid = str(record.get("goods_id") or "unknown")[:24]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Path("data") / "llm_compare" / f"compare_{gid}_{ts}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对比各 OPENAI_PROFILES 对同一商品的 LLM 输出（JSON 报告）")
    parser.add_argument("--album-id", default=None, help="相册 ID；默认 WECATALOG_ALBUM_ID")
    parser.add_argument("--index", type=int, default=0, help="SQLite 中第几条（默认 0）")
    parser.add_argument("--goods-id", default=None, help="指定 goods_id（优先于 --index）")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="输出 JSON 路径；默认 data/llm_compare/compare_<goods_id>_<ts>.json",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="实际调用各厂商 API（默认仅 dry-run，不请求）",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="同时打印 JSON 到 stdout",
    )
    args = parser.parse_args(argv)

    configure_llm_logging()
    reload_seven17_config()
    aid = (args.album_id or _cfg_get("WECATALOG_ALBUM_ID") or "").strip()
    if not aid:
        parser.error("缺少 album_id：--album-id 或配置 WECATALOG_ALBUM_ID")

    profiles = listing_llm_all_profile_slots()
    if not profiles:
        print("OPENAI_PROFILES 为空，请先在 config/seven17.json 配置。", file=sys.stderr)
        return 2

    try:
        record = _load_store_record(aid, index=args.index, goods_id=args.goods_id)
    except (ValueError, IndexError) as e:
        print(str(e), file=sys.stderr)
        return 1

    dry_run = not args.run
    report = run_compare(record, dry_run=dry_run, profiles=profiles)

    out_path = args.output or _default_output_path(record)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    mode = "dry-run（未调 API）" if dry_run else "已调 API"
    print(f"[llm-compare] {mode} profiles={len(profiles)} -> {out_path.resolve()}")
    if dry_run:
        print("[llm-compare] 配置好 api_key 后加 --run 才会真实请求各厂商。")
    if args.stdout:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
