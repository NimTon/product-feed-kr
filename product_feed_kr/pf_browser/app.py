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
    sync_all_category_maps,
)
from product_feed_kr.pf_browser.diagnose import diagnose_wecatalog_goods
from product_feed_kr.pf_browser.queries import get_item, list_albums, list_items, scrape_category_summary
from product_feed_kr.common.seven17_config import getenv
from product_feed_kr.pf_browser.category_whitelists import (
    get_category_whitelists_state,
    save_category_whitelists,
)
from product_feed_kr.pf_browser.upload_settings import (
    get_upload_settings_state,
    save_upload_settings,
)
from product_feed_kr.pf_browser.upload_priority import (
    get_upload_priority_state,
    save_upload_priority_from_body,
)
from product_feed_kr.db.store_sqlite import (
    connect_sqlite,
    ensure_sqlite_schema,
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

    @app.get("/api/scrape-category-summary")
    def scrape_category_summary_api() -> object:
        try:
            return jsonify(scrape_category_summary())
        except Exception as e:
            _log.exception("scrape_category_summary failed")
            return jsonify({"ok": False, "error": str(e)}), 500

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

    @app.get("/api/category-whitelists")
    def category_whitelists_get() -> object:
        try:
            return jsonify(get_category_whitelists_state())
        except Exception as e:
            _log.exception("category_whitelists_get failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/category-whitelists")
    def category_whitelists_save() -> object:
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(save_category_whitelists(body))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            _log.exception("category_whitelists_save failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/upload-settings")
    def upload_settings_get() -> object:
        try:
            return jsonify(get_upload_settings_state())
        except Exception as e:
            _log.exception("upload_settings_get failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/upload-settings")
    def upload_settings_save() -> object:
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(save_upload_settings(body))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            _log.exception("upload_settings_save failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/upload-priority")
    def upload_priority_get() -> object:
        try:
            return jsonify(get_upload_priority_state())
        except Exception as e:
            _log.exception("upload_priority_get failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/upload-priority")
    def upload_priority_save() -> object:
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(save_upload_priority_from_body(body))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            _log.exception("upload_priority_save failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="product-feed-kr 商品库浏览（只读）")
    ap.add_argument("--host", default=getenv("PF_BROWSER_HOST", "127.0.0.1") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(getenv("PF_BROWSER_PORT", "8765") or "8765"))
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument(
        "--skip-category-sync",
        action="store_true",
        help="跳过启动时的微猫/韩文分类同步",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db = sqlite_db_path()
    had_db = db.is_file()
    conn = connect_sqlite()
    try:
        ensure_sqlite_schema(conn)
    finally:
        conn.close()
    if not had_db:
        print(f"已初始化空数据库: {db}")

    app = create_app()
    url = f"http://{args.host}:{args.port}/"
    print(f"商品库浏览: {url}")
    print(f"数据库: {db}")
    if args.skip_category_sync:
        print("已跳过启动分类同步（--skip-category-sync）")
    else:
        print("正在同步微猫/韩文分类（commodity/tags + seven17 itemform）…")
        sync_result = sync_all_category_maps()
        if sync_result.get("ok"):
            wc = sync_result.get("wecatalog") or {}
            s17 = sync_result.get("seven17") or {}
            print(
                f"分类同步完成：微猫 {wc.get('group_count', '?')} 组 / "
                f"{wc.get('tag_count', '?')} 标签；"
                f"韩文 {s17.get('entry_count', '?')} 项"
            )
        else:
            errs = sync_result.get("errors") or []
            print("分类同步未全部成功（页面仍可使用已有 JSON）：")
            for err in errs:
                print(f"  - {err}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0
