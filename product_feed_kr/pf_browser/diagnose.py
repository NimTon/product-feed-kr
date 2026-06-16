"""按微猫商品 URL / goods_id 诊断为何未上架（采集 → 处理 → 上传）。"""

from __future__ import annotations

import re
from typing import Any

from product_feed_kr.db.store_sqlite import connect_sqlite, sqlite_db_path
from product_feed_kr.pf_browser.queries import _row_to_item, _select_cols
from product_feed_kr.pf_browser.status_reasons import enrich_status_reasons
from product_feed_kr.wecatalog.wecatalog_scrape_store import goods_page_url
_GOODS_URL_RE = re.compile(
    r"wecatalog\.cn/weshop/goods/([^/?#]+)/([^/?#]+)",
    re.I,
)
_GOODS_ID_RE = re.compile(r"^[_\-A-Za-z0-9]{16,}$")

_SCRAPE_SKIP_REASON_ZH: dict[str, str] = {
    "list_incomplete": "列表项缺少标题或图片，采集时已跳过（记入 pf_scrape_skip）",
    "list_no_price": "列表项无有效价格，且 (分组, 标签) 不在无价白名单，采集时已跳过",
    "popups_invalid": "popUpsInfoV2 返回商品已失效，采集时已永久跳过",
}


def scrape_skip_reason_zh(reason: str | None) -> str:
    code = str(reason or "").strip() or "unknown"
    return _SCRAPE_SKIP_REASON_ZH.get(code, f"采集跳过（{code}）")


