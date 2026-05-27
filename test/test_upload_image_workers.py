"""上架图片并行下载 worker 数 / 预取队列。"""

from __future__ import annotations

from product_feed_kr.seven17.seven17_upload import (
    upload_image_download_workers,
    upload_image_prefetch_queue_size,
)


def test_upload_image_download_workers_in_range():
    n = upload_image_download_workers(100)
    assert 1 <= n <= 16


def test_upload_image_prefetch_queue_size_from_config():
    assert upload_image_prefetch_queue_size() == 8
