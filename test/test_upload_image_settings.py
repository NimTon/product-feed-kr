"""上架图片最大边长配置与缩放。"""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from product_feed_kr.pf_browser import upload_settings as us
from product_feed_kr.seven17 import upload_image_settings as uis
from product_feed_kr.seven17.seven17_upload import (
    _cleanup_upload_temp_images,
    resize_upload_image_if_needed,
    upload_temp_dir,
)


def test_clamp_upload_image_max_px() -> None:
    assert uis.clamp_upload_image_max_px(0) == 0
    assert uis.clamp_upload_image_max_px(1200) == 1200
    assert uis.clamp_upload_image_max_px(99999) == 8192
    assert uis.clamp_upload_image_max_px("bad") == 0


def test_save_upload_settings_writes_config(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "seven17.json"
    cfg.write_text(json.dumps({"SEVEN17_MB_ID": "x"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        "product_feed_kr.seven17.no_price_whitelist.seven17_config_path",
        lambda: cfg,
    )
    monkeypatch.setattr("product_feed_kr.common.seven17_config.reload_seven17_config", lambda: None)

    out = us.save_upload_settings({"upload_image_max_px": 1600})
    assert out["ok"] is True
    assert out["upload_image_max_px"] == 1600
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["SEVEN17_UPLOAD_IMAGE_MAX_PX"] == "1600"
    assert data["SEVEN17_MB_ID"] == "x"


def test_resize_upload_image_if_needed_downscales(tmp_path) -> None:
    src = tmp_path / "big.png"
    im = Image.new("RGB", (2000, 1000), (255, 0, 0))
    im.save(src, format="PNG")

    out = resize_upload_image_if_needed(src, 800)
    assert out.is_file()
    with Image.open(out) as resized:
        w, h = resized.size
    assert max(w, h) == 800
    assert out.suffix.lower() == ".jpg"


def test_resize_upload_image_if_needed_skips_when_small(tmp_path) -> None:
    src = tmp_path / "small.jpg"
    im = Image.new("RGB", (400, 300), (0, 255, 0))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    src.write_bytes(buf.getvalue())

    out = resize_upload_image_if_needed(src, 1200)
    assert out == src
    with Image.open(out) as kept:
        assert kept.size == (400, 300)


def test_resize_upload_image_if_needed_noop_when_zero(tmp_path) -> None:
    src = tmp_path / "img.jpg"
    im = Image.new("RGB", (3000, 2000), (0, 0, 255))
    im.save(src, format="JPEG")
    out = resize_upload_image_if_needed(src, 0)
    assert out == src


def test_cleanup_upload_temp_images_deletes_files(tmp_path) -> None:
    paths = []
    for i in range(3):
        p = tmp_path / f"tmp{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff" + b"0" * 20)
        paths.append(p)
    _cleanup_upload_temp_images(paths)
    assert all(not p.exists() for p in paths)


def test_upload_temp_dir_is_repo_tmp() -> None:
    from product_feed_kr._paths import REPO_ROOT

    d = upload_temp_dir()
    assert d == REPO_ROOT / "tmp"
    assert d.is_dir()
