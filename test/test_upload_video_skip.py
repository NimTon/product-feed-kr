"""上架跳过：媒体为视频。"""

from product_feed_kr.seven17.seven17_upload import _upload_skip_reason


def test_upload_skip_reason_video_media() -> None:
    rec = {
        "goods_id": "g1",
        "can_process": True,
        "llm_processed_at": "2026-01-01T00:00:00+08:00",
        "can_upload": True,
        "seven17_ca_id": "201010",
        "commodity_title": "测试鞋",
        "commodity_image_urls": ["https://example.com/promo.mp4"],
        "price_cny": "650",
        "price_krw": "130000",
    }
    assert (
        _upload_skip_reason(
            rec,
            skip_uploaded=False,
            llm_on=False,
            default_price="0",
        )
        == "video_media"
    )


def test_upload_skip_reason_mixed_media_not_skipped_as_video() -> None:
    rec = {
        "goods_id": "g2",
        "can_process": True,
        "llm_processed_at": "2026-01-01T00:00:00+08:00",
        "can_upload": True,
        "seven17_ca_id": "201010",
        "commodity_title": "测试鞋",
        "commodity_image_urls": [
            "https://example.com/promo.mp4",
            "https://example.com/photo.jpg",
        ],
        "price_cny": "650",
        "price_krw": "130000",
    }
    assert (
        _upload_skip_reason(
            rec,
            skip_uploaded=False,
            llm_on=False,
            default_price="0",
        )
        is None
    )
