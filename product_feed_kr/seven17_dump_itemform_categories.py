"""登录 seven17 后台，打开商品录入页 **itemform.php**，抓取分类下拉框选项。

读出每个 `<option>` 的 **value**（即上架时要填的 `ca_id`）与 **label**（后台显示的文案），
便于你对照 `wecatalog_tag_category_map.json`，把合适的 **value** 写进 **meta.seven17_ca_id**。

凭据：`config/seven17.json` 或环境变量（与 `seven17_upload` 相同）：`SEVEN17_MB_ID`、`SEVEN17_MB_PASSWORD`。

用法::

  python -m product_feed_kr.seven17_dump_itemform_categories
  python -m product_feed_kr.seven17_dump_itemform_categories --out data/seven17_ca_options.json

调试：`SEVEN17_HEADLESS=0` 或配置里 `"SEVEN17_HEADLESS": false` 可看浏览器。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from product_feed_kr.playwright_path import chromium_executable
from product_feed_kr.seven17_adm import login_admin
from product_feed_kr.seven17_config import bool_env, getenv, getenv_required


def _dump_one_select(page, name: str) -> list[dict[str, str]] | None:
    return page.evaluate(
        """name => {
          const sel = document.querySelector('select[name="' + name + '"]');
          if (!sel) return null;
          return Array.from(sel.options).map(o => ({
            value: String(o.value),
            label: (o.textContent || '').trim().replace(/\\s+/g, ' ')
          }));
        }""",
        name,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取 itemform 上 ca_id / ca_id2 / ca_id3 下拉选项")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="另存为 UTF-8 JSON（仍会打印一份到 stdout）",
    )
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    mb_id = getenv_required("SEVEN17_MB_ID")
    mb_password = getenv_required("SEVEN17_MB_PASSWORD")
    base = (getenv("SEVEN17_BASE_URL", "https://www.seven17.kr") or "https://www.seven17.kr").rstrip("/")
    headless = bool_env("SEVEN17_HEADLESS", True)

    exe = chromium_executable()
    if not exe:
        print(json.dumps({"ok": False, "error": "未找到 Chromium"}, ensure_ascii=False), file=sys.stderr)
        return 1

    itemform_url = f"{base}/adm/shop_admin/itemform.php"

    payload: dict = {
        "ok": True,
        "itemform_url": itemform_url,
        "selects": {},
        "hint": "把需要的 option.value 写入 wecatalog_tag_category_map.json 对应行的 meta.seven17_ca_id（字符串）",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, executable_path=str(exe))
        try:
            page = browser.new_page()
            login_admin(
                page,
                base=base,
                mb_id=mb_id,
                mb_password=mb_password,
                redirect_full_url=itemform_url,
            )

            if "login.php" in page.url:
                print(
                    json.dumps(
                        {"ok": False, "error": "登录失败，仍在 login.php", "url": page.url},
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                return 2

            page.wait_for_selector(
                'form[name="fitemform"], select[name="ca_id"]',
                timeout=90_000,
            )

            for name in ("ca_id", "ca_id2", "ca_id3"):
                payload["selects"][name] = _dump_one_select(page, name)

            payload["page_url_after_load"] = page.url

        finally:
            browser.close()

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
