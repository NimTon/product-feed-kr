"""用本地 chrome-win 下的 Chromium 做一次连通性测试。"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from product_feed_kr.playwright_path import chromium_executable


def main() -> int:
    exe = chromium_executable()
    if not exe:
        print(
            "未找到 Chromium：请在项目根放置 chrome-win/chrome.exe，"
            "或设置环境变量 PLAYWRIGHT_CHROMIUM_EXECUTABLE=完整路径",
            file=sys.stderr,
        )
        return 1
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=str(exe),
        )
        try:
            page = browser.new_page()
            page.goto("https://example.com", wait_until="domcontentloaded", timeout=60_000)
            print("ok title=", page.title())
            print("executable=", exe)
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
