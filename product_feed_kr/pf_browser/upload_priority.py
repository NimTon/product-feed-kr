"""上架优先级设置：独立 JSON 配置，控制 02/03 上架顺序。"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from product_feed_kr._paths import REPO_ROOT
from product_feed_kr.common.pf_time import now_cst8_iso
from product_feed_kr.common.seven17_config import getenv

_log = logging.getLogger(__name__)

UPLOAD_PRIORITY_ENV = "WECATALOG_UPLOAD_PRIORITY_JSON"


def upload_priority_path() -> Path:
    raw = (getenv(UPLOAD_PRIORITY_ENV) or "data/wecatalog_upload_priority.json").strip()
    p = Path(raw).expanduser()
    return p.resolve() if p.is_absolute() else (REPO_ROOT / p).resolve()


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_upload_priority_rows() -> list[list[Any]]:
    raw = _read_json(upload_priority_path())
    if isinstance(raw, dict):
        pairs = raw.get("priority_pairs")
        if isinstance(pairs, list):
            return [list(r) for r in pairs if isinstance(r, (list, tuple)) and len(r) >= 2]
    return []


@lru_cache(maxsize=1)
def _cached_priority_pairs() -> tuple[tuple[str, str], ...]:
    rows = load_upload_priority_rows()
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        g = str(row[0]).strip()
        t = str(row[1]).strip()
        if not g or not t or (g, t) in seen:
            continue
        seen.add((g, t))
        out.append((g, t))
    return tuple(out)


def invalidate_upload_priority_cache() -> None:
    _cached_priority_pairs.cache_clear()


def upload_priority_rank_map() -> dict[tuple[str, str], int]:
    pairs = _cached_priority_pairs()
    if not pairs:
        return {}
    return {pair: i for i, pair in enumerate(pairs)}


def save_upload_priority(pairs: list[list[Any]]) -> dict[str, Any]:
    clean: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for item in pairs:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        g = str(item[0] or "").strip()
        t = str(item[1] or "").strip()
        if not g or not t or (g, t) in seen:
            continue
        seen.add((g, t))
        clean.append([g, t])
    payload = {
        "updated_at": now_cst8_iso(),
        "priority_pairs": clean,
    }
    _write_json(upload_priority_path(), payload)
    invalidate_upload_priority_cache()
    return payload


def _distinct_db_pairs() -> list[dict[str, Any]]:
    from product_feed_kr.db.store_sqlite import connect_sqlite

    conn = connect_sqlite()
    try:
        cur = conn.execute("""
            SELECT wecatalog_group, wecatalog_tag, COUNT(*) AS item_count
            FROM pf_store_item
            WHERE trim(COALESCE(wecatalog_group,'')) <> '' AND trim(COALESCE(wecatalog_tag,'')) <> ''
            GROUP BY wecatalog_group, wecatalog_tag
            ORDER BY wecatalog_group, wecatalog_tag
        """)
        out: list[dict[str, Any]] = []
        for r in cur.fetchall():
            g = str(r["wecatalog_group"] or "").strip()
            t = str(r["wecatalog_tag"] or "").strip()
            if g and t:
                out.append({"group": g, "tag": t, "item_count": r["item_count"]})
        return out
    finally:
        conn.close()


def get_upload_priority_state() -> dict[str, Any]:
    path = upload_priority_path()
    raw = _read_json(path)
    priority_pairs = list(_cached_priority_pairs())
    rank_map = {p: i for i, p in enumerate(priority_pairs)} if priority_pairs else {}

    db_pairs = _distinct_db_pairs()
    ui_pairs: list[dict[str, Any]] = []
    for r in db_pairs:
        key = (r["group"], r["tag"])
        ui_pairs.append({
            "group": r["group"],
            "tag": r["tag"],
            "item_count": r["item_count"],
            "priority_index": rank_map.get(key),
        })

    return {
        "ok": True,
        "path": str(path),
        "updated_at": (raw or {}).get("updated_at") if isinstance(raw, dict) else None,
        "priority_count": len(priority_pairs),
        "priority_pairs": [[g, t] for g, t in priority_pairs],
        "pairs": sorted(ui_pairs, key=lambda x: (str(x["group"]), str(x["tag"]))),
    }


def save_upload_priority_from_body(body: dict[str, Any]) -> dict[str, Any]:
    pairs_raw = body.get("priority_pairs")
    if not isinstance(pairs_raw, list):
        raise ValueError("missing_priority_pairs")

    db_set = {(r["group"], r["tag"]) for r in _distinct_db_pairs()}

    clean: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for item in pairs_raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        g = str(item[0] or "").strip()
        t = str(item[1] or "").strip()
        if not g or not t or (g, t) not in db_set or (g, t) in seen:
            continue
        seen.add((g, t))
        clean.append([g, t])

    payload = save_upload_priority(clean)
    return {
        "ok": True,
        "priority_count": len(clean),
        "updated_at": payload["updated_at"],
        "path": str(upload_priority_path()),
    }
