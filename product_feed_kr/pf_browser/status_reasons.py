"""商品库浏览：处理/上传阻塞原因（中文 tooltip 文案）。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from product_feed_kr.llm_spec_fields import listing_llm_from_row
from product_feed_kr.seven17_config import bool_env, getenv
from product_feed_kr.seven17_upload import _upload_skip_reason


def row_as_upload_record(row: dict[str, Any]) -> dict[str, Any]:
    """扁平行 → 与上架/LLM 模块一致的 record 形状。"""
    rec = dict(row)
    ll = listing_llm_from_row(row)
    if ll is not None:
        rec["listing_llm"] = ll
    return rec


def _duplicate_reason_zh(sibling: dict[str, Any] | None) -> str:
    if sibling is None:
        return "首图与其他商品重复，同一货源只处理一条（应保留 id 最小的一条可处理）"
    sid = sibling.get("id")
    gnum = str(sibling.get("commodity_goods_num") or "").strip() or "—"
    if sibling.get("can_process"):
        return f"首图与其他商品重复，请处理 #{sid}（货号 {gnum}）"
    return f"首图与其他商品重复（应处理 #{sid} 货号 {gnum}），同一货源只处理一条"


def process_block_reason_zh(
    row: dict[str, Any],
    *,
    duplicate_sibling: dict[str, Any] | None = None,
) -> str | None:
    """``can_process=False`` 时的说明；可处理时返回 None。"""
    if row.get("can_process") is not False:
        return None
    h = row.get("first_image_hash")
    if isinstance(h, str) and h.strip():
        return _duplicate_reason_zh(duplicate_sibling)
    return "已标记为不可处理（缺少首图 hash）"


def _llm_upload_block_detail(rec: dict[str, Any]) -> str:
    from product_feed_kr.listing_llm_enrich import (
        listing_llm_attempts_exhausted,
        listing_llm_is_gave_up,
        listing_llm_name_ko_usable,
        record_is_no_price_allowed_by_map_category,
    )

    if listing_llm_attempts_exhausted(rec):
        return "LLM 处理次数已达上限"
    if listing_llm_is_gave_up(rec):
        return "LLM 已放弃（次数用尽或永久跳过）"
    ll = rec.get("listing_llm")
    if not isinstance(ll, dict):
        return "尚无 LLM 结果"
    if not listing_llm_name_ko_usable(ll):
        return "缺少韩文商品名 name_ko"
    if not str(ll.get("desc_ko") or "").strip():
        return "缺少韩文描述 desc_ko"
    if not bool(rec.get("can_upload")) and not record_is_no_price_allowed_by_map_category(rec):
        return "缺少有效韩元价，且当前分类不在无价白名单"
    return "LLM 结果未达上架条件"


def upload_block_reason_zh(rec: dict[str, Any], code: str) -> str:
    """上传阻塞原因码 → 中文说明。"""
    if code == "duplicate_item":
        return "重复商品（can_process=0），LLM/上传均跳过"
    if code == "no_detail":
        return "缺少商品标题或详情"
    if code == "llm_not_processed":
        return "尚未 LLM 处理，请运行 02_LLM补全上架信息"
    if code == "llm_not_uploadable":
        return _llm_upload_block_detail(rec)
    if code == "no_category":
        return "无法解析 seven17 分类 ca_id，请补全 map 映射"
    if code == "no_images":
        return "缺少商品图片 URL"
    if code == "already_uploaded":
        return "已上传"
    return f"不可上传（{code}）"


def upload_status_for_row(row: dict[str, Any]) -> tuple[bool, str | None]:
    """
    返回 (upload_eligible, upload_block_reason_zh)。
    已上传时两者均为 False / None（由前端单独展示「已上传」）。
    """
    if row.get("uploaded_to_platform"):
        return False, None
    llm_on = bool_env("OPENAI_ENRICH_LISTING", True)
    default_price = getenv("SEVEN17_DEFAULT_PRICE", "0") or "0"
    rec = row_as_upload_record(row)
    code = _upload_skip_reason(
        rec,
        skip_uploaded=True,
        llm_on=llm_on,
        default_price=default_price,
    )
    if code is None:
        return True, None
    return False, upload_block_reason_zh(rec, code)


def _batch_duplicate_siblings(
    conn: Any,
    items: list[dict[str, Any]],
) -> dict[tuple[int, str], dict[str, Any]]:
    """(item_id, album_id) → 同首图 hash 的另一条记录（优先 id 更小者）。"""
    by_album_hash: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    hashes_by_album: dict[str, set[str]] = defaultdict(set)
    for it in items:
        if it.get("can_process") is not False:
            continue
        h = it.get("first_image_hash")
        aid = str(it.get("album_id") or "").strip()
        if not aid or not isinstance(h, str) or not h.strip():
            continue
        hashes_by_album[aid].add(h.strip())

    for aid, hashes in hashes_by_album.items():
        if not hashes:
            continue
        placeholders = ",".join("?" * len(hashes))
        cur = conn.execute(
            f"""
            SELECT id, album_id, goods_id, tag_id, first_image_hash, commodity_goods_num,
                   can_process
            FROM pf_store_item
            WHERE album_id = ? AND first_image_hash IN ({placeholders})
            ORDER BY can_process DESC, id ASC
            """,
            (aid, *sorted(hashes)),
        )
        for row in cur.fetchall():
            rd = dict(row)
            h = str(rd.get("first_image_hash") or "").strip()
            if h:
                by_album_hash[(aid, h)].append(rd)

    out: dict[tuple[int, str], dict[str, Any]] = {}
    for it in items:
        if it.get("can_process") is not False:
            continue
        iid = int(it.get("id") or 0)
        aid = str(it.get("album_id") or "").strip()
        h = str(it.get("first_image_hash") or "").strip()
        if not aid or not h:
            continue
        group = by_album_hash.get((aid, h), [])
        others = [x for x in group if int(x.get("id") or 0) != iid]
        sibling = next((x for x in others if x.get("can_process")), None)
        if sibling is None and others:
            sibling = min(others, key=lambda x: int(x.get("id") or 0))
        if sibling is not None:
            out[(iid, aid)] = {
                "id": sibling.get("id"),
                "goods_id": sibling.get("goods_id"),
                "commodity_goods_num": sibling.get("commodity_goods_num"),
                "can_process": bool(sibling.get("can_process")),
            }
    return out


def enrich_status_reasons(conn: Any | None, items: list[dict[str, Any]]) -> None:
    """就地写入 ``process_block_reason`` / ``upload_eligible`` / ``upload_block_reason``。"""
    dup_map: dict[tuple[int, str], dict[str, Any]] = {}
    if conn is not None:
        dup_map = _batch_duplicate_siblings(conn, items)

    for it in items:
        iid = int(it.get("id") or 0)
        aid = str(it.get("album_id") or "").strip()
        sibling = dup_map.get((iid, aid))
        it["process_block_reason"] = process_block_reason_zh(it, duplicate_sibling=sibling)
        eligible, upload_reason = upload_status_for_row(it)
        it["upload_eligible"] = eligible
        it["upload_block_reason"] = upload_reason