def parse_wecatalog_goods_ref(text: str) -> tuple[str | None, str]:
    """
    从微猫商品 URL 或裸 ``goods_id`` 解析标识。

    返回 ``(album_id | None, goods_id)``；仅 goods_id 时 album_id 为 None。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("请输入微猫商品 URL 或 goods_id")

    m = _GOODS_URL_RE.search(raw)
    if m:
        album_id = m.group(1).strip()
        goods_id = m.group(2).strip()
        if album_id and goods_id:
            return album_id, goods_id

    if _GOODS_ID_RE.match(raw):
        return None, raw

    raise ValueError(
        "无法识别：请粘贴完整微猫商品链接（…/weshop/goods/{album_id}/{goods_id}）或 goods_id",
    )


def _load_scrape_skip(
    conn: Any,
    *,
    album_id: str | None,
    goods_id: str,
) -> dict[str, Any] | None:
    if album_id:
        cur = conn.execute(
            """
            SELECT album_id, goods_id, reason, errcode, errmsg, goods_url,
                   first_seen_at, last_seen_at, hit_count
            FROM pf_scrape_skip
            WHERE album_id = ? AND goods_id = ?
            LIMIT 1
            """,
            (album_id, goods_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    cur = conn.execute(
        """
        SELECT album_id, goods_id, reason, errcode, errmsg, goods_url,
               first_seen_at, last_seen_at, hit_count
        FROM pf_scrape_skip
        WHERE goods_id = ?
        ORDER BY last_seen_at DESC, id DESC
        LIMIT 1
        """,
        (goods_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _load_store_items(
    conn: Any,
    *,
    album_id: str | None,
    goods_id: str,
) -> list[dict[str, Any]]:
    select_cols = _select_cols(conn, detail=True)
    if album_id:
        cur = conn.execute(
            f"""
            SELECT {select_cols}
            FROM pf_store_item
            WHERE album_id = ? AND goods_id = ?
            ORDER BY id ASC
            """,
            (album_id, goods_id),
        )
    else:
        cur = conn.execute(
            f"""
            SELECT {select_cols}
            FROM pf_store_item
            WHERE goods_id = ?
            ORDER BY id ASC
            """,
            (goods_id,),
        )
    items = [_row_to_item(r) for r in cur.fetchall()]
    enrich_status_reasons(conn, items)
    return items


def _not_scraped_conclusion_zh(
    *,
    album_id: str | None,
    goods_id: str,
) -> str:
    parts = [
        "该商品尚未写入商品库（pf_store_item）。",
        "常见原因：① 所在微猫分类未在 data/wecatalog_category_pairs.json 中配对（未配对标签不会进入采集队列）；",
        "② 该标签列表尚未爬完；③ 尚未对该店铺执行采集。",
    ]
    if album_id:
        parts.append(f"可确认店铺 album_id={album_id} 是否已运行 01_采集微猫店铺。")
    else:
        parts.append(f"建议在商品链接中带上 album_id，或先在库内搜索 goods_id={goods_id}。")
    return "".join(parts)


def _build_steps(
    *,
    scrape_ok: bool,
    scrape_detail: str,
    items: list[dict[str, Any]],
    scrape_skip: dict[str, Any] | None,
    live: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    if scrape_ok:
        steps.append(
            {
                "key": "scrape",
                "status": "ok",
                "title": "采集入库",
                "detail_zh": f"已在库内，共 {len(items)} 条记录（不同 tag_id 各一条）",
            },
        )
    elif scrape_skip:
        reason = str(scrape_skip.get("reason") or "").strip()
        detail = scrape_skip_reason_zh(reason)
        if live and live.get("primary_blocker_zh"):
            detail = f"{detail}。实时：{live['primary_blocker_zh']}"
        steps.append(
            {
                "key": "scrape",
                "status": "fail",
                "title": "采集入库",
                "detail_zh": detail,
                "reason_code": reason or None,
                "hit_count": scrape_skip.get("hit_count"),
                "first_seen_at": scrape_skip.get("first_seen_at"),
                "last_seen_at": scrape_skip.get("last_seen_at"),
            },
        )
    else:
        detail = "未入库，且不在 pf_scrape_skip 跳过表"
        if live and live.get("probe_ok") and live.get("primary_blocker_zh"):
            detail = live["primary_blocker_zh"]
        elif live and live.get("probe_error"):
            detail = f"{detail}（实时拉取失败：{live['probe_error']}）"
        steps.append(
            {
                "key": "scrape",
                "status": "fail",
                "title": "采集入库",
                "detail_zh": detail,
            },
        )

    if not scrape_ok:
        steps.append(
            {
                "key": "process",
                "status": "skip",
                "title": "LLM 处理",
                "detail_zh": "未到该阶段（须先成功采集入库）",
            },
        )
        steps.append(
            {
                "key": "upload",
                "status": "skip",
                "title": "上架",
                "detail_zh": "未到该阶段（须先成功采集入库）",
            },
        )
        return steps

    # 以「最阻碍上架」的一条为准（优先未上传且不可上传）
    focus = items[0]
    for it in items:
        if it.get("uploaded_to_platform"):
            continue
        if not it.get("upload_eligible"):
            focus = it
            break
        focus = it

    if focus.get("can_process") is False:
        proc_status = "fail"
        proc_detail = str(focus.get("process_block_reason") or "不可处理（can_process=0）")
    elif focus.get("llm_processed"):
        proc_status = "ok"
        proc_detail = "已完成 LLM 处理"
    elif focus.get("can_process"):
        proc_status = "warn"
        proc_detail = "可处理但尚未 LLM，请运行 02_LLM补全上架信息"
    else:
        proc_status = "fail"
        proc_detail = str(focus.get("process_block_reason") or "不可处理")

    steps.append(
        {
            "key": "process",
            "status": proc_status,
            "title": "LLM 处理",
            "detail_zh": proc_detail,
            "item_id": focus.get("id"),
        },
    )

    if focus.get("uploaded_to_platform"):
        steps.append(
            {
                "key": "upload",
                "status": "ok",
                "title": "上架",
                "detail_zh": "已上传至平台",
                "item_id": focus.get("id"),
                "seven17_uploaded_at": focus.get("seven17_uploaded_at"),
            },
        )
    elif focus.get("upload_eligible"):
        steps.append(
            {
                "key": "upload",
                "status": "warn",
                "title": "上架",
                "detail_zh": "满足上架条件，但尚未上传；请运行 03 或检查上架脚本是否已执行",
                "item_id": focus.get("id"),
            },
        )
    else:
        steps.append(
            {
                "key": "upload",
                "status": "fail",
                "title": "上架",
                "detail_zh": str(focus.get("upload_block_reason") or "当前不满足上架条件"),
                "item_id": focus.get("id"),
            },
        )

    return steps


def diagnose_wecatalog_goods(text: str, *, live_probe: bool = True) -> dict[str, Any]:
    """诊断单条微猫商品为何未上架；返回 JSON 可序列化 dict。"""
    album_id, goods_id = parse_wecatalog_goods_ref(text)

    conn = connect_sqlite()
    try:
        scrape_skip = _load_scrape_skip(conn, album_id=album_id, goods_id=goods_id)
        if scrape_skip and not album_id:
            album_id = str(scrape_skip.get("album_id") or "").strip() or None

        items = _load_store_items(conn, album_id=album_id, goods_id=goods_id)
        if items and not album_id:
            album_id = str(items[0].get("album_id") or "").strip() or None

        resolved_album = album_id or (
            str(scrape_skip.get("album_id") or "").strip() if scrape_skip else ""
        ) or None
        goods_url = goods_page_url(resolved_album, goods_id) if resolved_album else None
        if scrape_skip and scrape_skip.get("goods_url"):
            goods_url = str(scrape_skip["goods_url"]).strip() or goods_url

        scrape_ok = bool(items)

        live: dict[str, Any] | None = None
        need_live = live_probe and resolved_album and (not scrape_ok or scrape_skip is not None)
        if need_live:
            from product_feed_kr.pf_browser.diagnose_probe import probe_wecatalog_goods_live

            live = probe_wecatalog_goods_live(resolved_album, goods_id)

        steps = _build_steps(
            scrape_ok=scrape_ok,
            scrape_detail="",
            items=items,
            scrape_skip=scrape_skip,
            live=live,
        )

        uploaded_any = any(it.get("uploaded_to_platform") for it in items)
        upload_eligible_any = any(it.get("upload_eligible") for it in items)

        if uploaded_any:
            pipeline_status = "uploaded"
            conclusion_zh = "该商品至少有一条记录已上传至平台。"
        elif scrape_ok and upload_eligible_any:
            pipeline_status = "uploadable"
            conclusion_zh = "已采集且满足上架条件，但尚未上传；请运行上架流程（03）。"
        elif scrape_ok:
            pipeline_status = "in_db_blocked"
            block = next(
                (
                    it.get("upload_block_reason") or it.get("process_block_reason")
                    for it in items
                    if not it.get("uploaded_to_platform")
                ),
                "当前不满足上架条件",
            )
            conclusion_zh = f"已采集入库，但未上架：{block}"
        elif scrape_skip:
            pipeline_status = "scrape_skipped"
            conclusion_zh = scrape_skip_reason_zh(str(scrape_skip.get("reason") or ""))
            if live and live.get("primary_blocker_zh"):
                conclusion_zh = f"{conclusion_zh}。实时复核：{live['primary_blocker_zh']}"
        else:
            pipeline_status = "not_in_db"
            if live and live.get("probe_ok") and live.get("primary_blocker_zh"):
                conclusion_zh = live["primary_blocker_zh"]
            else:
                conclusion_zh = _not_scraped_conclusion_zh(album_id=resolved_album, goods_id=goods_id)
            if live and not live.get("probe_ok") and live.get("probe_error"):
                conclusion_zh += f"（实时拉取失败：{live['probe_error']}）"

        store_summary = [
            {
                "id": it.get("id"),
                "tag_id": it.get("tag_id"),
                "wecatalog_group": it.get("wecatalog_group"),
                "wecatalog_tag": it.get("wecatalog_tag"),
                "commodity_title_short": it.get("commodity_title_short"),
                "thumbnail_url": (it.get("image_urls") or [None])[0],
                "can_process": it.get("can_process"),
                "can_upload": it.get("can_upload"),
                "uploaded_to_platform": it.get("uploaded_to_platform"),
                "upload_eligible": it.get("upload_eligible"),
                "process_block_reason": it.get("process_block_reason"),
                "upload_block_reason": it.get("upload_block_reason"),
                "llm_processed_at": it.get("llm_processed_at"),
                "seven17_uploaded_at": it.get("seven17_uploaded_at"),
            }
            for it in items
        ]

        thumbnail_url: str | None = None
        thumbnail_title: str | None = None
        for it in items:
            urls = it.get("image_urls") or []
            if urls and str(urls[0]).strip():
                thumbnail_url = str(urls[0]).strip()
                thumbnail_title = str(it.get("commodity_title_short") or it.get("commodity_title") or "").strip() or None
                break
        if not thumbnail_url and live and live.get("thumbnail_url"):
            thumbnail_url = str(live["thumbnail_url"]).strip()
            summary = live.get("scrape_fields_summary") or {}
            thumbnail_title = str(summary.get("commodity_title") or "").strip() or None

        live_public = None
        if live:
            live_public = {
                k: live.get(k)
                for k in (
                    "probe_ok",
                    "probe_error",
                    "view_url",
                    "view_errcode",
                    "view_errmsg",
                    "view_ready",
                    "groups_loaded",
                    "map_rebuild_rows",
                    "map_rebuild_error",
                    "scrape_progress",
                    "findings",
                    "blockers",
                    "tag_contexts",
                    "scrape_fields_summary",
                    "primary_blocker_zh",
                    "thumbnail_url",
                )
            }

        return {
            "ok": True,
            "input": text.strip(),
            "album_id": resolved_album,
            "goods_id": goods_id,
            "goods_url": goods_url,
            "thumbnail_url": thumbnail_url,
            "thumbnail_title": thumbnail_title,
            "db_path": str(sqlite_db_path()),
            "pipeline_status": pipeline_status,
            "conclusion_zh": conclusion_zh,
            "can_upload": upload_eligible_any,
            "uploaded": uploaded_any,
            "in_store": scrape_ok,
            "scrape_skipped": scrape_skip is not None,
            "live_probe": live_public,
            "steps": steps,
            "scrape_skip": scrape_skip,
            "store_items": store_summary,
        }
    finally:
        conn.close()
