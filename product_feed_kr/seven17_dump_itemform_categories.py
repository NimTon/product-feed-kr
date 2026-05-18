"""登录 seven17 后台，打开商品录入页 **itemform.php**，抓取分类下拉框选项。

读出每个 `<option>` 的 **value**（`ca_id`）与 **label**（韩文路径），写入 ``seven17_path_ca_map.json``。

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

from product_feed_kr.seven17_config import getenv, getenv_required
from product_feed_kr.seven17_path_ca_map import (
    build_path_ca_map_payload,
    fetch_itemform_ca_selects,
    write_path_ca_map,
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
    itemform_url = f"{base}/adm/shop_admin/itemform.php"

    try:
        selects = fetch_itemform_ca_selects(mb_id=mb_id, mb_password=mb_password, base=base)
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1

    path_payload = build_path_ca_map_payload(selects, itemform_url=itemform_url)
    write_path_ca_map(path_payload)

    payload: dict = {
        "ok": True,
        "itemform_url": itemform_url,
        "selects": selects,
        "path_ca_map": {
            "updated_at": path_payload.get("updated_at"),
            "entries": len(path_payload.get("path_to_ca_id") or {}),
        },
        "hint": "韩文路径→ca_id 已写入 data/seven17_path_ca_map.json；上架时按映射表路径查 id",
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
