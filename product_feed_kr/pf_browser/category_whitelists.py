"""微猫分类白名单：抓取白名单（独立 JSON，与分类配对无关）与无价白名单聚合 API。"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from product_feed_kr._paths import REPO_ROOT
from product_feed_kr.common.pf_time import now_cst8_iso
from product_feed_kr.common.seven17_config import getenv, reload_seven17_config
from product_feed_kr.pf_browser.category_maps import (
    _read_json,
    iter_pairable_wecatalog_tags,
    load_all_wecatalog_tag_pairs,
    wecatalog_categories_path,
)
from product_feed_kr.seven17.no_price_whitelist import (
    CONFIG_KEY as NO_PRICE_CONFIG_KEY,
    format_selected_pairs,
    load_config_data,
    parse_selected_pairs,
)
from product_feed_kr.wecatalog.wecatalog_tag_mapping import (
    ScrapeTagTarget,
    _resolve_tag_id_for_pair,
    resolve_category_path,
)

_log = logging.getLogger(__name__)

SCRAPE_WHITELIST_ENV = "WECATALOG_SCRAPE_WHITELIST_JSON"
MAX_PARALLEL_ENV = "WECATALOG_SCRAPE_MAX_PARALLEL"


def scrape_whitelist_path() -> Path:
    raw = (getenv(SCRAPE_WHITELIST_ENV) or "data/wecatalog_scrape_whitelist.json").strip()
    p = Path(raw).expanduser()
    return p.resolve() if p.is_absolute() else (REPO_ROOT / p).resolve()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_wecatalog_groups() -> list[dict[str, Any]]:
    wc = _read_json(wecatalog_categories_path())
    groups = wc.get("groups") if isinstance(wc, dict) else None
    return groups if isinstance(groups, list) else []


def _rows_from_scrape_payload(raw: Any) -> list[list[Any]]:
    if isinstance(raw, dict):
        rows = raw.get("scrape_pairs")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, list)]
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, list)]
    return []


def load_scrape_whitelist_rows() -> list[list[Any]]:
    raw = _read_json(scrape_whitelist_path())
    return _rows_from_scrape_payload(raw)


def _raw_scrape_max_parallel() -> int:
    raw = _read_json(scrape_whitelist_path())
    if isinstance(raw, dict) and raw.get("max_parallel") is not None:
        try:
            return max(1, int(raw["max_parallel"]))
        except (TypeError, ValueError):
            pass
    cfg = (getenv(MAX_PARALLEL_ENV) or "1").strip()
    try:
        return max(1, int(cfg))
    except ValueError:
        return 1


def clamp_scrape_max_parallel(parallel: int, scrape_count: int) -> int:
    """最大并行不得超过白名单抓取项数（每项至多一个 worker）。"""
    n = max(1, int(parallel))
    if scrape_count > 0:
        return min(n, int(scrape_count))
    return n


def load_scrape_max_parallel() -> int:
    return clamp_scrape_max_parallel(
        _raw_scrape_max_parallel(),
        len(scrape_whitelist_pairs_ordered()),
    )


def merge_scrape_tag_ids(rows: list[list[Any]]) -> list[list[Any]]:
    api_ids = {(g, t): tid for g, t, tid in iter_pairable_wecatalog_tags()}
    out: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        g = str(row[0]).strip()
        t = str(row[1]).strip()
        if not g or not t:
            continue
        meta: dict[str, Any] = {}
        if len(row) > 2 and isinstance(row[2], dict):
            meta = dict(row[2])
        tid = api_ids.get((g, t))
        if tid is not None:
            meta["tag_id"] = tid
        item: list[Any] = [g, t]
        if meta:
            item.append(meta)
        out.append(item)
    return out


def save_scrape_whitelist(
    pairs: list[list[Any]],
    *,
    max_parallel: int | None = None,
) -> dict[str, Any]:
    rows = merge_scrape_tag_ids(pairs)
    if max_parallel is not None:
        parallel = clamp_scrape_max_parallel(max(1, int(max_parallel)), len(rows))
    else:
        parallel = clamp_scrape_max_parallel(_raw_scrape_max_parallel(), len(rows))
    payload = {
        "updated_at": now_cst8_iso(),
        "max_parallel": parallel,
        "scrape_pairs": rows,
    }
    _write_json(scrape_whitelist_path(), payload)
    invalidate_scrape_whitelist_cache()
    return payload


@lru_cache(maxsize=1)
def _cached_scrape_rows() -> tuple[tuple[str, str, int | None], ...]:
    rows = load_scrape_whitelist_rows()
    valid = set(load_all_wecatalog_tag_pairs())
    out: list[tuple[str, str, int | None]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if len(row) < 2:
            continue
        g = str(row[0]).strip()
        t = str(row[1]).strip()
        if not g or not t or (g, t) not in valid or (g, t) in seen:
            continue
        seen.add((g, t))
        tid: int | None = None
        if len(row) > 2 and isinstance(row[2], dict):
            try:
                tid = int(row[2]["tag_id"])
            except (TypeError, ValueError, KeyError):
                tid = None
        out.append((g, t, tid))
    return tuple(out)


def invalidate_scrape_whitelist_cache() -> None:
    _cached_scrape_rows.cache_clear()


def scrape_whitelist_pairs_ordered() -> tuple[tuple[str, str, int | None], ...]:
    return _cached_scrape_rows()


def build_scrape_tag_targets_from_whitelist(
    groups: list[dict[str, Any]] | None = None,
) -> tuple[ScrapeTagTarget, ...]:
    """按抓取白名单顺序构建爬取目标（与分类配对无关）。"""
    out: list[ScrapeTagTarget] = []
    seen_ids: set[int] = set()
    for g, t, tid_meta in scrape_whitelist_pairs_ordered():
        tid = _resolve_tag_id_for_pair(g, t, meta_tag_id=tid_meta, groups=groups)
        if tid is None or tid in seen_ids:
            continue
        seen_ids.add(tid)
        path = resolve_category_path(g, t)
        shop_path = tuple(path) if path else ()
        out.append(ScrapeTagTarget(g, t, tid, shop_path))
    return tuple(out)


def scrape_whitelist_empty_diagnostic(groups: list[dict[str, Any]] | None = None) -> str:
    path = scrape_whitelist_path()
    pairs = scrape_whitelist_pairs_ordered()
    if not path.is_file() or not pairs:
        return (
            f"抓取白名单为空或文件不存在：{path}\n"
            "请在 05 商品库浏览 →「分类白名单」勾选需要抓取的微猫分类并保存。"
        )
    unresolved: list[str] = []
    for g, t, tid_meta in pairs:
        if _resolve_tag_id_for_pair(g, t, meta_tag_id=tid_meta, groups=groups) is None:
            unresolved.append(f"{g}/{t}")
    if unresolved:
        sample = "、".join(unresolved[:5])
        more = f" 等共 {len(unresolved)} 条" if len(unresolved) > 5 else ""
        return (
            f"抓取白名单 {path} 有 {len(pairs)} 项，但 {len(unresolved)} 项无法解析微猫 tagId。\n"
            f"无法解析：{sample}{more}\n"
            "请先在 05 启动一次（同步微猫分类），或检查分组/标签名是否与店铺一致。"
        )
    return (
        f"抓取白名单 {path} 有 {len(pairs)} 项，"
        "但与 commodity/tags 未能对齐 tagId。请确认 05 已同步微猫分类。"
    )


def build_whitelist_ui_groups(
    *,
    scrape_selected: set[tuple[str, str]],
    no_price_selected: set[tuple[str, str]],
    scrape_order: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    order_index = {pair: i for i, pair in enumerate(scrape_order)}
    by_group: dict[str, list[dict[str, Any]]] = {}
    for g, t, tid in iter_pairable_wecatalog_tags():
        by_group.setdefault(g, []).append(
            {
                "tagName": t,
                "tagId": tid,
                "scrape": (g, t) in scrape_selected,
                "no_price": (g, t) in no_price_selected,
                "scrape_order": order_index.get((g, t)),
            },
        )
    out: list[dict[str, Any]] = []
    for g in sorted(by_group.keys()):
        tags = sorted(by_group[g], key=lambda x: str(x["tagName"]))
        n_scrape = sum(1 for t in tags if t["scrape"])
        n_np = sum(1 for t in tags if t["no_price"])
        out.append(
            {
                "groupName": g,
                "tags": tags,
                "scrape_count": n_scrape,
                "no_price_count": n_np,
                "tag_count": len(tags),
            },
        )
    return out


def get_category_whitelists_state() -> dict[str, Any]:
    groups = load_wecatalog_groups()
    all_pairs = set(load_all_wecatalog_tag_pairs())
    scrape_rows = load_scrape_whitelist_rows()
    scrape_order: list[tuple[str, str]] = []
    scrape_seen: set[tuple[str, str]] = set()
    for row in scrape_rows:
        if len(row) < 2:
            continue
        g = str(row[0]).strip()
        t = str(row[1]).strip()
        key = (g, t)
        if not g or not t or key not in all_pairs or key in scrape_seen:
            continue
        scrape_seen.add(key)
        scrape_order.append(key)

    cfg_path, cfg_data = load_config_data()
    no_price_raw = str(cfg_data.get(NO_PRICE_CONFIG_KEY) or "").strip()
    no_price_selected = parse_selected_pairs(no_price_raw, list(all_pairs))

    ui_groups = build_whitelist_ui_groups(
        scrape_selected=scrape_seen,
        no_price_selected=no_price_selected,
        scrape_order=scrape_order,
    )
    wl_path = scrape_whitelist_path()
    wl_raw = _read_json(wl_path)
    return {
        "ok": True,
        "paths": {
            "wecatalog_categories": str(wecatalog_categories_path()),
            "scrape_whitelist": str(wl_path),
            "no_price_config": str(cfg_path),
        },
        "max_parallel": load_scrape_max_parallel(),
        "scrape_whitelist_updated_at": (wl_raw or {}).get("updated_at")
        if isinstance(wl_raw, dict)
        else None,
        "scrape_count": len(scrape_order),
        "scrape_pairs": [[g, t] for g, t in scrape_order],
        "no_price_config_key": NO_PRICE_CONFIG_KEY,
        "no_price_count": len(no_price_selected),
        "no_price_pairs": [[g, t] for g, t in sorted(no_price_selected)],
        "no_price_raw": no_price_raw,
        "tag_count": len(all_pairs),
        "groups": ui_groups,
    }


def save_category_whitelists(body: dict[str, Any]) -> dict[str, Any]:
    all_pairs = set(load_all_wecatalog_tag_pairs())
    scrape_pairs_raw = body.get("scrape_pairs")
    if not isinstance(scrape_pairs_raw, list):
        raise ValueError("missing_scrape_pairs")
    scrape_clean: list[list[Any]] = []
    scrape_seen: set[tuple[str, str]] = set()
    for item in scrape_pairs_raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        g = str(item[0] or "").strip()
        t = str(item[1] or "").strip()
        key = (g, t)
        if not g or not t or key not in all_pairs or key in scrape_seen:
            continue
        scrape_seen.add(key)
        scrape_clean.append([g, t])

    no_price_raw = body.get("no_price_pairs")
    if not isinstance(no_price_raw, list):
        raise ValueError("missing_no_price_pairs")
    no_price_clean: list[tuple[str, str]] = []
    no_price_seen: set[tuple[str, str]] = set()
    for item in no_price_raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        g = str(item[0] or "").strip()
        t = str(item[1] or "").strip()
        key = (g, t)
        if not g or not t or key not in all_pairs or key in no_price_seen:
            continue
        no_price_seen.add(key)
        no_price_clean.append(key)
    no_price_clean.sort()

    max_parallel = body.get("max_parallel", load_scrape_max_parallel())
    try:
        parallel = clamp_scrape_max_parallel(max(1, int(max_parallel)), len(scrape_clean))
    except (TypeError, ValueError):
        parallel = clamp_scrape_max_parallel(1, len(scrape_clean))

    scrape_payload = save_scrape_whitelist(scrape_clean, max_parallel=parallel)
    cfg_path, cfg_data = load_config_data()
    cfg_data[NO_PRICE_CONFIG_KEY] = format_selected_pairs(no_price_clean)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reload_seven17_config()

    return {
        "ok": True,
        "scrape_count": len(scrape_clean),
        "no_price_count": len(no_price_clean),
        "max_parallel": scrape_payload["max_parallel"],
        "scrape_updated_at": scrape_payload["updated_at"],
        "no_price_raw": cfg_data[NO_PRICE_CONFIG_KEY],
        "paths": {
            "scrape_whitelist": str(scrape_whitelist_path()),
            "no_price_config": str(cfg_path),
        },
    }


if __name__ == "__main__":
    print(load_scrape_max_parallel())
