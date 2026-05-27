"""本地 Chromium 路径：默认使用项目根目录下 chrome-win/chrome.exe。"""

from __future__ import annotations

import os
from pathlib import Path

from product_feed_kr._paths import REPO_ROOT as PROJECT_ROOT
DEFAULT_CHROMIUM = PROJECT_ROOT / "chrome-win" / "chrome.exe"


def chromium_executable() -> Path | None:
    env = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    return DEFAULT_CHROMIUM if DEFAULT_CHROMIUM.is_file() else None
