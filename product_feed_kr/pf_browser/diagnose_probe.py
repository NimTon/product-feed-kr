"""诊断：实时拉取微猫 commodity/view + 标签树，分析未入库的具体原因。"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

from product_feed_kr.common.seven17_config import bool_env
from product_feed_kr.db.store_sqlite import connect_sqlite, sqlite_load_store_snapshot
from product_feed_kr.listing.listing_llm_enrich import record_is_no_price_allowed_by_map_category
from product_feed_kr.wecatalog.wecatalog_fetch_tags import (
    _launch_browser,
    build_group_tree,
    tags_api_url,
)
from product_feed_kr.wecatalog.wecatalog_popups import (
    POPUPS_ERR_COMMODITY_INVALID,
    POPUPS_ERR_LOGIN_EXPIRED,
    popups_errcode,
    popups_errmsg,
)
from product_feed_kr.wecatalog.wecatalog_scrape_fields import (
    detail_response_ready,
    fields_from_list_item,
    list_item_scrape_ready,
    scrape_fields_has_price_cny,
    scrape_no_price_skip_needed,
)
from product_feed_kr.wecatalog.wecatalog_scrape_store import FETCH_TAGS_JS, goods_page_url, list_progress_from_stats
from product_feed_kr.wecatalog.wecatalog_tag_mapping import resolve_category_path
from product_feed_kr.wego.wego_commodity import commodity_raw_media_urls, filter_image_urls

_log = logging.getLogger(__name__)

_PLACEHOLDER = "（待补全）"

FETCH_VIEW_JS = """
async ({ targetAlbumId, itemId }) => {
  const t = Date.now();
  const qs = new URLSearchParams({
    targetAlbumId,
    itemId,
    t: String(t),
  });
  const u = "https://www.wecatalog.cn/commodity/view?" + qs.toString();
  const r = await fetch(u, {
    credentials: "include",
    headers: { Accept: "application/json, text/plain, */*" },
  });
  return await r.json();
}
"""


def commodity_view_url(album_id: str, goods_id: str) -> str:
    qs = urlencode(
        {
            "targetAlbumId": album_id.strip(),
            "itemId": goods_id.strip(),
            "t": str(int(time.time() * 1000)),
        },
    )
    return f"https://www.wecatalog.cn/commodity/view?{qs}"


def view_commodity(view_resp: dict[str, Any] | None) -> dict[str, Any] | None:
    if not detail_response_ready(view_resp):
        return None
    assert isinstance(view_resp, dict)
    result = view_resp.get("result")
    if not isinstance(result, dict):
        return None
    com = result.get("commodity")
    return com if isinstance(com, dict) else None


def _tag_id_to_group_tag(
    groups: list[dict[str, Any]],
    tag_id: int,
) -> tuple[str, str] | None:
    for g in groups:
        if not isinstance(g, dict):
            continue
        gname = str(g.get("groupName") or "").strip()
        for t in g.get("tags") or []:
            if not isinstance(t, dict):
                continue
            try:
                tid = int(t.get("tagId"))
            except (TypeError, ValueError):
                continue
            if tid == tag_id:
                tname = str(t.get("tagName") or "").strip()
                if gname and tname:
                    return gname, tname
    return None


def _path_valid(path: tuple[str, ...] | None) -> bool:
    if not path:
        return False
    return all(_PLACEHOLDER not in str(seg) for seg in path)


def _tag_entries_from_commodity(com: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(com, dict):
        return []
    raw = com.get("tags") or []
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, dict)]


def _scrape_list_progress(album_id: str) -> dict[str, Any] | None:
    conn = connect_sqlite()
    try:
        snap = sqlite_load_store_snapshot(conn, album_id)
        if not isinstance(snap, dict):
            return None
        stats = snap.get("stats")
        if not isinstance(stats, dict):
            return None
        done_pages, next_ts = list_progress_from_stats(stats)
        return {
            "list_page_num": done_pages or None,
            "list_page_next_ts": next_ts,
            "map_unmapped": stats.get("map_unmapped"),
            "list_no_price": stats.get("list_no_price"),
            "list_incomplete": stats.get("list_incomplete"),
        }
    finally:
        conn.close()


def analyze_live_scrape_blockers(
    *,
    album_id: str,
    goods_id: str,
    groups: list[dict[str, Any]],
    view_resp: dict[str, Any] | None,
    scrape_progress: dict[str, Any] | None,
) -> dict[str, Any]:
    """根据 commodity/view 实时数据推断采集阶段是否会跳过及原因。"""
    blockers: list[dict[str, str]] = []
    findings: list[dict[str, str]] = []
    skip_uncategorized = bool_env("WECATALOG_SCRAPE_SKIP_UNCATEGORIZED", True)

    ec = popups_errcode(view_resp)
    em = popups_errmsg(view_resp)
    com = view_commodity(view_resp)

    findings.append(
        {
            "key": "view_api",
            "label": "commodity/view",
            "value": commodity_view_url(album_id, goods_id),
        },
    )

    if view_resp is None:
        blockers.append(
            {
                "code": "probe_view_failed",
                "detail_zh": "未能请求 commodity/view（浏览器异常）",
            },
        )
    elif ec == POPUPS_ERR_COMMODITY_INVALID:
        blockers.append(
            {
                "code": "popups_invalid",
                "detail_zh": f"微猫返回商品已失效（errcode={ec}，{em or '无 errmsg'}）",
            },
        )
    elif ec == POPUPS_ERR_LOGIN_EXPIRED:
        blockers.append(
            {
                "code": "view_login_expired",
                "detail_zh": "微猫登录已过期（errcode=9）；请用 01 采集 --headed 在浏览器内登录后重试诊断",
            },
        )
    elif not detail_response_ready(view_resp):
        blockers.append(
            {
                "code": "view_error",
                "detail_zh": f"commodity/view 未返回有效商品（errcode={ec}，{em or '无 errmsg'}）",
            },
        )
    elif com is not None:
        scrape_fields = fields_from_list_item(com)
        title = str(scrape_fields.get("commodity_title") or "").strip()
        findings.append(
            {
                "key": "view_title",
                "label": "商品标题",
                "value": title or "（空）",
            },
        )
        price = scrape_fields.get("price_cny")
        findings.append(
            {
                "key": "view_price",
                "label": "商品价格",
                "value": str(price).strip() if price else "无有效人民币价（含标题💰解析）",
            },
        )
    else:
        scrape_fields = {}

    if com is not None:
        scrape_fields = fields_from_list_item(com)
    else:
        scrape_fields = {}

    if scrape_progress and scrape_progress.get("list_page_num"):
        findings.append(
            {
                "key": "scrape_progress",
                "label": "采集列表断点",
                "value": f"约第 {scrape_progress['list_page_num']} 页（仅供参考，诊断不翻列表）",
            },
        )

    raw_media = commodity_raw_media_urls(com) if com else []
    image_urls = filter_image_urls(raw_media)
    if raw_media and not image_urls:
        blockers.append(
            {
                "code": "video_media",
                "detail_zh": "媒体仅为视频（imgsSrc/imgs 无静态图），上架与列表入库均会跳过",
            },
        )
        findings.append(
            {
                "key": "media",
                "label": "媒体",
                "value": f"仅视频 {len(raw_media)} 个，无静态图",
            },
        )
    else:
        findings.append(
            {
                "key": "media",
                "label": "媒体",
                "value": f"静态图 {len(image_urls)} 张" if image_urls else "无图片 URL",
            },
        )

    if com is not None and not list_item_scrape_ready(com, scrape_fields):
        missing: list[str] = []
        if not str(scrape_fields.get("commodity_title") or "").strip():
            missing.append("标题")
        if not (scrape_fields.get("commodity_image_urls") or []):
            missing.append("图片")
        blockers.append(
            {
                "code": "list_incomplete",
                "detail_zh": f"商品缺{'/'.join(missing)}，采集会记入 pf_scrape_skip（list_incomplete）",
            },
        )

    tag_entries = _tag_entries_from_commodity(com)
    tag_contexts: list[dict[str, Any]] = []

    if com is not None and not tag_entries:
        blockers.append(
            {
                "code": "no_tags",
                "detail_zh": "商品无微猫标签，无法匹配分类配对 JSON",
            },
        )
    elif com is not None:
        has_price = scrape_fields_has_price_cny(scrape_fields)
        findings.append(
            {
                "key": "has_price",
                "label": "有效价格",
                "value": "是" if has_price else "否",
            },
        )
        findings.append(
            {
                "key": "skip_uncategorized",
                "label": "skip-uncategorized",
                "value": "开启（未映射分类不爬）" if skip_uncategorized else "关闭",
            },
        )

        for te in tag_entries:
            try:
                tid = int(te.get("tagId"))
            except (TypeError, ValueError):
                continue
            tname = str(te.get("tagName") or "").strip()
            mapped = _tag_id_to_group_tag(groups, tid)
            if mapped:
                gname, tname = mapped
            else:
                gname = ""
            ctx: dict[str, Any] = {
                "tag_id": tid,
                "wecatalog_group": gname or None,
                "wecatalog_tag": tname or None,
            }
            path = resolve_category_path(gname, tname) if gname and tname else None
            ctx["shop_category_path"] = list(path) if path else None
            ctx["has_category_mapping"] = _path_valid(path)

            if gname and tname:
                if skip_uncategorized and not ctx["has_category_mapping"]:
                    if path and any(_PLACEHOLDER in str(s) for s in path):
                        detail = (
                            f"分类「{gname} / {tname}」映射为占位路径（待补全），"
                            "开启 --skip-uncategorized 时不采集"
                        )
                    else:
                        detail = (
                            f"分类「{gname} / {tname}」在 data/wecatalog_category_pairs.json 中无匹配，"
                            "请在 05 商品库浏览的「分类配对」中配置；"
                            "开启 --skip-uncategorized 时不采集"
                        )
                    blockers.append({"code": "unmapped_category", "detail_zh": detail})
                    ctx["would_skip_uncategorized"] = True
                else:
                    ctx["would_skip_uncategorized"] = False

                no_price_skip = scrape_no_price_skip_needed(
                    wecatalog_group=gname,
                    wecatalog_tag=tname,
                    scrape_fields=scrape_fields,
                )
                ctx["no_price_skip"] = no_price_skip
                ctx["no_price_whitelist"] = record_is_no_price_allowed_by_map_category(
                    {"wecatalog_group": gname, "wecatalog_tag": tname},
                )
                if no_price_skip:
                    blockers.append(
                        {
                            "code": "list_no_price",
                            "detail_zh": (
                                f"分类「{gname} / {tname}」无有效价格，且不在无价白名单"
                                "（SEVEN17_NO_PRICE_ALLOW_CATEGORIES），采集会跳过"
                            ),
                        },
                    )
                elif not has_price and ctx["no_price_whitelist"]:
                    findings.append(
                        {
                            "key": f"whitelist_{tid}",
                            "label": f"无价白名单 ({gname}/{tname})",
                            "value": "该分类允许无价，采集不应因价格跳过",
                        },
                    )
            else:
                ctx["would_skip_uncategorized"] = None
                findings.append(
                    {
                        "key": f"tag_unknown_{tid}",
                        "label": f"标签 #{tid}",
                        "value": tname or "（无法在 commodity/tags 树中定位分组）",
                    },
                )
            tag_contexts.append(ctx)

    primary = blockers[0]["detail_zh"] if blockers else (
        "commodity/view 数据正常，未发现采集硬性阻塞；若仍未入库，可能采集列表尚未翻到该商品"
    )

    thumb: str | None = None
    urls = scrape_fields.get("commodity_image_urls") or image_urls
    if urls and str(urls[0]).strip():
        thumb = str(urls[0]).strip()

    return {
        "blockers": blockers,
        "findings": findings,
        "tag_contexts": tag_contexts,
        "primary_blocker_zh": primary,
        "scrape_fields_summary": {
            "commodity_title": str(scrape_fields.get("commodity_title") or "").strip() or None,
            "price_cny": scrape_fields.get("price_cny"),
            "image_count": len(scrape_fields.get("commodity_image_urls") or image_urls),
        },
        "thumbnail_url": thumb,
        "view_ready": detail_response_ready(view_resp),
    }


def probe_wecatalog_goods_live(
    album_id: str,
    goods_id: str,
    *,
    trans_lang: str = "zh",
) -> dict[str, Any]:
    """浏览器拉取 commodity/view + 标签树，分析采集阻塞原因。"""
    from playwright.sync_api import sync_playwright

    seed = goods_page_url(album_id, goods_id)
    out: dict[str, Any] = {
        "probe_ok": False,
        "probe_error": None,
        "album_id": album_id,
        "goods_id": goods_id,
        "goods_url": seed,
        "view_url": commodity_view_url(album_id, goods_id),
    }

    scrape_progress = _scrape_list_progress(album_id)
    if scrape_progress:
        out["scrape_progress"] = scrape_progress

    p = sync_playwright().start()
    browser = _launch_browser(p, headless=True)
    try:
        page = browser.new_page()
        page.goto(seed, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(1_000)

        view_resp = page.evaluate(
            FETCH_VIEW_JS,
            {"targetAlbumId": album_id, "itemId": goods_id},
        )
        if not isinstance(view_resp, dict):
            view_resp = {"error": str(view_resp)}

        api_tags = tags_api_url(album_id=album_id, trans_lang=trans_lang)
        tags_raw = page.evaluate(FETCH_TAGS_JS, api_tags)
        groups: list[dict[str, Any]] = []
        if isinstance(tags_raw, dict) and tags_raw.get("errcode") in (0, None):
            result = tags_raw.get("result")
            if isinstance(result, dict):
                groups = build_group_tree(result)

        analysis = analyze_live_scrape_blockers(
            album_id=album_id,
            goods_id=goods_id,
            groups=groups,
            view_resp=view_resp,
            scrape_progress=scrape_progress,
        )
        out.update(
            {
                "probe_ok": True,
                "view_errcode": popups_errcode(view_resp),
                "view_errmsg": popups_errmsg(view_resp),
                "groups_loaded": len(groups),
                **analysis,
            },
        )
        return out
    except Exception as e:
        _log.warning("live probe failed: %s", e)
        out["probe_error"] = str(e)[:500]
        return out
    finally:
        browser.close()
        p.stop()
