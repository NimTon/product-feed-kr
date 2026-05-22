"""Flask 商品库浏览：查询 ``pf_store_item`` + 单条重跑（爬取/处理/上传）。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from product_feed_kr.pf_browser.actions import request_item_rerun
from product_feed_kr.pf_browser.queries import get_item, list_albums, list_items
from product_feed_kr.seven17_config import getenv
from product_feed_kr.store_sqlite import (
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
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0
