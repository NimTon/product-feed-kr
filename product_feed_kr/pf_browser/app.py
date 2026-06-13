"""Flask 商品库浏览：查询 ``pf_store_item`` + 单条重跑（爬取/处理/上传）。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from typing import Any

from product_feed_kr.pf_browser.actions import request_item_rerun
from product_feed_kr.pf_browser.category_maps import (
    apply_pair_updates,
    get_category_maps_state,
    save_category_pairs,
    start_background_sync,
    sync_all_category_maps,
)
from product_feed_kr.pf_browser.diagnose import diagnose_wecatalog_goods
from product_feed_kr.pf_browser.queries import get_item, list_albums, list_items
from product_feed_kr.common.seven17_config import getenv, reload_seven17_config
from product_feed_kr.seven17.no_price_whitelist import get_no_price_whitelist_state, save_no_price_whitelist
from product_feed_kr.db.store_sqlite import (
    connect_sqlite,
    sqlite_db_path,
    sqlite_reconcile_can_process_dup_hashes,
)

_log = logging.getLogger(__name__)

_STATIC = Path(__file__).resolve().parent / "static"


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(_STATIC), static_url_path="/static")

    @app.get("/")
    def index() -> object:
        return send_from_directory(_STATIC, "index.html")

    @app.get("/api/health")
    def health() -> object:
        return jsonify({"ok": True, "db_path": str(sqlite_db_path())})

    @app.get("/api/albums")
    def albums() -> object:
        return jsonify({"albums": list_albums()})

    @app.get("/api/items")
    def items() -> object:
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1
        try:
            page_size = int(request.args.get("page_size", "50"))
        except ValueError:
            page_size = 50
        album_id = (request.args.get("album_id") or "").strip() or None
        q = (request.args.get("q") or "").strip() or None
        sort = (request.args.get("sort") or "llm").strip()
        return jsonify(
            list_items(
                page=page,
                page_size=page_size,
                album_id=album_id,
                q=q,
                sort=sort,
            ),
        )

    @app.get("/api/diagnose")
    def diagnose() -> object:
        q = (request.args.get("url") or request.args.get("q") or "").strip()
        if not q:
            return jsonify({"ok": False, "error": "missing_url", "message_zh": "请提供微猫商品 URL 或 goods_id"}), 400
        live_raw = (request.args.get("live") or "1").strip().lower()
        live_probe = live_raw not in ("0", "false", "no", "off")
        try:
            return jsonify(diagnose_wecatalog_goods(q, live_probe=live_probe))
        except ValueError as e:
            return jsonify({"ok": False, "error": "invalid_input", "message_zh": str(e)}), 400

    @app.get("/api/items/<int:item_id>")
    def item_detail(item_id: int) -> object:
        row = get_item(item_id)
        if row is None:
            return jsonify({"error": "not_found"}), 404
        return jsonify(row)

    @app.post("/api/admin/reconcile-can-process")
    def reconcile_can_process() -> object:
        """按首图 hash 对账：每组仅 id 最小者为可处理（修复重复组全部被标 0）。"""
        album_id = (request.args.get("album_id") or "").strip() or None
        conn = connect_sqlite()
        try:
            n = sqlite_reconcile_can_process_dup_hashes(conn, album_id=album_id)
            conn.commit()
        finally:
            conn.close()
        return jsonify({"ok": True, "rows_updated": n, "album_id": album_id})

    @app.post("/api/items/<int:item_id>/rerun")
    def item_rerun(item_id: int) -> object:
        body = request.get_json(silent=True) or {}
        action = (body.get("action") or request.args.get("action") or "").strip()
        if not action:
            return jsonify({"ok": False, "error": "missing_action"}), 400
        result = request_item_rerun(item_id, action)
        if not result.get("ok"):
            err = result.get("error") or "failed"
            code = 404 if err == "not_found" else 400
            return jsonify(result), code
        return jsonify(result)

    @app.get("/api/category-maps")
    def category_maps_get() -> object:
        return jsonify(get_category_maps_state())

    @app.post("/api/category-maps/sync")
    def category_maps_sync() -> object:
        album_id = (request.args.get("album_id") or request.get_json(silent=True) or {}).get("album_id")
        album_id = str(album_id or "").strip() or None
        return jsonify(sync_all_category_maps(album_id=album_id))

    @app.post("/api/category-maps/pairs")
    def category_maps_save_pairs() -> object:
        body = request.get_json(silent=True) or {}
        rows = body.get("rows")
        if isinstance(rows, list):
            try:
                cleaned: list[list[Any]] = []
                for row in rows:
                    if not isinstance(row, list) or len(row) < 3:
                        continue
                    cleaned.append(row)
                payload = save_category_pairs(cleaned, merge_tag_ids=True)
                return jsonify({"ok": True, "row_count": len(cleaned), "updated_at": payload["updated_at"]})
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
        updates = body.get("updates")
        if not isinstance(updates, list):
            return jsonify({"ok": False, "error": "missing_rows_or_updates"}), 400
        try:
            return jsonify(apply_pair_updates(updates))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.get("/api/no-price-whitelist")
    def no_price_whitelist_get() -> object:
        try:
            return jsonify(get_no_price_whitelist_state())
        except Exception as e:
            _log.exception("no_price_whitelist_get failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/no-price-whitelist")
    def no_price_whitelist_save() -> object:
        body = request.get_json(silent=True) or {}
        pairs = body.get("pairs")
        if not isinstance(pairs, list):
            return jsonify({"ok": False, "error": "missing_pairs"}), 400
        try:
            result = save_no_price_whitelist(pairs)
            reload_seven17_config()
            return jsonify(result)
        except Exception as e:
            _log.exception("no_price_whitelist_save failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="product-feed-kr 商品库浏览（只读）")
    ap.add_argument("--host", default=getenv("PF_BROWSER_HOST", "127.0.0.1") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(getenv("PF_BROWSER_PORT", "8765") or "8765"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db = sqlite_db_path()
    if not db.is_file():
        _log.warning("数据库文件尚不存在: %s（页面将为空，采集后会写入）", db)

    app = create_app()
    url = f"http://{args.host}:{args.port}/"
    print(f"商品库浏览: {url}")
    print(f"数据库: {db}")
    print("启动时将后台同步微猫/韩文分类 JSON（可在页面「分类配对」「无价白名单」中维护）")
    start_background_sync()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0
