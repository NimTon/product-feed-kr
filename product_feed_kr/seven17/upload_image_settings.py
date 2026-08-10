"""上架图片缩放配置（``SEVEN17_UPLOAD_IMAGE_MAX_PX``）。"""

from __future__ import annotations

from typing import Any

from product_feed_kr.common.seven17_config import getenv

CONFIG_KEY = "SEVEN17_UPLOAD_IMAGE_MAX_PX"
MIN_PX = 0
MAX_PX = 8192


def clamp_upload_image_max_px(value: Any) -> int:
    """上传图长边像素上限；0 表示不缩放。"""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        n = 0
    return max(MIN_PX, min(n, MAX_PX))


def upload_image_max_px() -> int:
    raw = getenv(CONFIG_KEY)
    if raw is None or not str(raw).strip():
        return 0
    return clamp_upload_image_max_px(raw)
