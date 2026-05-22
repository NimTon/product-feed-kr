"""Flask 只读浏览服务：查询 ``pf_store_item`` 表格 + 分页。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from product_feed_kr.pf_browser.queries import get_item, list_albums, list_items
from product_feed_kr.seven17_config import getenv
from product_feed_kr.store_sqlite import sqlite_db_path

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
