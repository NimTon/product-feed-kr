"""颜色九宫格合成与提示词。"""
from __future__ import annotations

from unittest.mock import patch

from product_feed_kr.listing_llm_enrich import (
    _build_color_vision_grid_data_url,
    _system_listing_prompt,
    listing_llm_color_vision_max_images,
)


def test_max_images_capped_at_nine() -> None:
    with patch(
        "product_feed_kr.listing_llm_enrich._cfg_get",
        return_value="12",
    ):
        assert listing_llm_color_vision_max_images() == 9


def test_vision_prompt_ignores_title_colors() -> None:
    with patch(
        "product_feed_kr.listing_llm_enrich.listing_llm_color_vision_enabled",
        return_value=True,
    ):
        p = _system_listing_prompt()
    assert "九宫格" in p
    assert "忽略" in p and "标题" in p


def test_build_grid_from_local_images() -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    import base64
    import io

    def _tiny_data_url(rgb: tuple[int, int, int]) -> str:
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), rgb).save(buf, format="JPEG")
        b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"

    urls = [
        _tiny_data_url((255, 0, 0)),
        _tiny_data_url((0, 255, 0)),
        _tiny_data_url((0, 0, 255)),
    ]

    with patch(
        "product_feed_kr.listing_llm_enrich._fetch_rgb_image_from_url",
        side_effect=lambda u: Image.open(
            io.BytesIO(base64.standard_b64decode(u.split(",", 1)[1]))
        ).convert("RGB"),
    ):
        out = _build_color_vision_grid_data_url(
            urls,
            max_images=9,
            max_grid_px=300,
        )
    assert out is not None
    assert out.startswith("data:image/jpeg;base64,")
    raw = base64.standard_b64decode(out.split(",", 1)[1])
    grid = Image.open(io.BytesIO(raw))
    assert max(grid.size) <= 300
