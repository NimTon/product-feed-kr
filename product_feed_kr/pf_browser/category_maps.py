"""分类映射：微猫分类、韩文分类、二者配对三个 JSON（由 05 商品库浏览维护）。"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from product_feed_kr._paths import PACKAGE_ROOT, REPO_ROOT
from product_feed_kr.common.pf_log import pf_kv
from product_feed_kr.common.pf_time import now_cst8_iso
from product_feed_kr.common.seven17_config import getenv
from product_feed_kr.seven17.seven17_path_ca_map import sync_path_ca_map_from_itemform
from product_feed_kr.wecatalog.wecatalog_fetch_tags import build_group_tree, fetch_tags
from product_feed_kr.wecatalog.wecatalog_tag_mapping import invalidate_mapping_cache

_log = logging.getLogger(__name__)

_LEGACY_MAP_JSON = PACKAGE_ROOT / "wecatalog_tag_category_map.json"  # 一次性迁移源（已废弃）
_DEFAULT_ALBUM_ID = "_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg"

_sync_lock = threading.Lock()
_sync_status: dict[str, Any] = {"running": False, "last": None}


def wecatalog_categories_path() -> Path:
    raw = (getenv("WECATALOG_CATEGORIES_JSON") or "data/wecatalog_categories.json").strip()
    p = Path(raw).expanduser()
    return p.resolve() if p.is_absolute() else (REPO_ROOT / p).resolve()


def seven17_categories_path() -> Path:
    raw = (getenv("SEVEN17_CATEGORIES_JSON") or "data/seven17_categories.json").strip()
    p = Path(raw).expanduser()
    return p.resolve() if p.is_absolute() else (REPO_ROOT / p).resolve()


def category_pairs_path() -> Path:
    raw = (getenv("WECATALOG_CATEGORY_PAIRS_JSON") or "data/wecatalog_category_pairs.json").strip()
    p = Path(raw).expanduser()
    return p.resolve() if p.is_absolute() else (REPO_ROOT / p).resolve()


def default_album_id() -> str:
    raw = (getenv("WECATALOG_ALBUM_ID") or _DEFAULT_ALBUM_ID).strip()
    return raw or _DEFAULT_ALBUM_ID


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _slim_wecatalog_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        tags_out: list[dict[str, Any]] = []
        for t in g.get("tags") or []:
            if not isinstance(t, dict):
                continue
            tname = str(t.get("tagName") or "").strip()
            if not tname:
                continue
            try:
                tid = int(t["tagId"])
            except (TypeError, ValueError, KeyError):
                tid = None
            tags_out.append({"tagId": tid, "tagName": tname})
        out.append(
            {
                "groupId": g.get("groupId"),
                "groupName": str(g.get("groupName") or "").strip(),
                "tagCount": g.get("tagCount"),
                "tags": tags_out,
            },
        )
    return out


def _path_label_to_segments(label: str) -> list[str]:
    return [p.strip() for p in str(label or "").split(">") if p.strip()]


def is_wecatalog_root_tag(group: str, tag: str, meta: dict[str, Any] | None = None) -> bool:
    """微猫分组锚点（group==tag 或 anchor_only）不参与配对。"""
    if meta and meta.get("anchor_only"):
        return True
    return str(group or "").strip() == str(tag or "").strip()


def is_seven17_root_path(path: list[str] | None) -> bool:
    """韩文一级分类（路径仅一段）不参与配对。"""
    if not isinstance(path, list):
        return True
    segs = [str(x).strip() for x in path if str(x).strip()]
    return len(segs) <= 1


def filter_pairable_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """去掉微猫 root 标签与韩文 root 路径的配对行。"""
    out: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        if not isinstance(row[2], list):
            continue
        meta = row[3] if len(row) > 3 and isinstance(row[3], dict) else {}
        if is_wecatalog_root_tag(str(row[0]), str(row[1]), meta):
            continue
        if is_seven17_root_path(row[2]):
            continue
        meta = dict(meta)
        meta.pop("anchor_only", None)
        item: list[Any] = [row[0], row[1], row[2]]
        if meta:
            item.append(meta)
        out.append(item)
    return out


def _categories_from_path_ca_map(path_to_ca_id: dict[str, str]) -> list[dict[str, Any]]:
    cats: list[dict[str, Any]] = []
    for label, ca_id in sorted(path_to_ca_id.items()):
        lab = str(label).strip()
        if not lab:
            continue
        cats.append(
            {
                "label": lab,
                "path": _path_label_to_segments(lab),
                "ca_id": str(ca_id).strip(),
            },
        )
    return cats


def _rows_from_pairs_payload(data: Any) -> list[list[Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, list)]
    if isinstance(data, dict):
        raw = data.get("rows")
        if isinstance(raw, list):
            return [r for r in raw if isinstance(r, list)]
    return []


def migrate_legacy_map_if_needed() -> bool:
    """若配对 JSON 不存在，从旧 ``wecatalog_tag_category_map.json`` 迁移。"""
    dest = category_pairs_path()
    if dest.is_file():
        return False
    legacy = _LEGACY_MAP_JSON
    if not legacy.is_file():
        return False
    rows = _rows_from_pairs_payload(_read_json(legacy))
    if not rows:
        return False
    save_category_pairs(rows, merge_tag_ids=False)
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "category_maps.migrated"),
                ("from", str(legacy)),
                ("to", str(dest)),
                ("rows", len(rows)),
            ],
            zh="已从旧版 wecatalog_tag_category_map.json 迁移配对数据",
        ),
    )
    return True


def load_category_pairs_rows() -> list[list[Any]]:
    migrate_legacy_map_if_needed()
    return _rows_from_pairs_payload(_read_json(category_pairs_path()))


def save_category_pairs(rows: list[list[Any]], *, merge_tag_ids: bool = True) -> dict[str, Any]:
    """写入配对 JSON 并刷新 ``wecatalog_tag_mapping`` 缓存。"""
    if merge_tag_ids:
        rows = merge_tag_ids_into_rows(rows)
    rows = filter_pairable_rows(rows)
    payload = {
        "updated_at": now_cst8_iso(),
        "rows": rows,
    }
    _write_json(category_pairs_path(), payload)
    invalidate_mapping_cache()
    return payload


def merge_tag_ids_into_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """用 ``wecatalog_categories.json`` 的 tagId 补全 meta.tag_id。"""
    wc = _read_json(wecatalog_categories_path())
    if not isinstance(wc, dict):
        return rows
    groups = wc.get("groups")
    if not isinstance(groups, list):
        return rows
    api_ids: dict[tuple[str, str], int] = {}
    for g in groups:
        if not isinstance(g, dict):
            continue
        gname = str(g.get("groupName") or "").strip()
        if not gname:
            continue
        for t in g.get("tags") or []:
            if not isinstance(t, dict):
                continue
            tname = str(t.get("tagName") or "").strip()
            if not tname:
                continue
            try:
                api_ids[(gname, tname)] = int(t["tagId"])
            except (TypeError, ValueError, KeyError):
                continue

    out: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        g = str(row[0])
        t = str(row[1])
        path = row[2]
        meta: dict[str, Any] = {}
        if len(row) > 3 and isinstance(row[3], dict):
            meta = dict(row[3])
        tid = api_ids.get((g, t))
        if tid is not None:
            meta["tag_id"] = tid
        item: list[Any] = [g, t, path]
        if meta:
            item.append(meta)
        out.append(item)
    return out


def sync_wecatalog_categories(
    *,
    album_id: str | None = None,
    trans_lang: str = "zh",
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    lg = logger or _log
    aid = (album_id or default_album_id()).strip()
    raw, seed_used = fetch_tags(aid, trans_lang=trans_lang, seed_url=None)
    result = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError(f"commodity/tags 无 result: {raw!r}"[:400])
    groups = build_group_tree(result)
    payload = {
        "updated_at": now_cst8_iso(),
        "album_id": aid,
        "trans_lang": trans_lang,
        "browser_seed_url": seed_used,
        "groups": _slim_wecatalog_groups(groups),
    }
    _write_json(wecatalog_categories_path(), payload)

    rows = load_category_pairs_rows()
    if rows:
        save_category_pairs(rows, merge_tag_ids=True)

    lg.info(
        "%s",
        pf_kv(
            [
                ("event", "category_maps.wecatalog_synced"),
                ("path", str(wecatalog_categories_path())),
                ("groups", len(payload["groups"])),
                ("album_id", aid),
            ],
            zh="已同步微猫分类 JSON",
        ),
    )
    return payload


def sync_seven17_categories(*, logger: logging.Logger | None = None) -> dict[str, Any]:
    lg = logger or _log
    n = sync_path_ca_map_from_itemform(logger=lg)
    from product_feed_kr.seven17.seven17_path_ca_map import path_ca_map_file

    raw = _read_json(path_ca_map_file())
    path_to_ca_id: dict[str, str] = {}
    if isinstance(raw, dict) and isinstance(raw.get("path_to_ca_id"), dict):
        path_to_ca_id = {str(k): str(v) for k, v in raw["path_to_ca_id"].items()}
    categories = _categories_from_path_ca_map(path_to_ca_id)
    payload = {
        "updated_at": now_cst8_iso(),
        "source": str(path_ca_map_file()),
        "entry_count": len(categories),
        "categories": categories,
    }
    _write_json(seven17_categories_path(), payload)
    lg.info(
        "%s",
        pf_kv(
            [
                ("event", "category_maps.seven17_synced"),
                ("path", str(seven17_categories_path())),
                ("entries", len(categories)),
                ("path_ca_sync", n),
            ],
            zh="已同步韩文分类 JSON",
        ),
    )
    return payload


def sync_all_category_maps(
    *,
    album_id: str | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    lg = logger or _log
    migrate_legacy_map_if_needed()
    result: dict[str, Any] = {"ok": True, "updated_at": now_cst8_iso()}
    errors: list[str] = []
    try:
        result["wecatalog"] = sync_wecatalog_categories(album_id=album_id, logger=lg)
    except Exception as e:
        errors.append(f"wecatalog: {e}")
        lg.warning(
            "%s",
            pf_kv([("event", "category_maps.wecatalog_fail"), ("err", str(e)[:300])], zh="微猫分类同步失败"),
        )
    try:
        result["seven17"] = sync_seven17_categories(logger=lg)
    except Exception as e:
        errors.append(f"seven17: {e}")
        lg.warning(
            "%s",
            pf_kv([("event", "category_maps.seven17_fail"), ("err", str(e)[:300])], zh="韩文分类同步失败"),
        )
    if errors:
        result["ok"] = False
        result["errors"] = errors
    return result


def get_category_maps_state() -> dict[str, Any]:
    wc = _read_json(wecatalog_categories_path())
    s17 = _read_json(seven17_categories_path())
    pairs = _read_json(category_pairs_path())
    rows = filter_pairable_rows(_rows_from_pairs_payload(pairs))
    wc_groups = wc.get("groups") if isinstance(wc, dict) else []
    s17_cats = s17.get("categories") if isinstance(s17, dict) else []
    return {
        "paths": {
            "wecatalog": str(wecatalog_categories_path()),
            "seven17": str(seven17_categories_path()),
            "pairs": str(category_pairs_path()),
        },
        "wecatalog": {
            "updated_at": (wc or {}).get("updated_at") if isinstance(wc, dict) else None,
            "album_id": (wc or {}).get("album_id") if isinstance(wc, dict) else None,
            "group_count": len(wc_groups) if isinstance(wc_groups, list) else 0,
            "tag_count": sum(len(g.get("tags") or []) for g in wc_groups if isinstance(g, dict))
            if isinstance(wc_groups, list)
            else 0,
            "groups": wc_groups if isinstance(wc_groups, list) else [],
        },
        "seven17": {
            "updated_at": (s17 or {}).get("updated_at") if isinstance(s17, dict) else None,
            "category_count": len(s17_cats) if isinstance(s17_cats, list) else 0,
            "categories": s17_cats if isinstance(s17_cats, list) else [],
        },
        "pairs": {
            "updated_at": (pairs or {}).get("updated_at") if isinstance(pairs, dict) else None,
            "row_count": len(rows),
            "rows": rows,
        },
        "sync_status": dict(_sync_status),
    }


def apply_pair_updates(updates: list[dict[str, Any]]) -> dict[str, Any]:
    """
    保存前端提交的配对。每项::

        {"group": "...", "tag": "...", "path": ["seg", ...] | null, "anchor_only": bool}
    """
    existing_rows = load_category_pairs_rows()
    by_key: dict[tuple[str, str], list[Any]] = {}
    for row in existing_rows:
        if len(row) < 3:
            continue
        by_key[(str(row[0]), str(row[1]))] = list(row)

    for upd in updates:
        if not isinstance(upd, dict):
            continue
        g = str(upd.get("group") or "").strip()
        t = str(upd.get("tag") or "").strip()
        if not g or not t:
            continue
        path_raw = upd.get("path")
        if path_raw is None:
            by_key.pop((g, t), None)
            continue
        if not isinstance(path_raw, list):
            continue
        segs = [str(x).strip() for x in path_raw if str(x).strip()]
        if not segs:
            by_key.pop((g, t), None)
            continue
        if is_wecatalog_root_tag(g, t) or is_seven17_root_path(segs):
            by_key.pop((g, t), None)
            continue
        meta: dict[str, Any] = {}
        old = by_key.get((g, t))
        if old and len(old) > 3 and isinstance(old[3], dict):
            meta = dict(old[3])
        meta.pop("anchor_only", None)
        if upd.get("tag_id") is not None:
            try:
                meta["tag_id"] = int(upd["tag_id"])
            except (TypeError, ValueError):
                pass
        row: list[Any] = [g, t, segs]
        if meta:
            row.append(meta)
        by_key[(g, t)] = row

    rows = filter_pairable_rows([by_key[k] for k in sorted(by_key.keys())])
    payload = save_category_pairs(rows, merge_tag_ids=True)
    return {"ok": True, "row_count": len(rows), "updated_at": payload["updated_at"]}


def pair_key(group: str, tag: str) -> str:
    return f"{group}\x00{tag}"


def path_from_category_entry(entry: dict[str, Any]) -> list[str]:
    path = entry.get("path")
    if isinstance(path, list) and path:
        return [str(x).strip() for x in path if str(x).strip()]
    label = str(entry.get("label") or "").strip()
    return _path_label_to_segments(label)


def rows_to_pair_lookup(rows: list[list[Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if len(row) < 3 or not isinstance(row[2], list):
            continue
        g, t = str(row[0]), str(row[1])
        meta = row[3] if len(row) > 3 and isinstance(row[3], dict) else {}
        out[pair_key(g, t)] = {
            "group": g,
            "tag": t,
            "path": list(row[2]),
            "anchor_only": bool(meta.get("anchor_only")),
            "tag_id": meta.get("tag_id"),
        }
    return out


def start_background_sync(*, album_id: str | None = None) -> None:
    """Flask 启动时在后台同步分类（不阻塞 HTTP）。"""

    def _run() -> None:
        with _sync_lock:
            if _sync_status.get("running"):
                return
            _sync_status["running"] = True
        try:
            result = sync_all_category_maps(album_id=album_id)
            _sync_status["last"] = result
        finally:
            _sync_status["running"] = False

    threading.Thread(target=_run, name="category-maps-sync", daemon=True).start()
