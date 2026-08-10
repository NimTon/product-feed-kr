"""上架相关配置：浏览器读写 ``config/seven17.json``。"""

from __future__ import annotations

import json
from typing import Any

from product_feed_kr.common.seven17_config import getenv, reload_seven17_config
from product_feed_kr.seven17.no_price_whitelist import load_config_data
from product_feed_kr.seven17.upload_image_settings import (
    CONFIG_KEY,
    clamp_upload_image_max_px,
    upload_image_max_px,
)


def get_upload_settings_state() -> dict[str, Any]:
    cfg_path, cfg_data = load_config_data()
    stored = cfg_data.get(CONFIG_KEY, upload_image_max_px())
    return {
        "ok": True,
        "config_path": str(cfg_path),
        "config_key": CONFIG_KEY,
        "upload_image_max_px": clamp_upload_image_max_px(stored),
    }


def save_upload_settings(body: dict[str, Any]) -> dict[str, Any]:
    if "upload_image_max_px" not in body:
        raise ValueError("missing_upload_image_max_px")
    max_px = clamp_upload_image_max_px(body["upload_image_max_px"])
    cfg_path, cfg_data = load_config_data()
    cfg_data[CONFIG_KEY] = str(max_px)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reload_seven17_config()
    return {
        "ok": True,
        "config_path": str(cfg_path),
        "upload_image_max_px": max_px,
    }
