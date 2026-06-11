"""商品媒体 URL：视频识别与静态图过滤。"""

from product_feed_kr.wego.wego_commodity import (
    commodity_image_urls,
    commodity_raw_media_urls,
    filter_image_urls,
    is_video_media_url,
)


def test_is_video_media_url_by_extension():
    assert is_video_media_url("https://xcimg.szwego.com/foo/bar.mp4")
    assert is_video_media_url("https://example.com/a.MOV?token=1")
    assert not is_video_media_url("https://example.com/a.jpg")


def test_is_video_media_url_by_path():
    assert is_video_media_url("https://cdn.example.com/video/abc123")
    assert is_video_media_url("https://video.szwego.com/play/xyz")


def test_filter_image_urls_keeps_photos_only():
    urls = [
        "https://example.com/1.jpg",
        "https://example.com/2.mp4",
        "https://example.com/3.png",
    ]
    assert filter_image_urls(urls) == [
        "https://example.com/1.jpg",
        "https://example.com/3.png",
    ]


def test_commodity_image_urls_excludes_video():
    com = {
        "imgsSrc": [
            "https://example.com/cover.mp4",
            "https://example.com/photo.jpg",
        ],
    }
    assert commodity_raw_media_urls(com) == com["imgsSrc"]
    assert commodity_image_urls(com) == ["https://example.com/photo.jpg"]
