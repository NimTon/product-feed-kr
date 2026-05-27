"""拉取微猫 wecatalog 店铺「标签/分类树」（commodity/tags），写入 JSON。

`wecatalog_scrape_store` 会请求同一接口，按返回的分组顺序遍历标签并拉商品详情；独立站类目路径仍由
本地 `wecatalog_tag_category_map.json` 映射。

匿名 HTTPS 常返回 errcode 9，需在浏览器语境下请求。若 urllib 失败，则用 Playwright 打开种子页后
在页面内 fetch。未指定 `--seed-url` / `--item-id` 时，默认种子为
`https://www.wecatalog.cn/weshop/{album_id}`（店铺入口即可，无需具体商品）。

示例：

  python -m product_feed_kr.wecatalog.wecatalog_fetch_tags \\
    --album-id _ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg \\
    --out data/wecatalog_tag_tree.json

  # 仅 tagName 分组树（根为 JSON 数组）
  python -m product_feed_kr.wecatalog.wecatalog_fetch_tags \\
    --album-id _ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg \\
    --names-only --out data/wecatalog_tag_names.json

  # 可选：`-v` stderr DEBUG；`--log-file path` 另写 UTF-8 日志（与 scrape 一致）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from product_feed_kr.common.playwright_path import chromium_executable
from product_feed_kr.common.pf_log import configure_module_logging, pf_kv, pf_trunc

_log = logging.getLogger(__name__)

TAGS_PATH = "https://www.wecatalog.cn/commodity/tags"


def default_shop_seed_url(album_id: str) -> str:
    return f"https://www.wecatalog.cn/weshop/{album_id}"


def tags_api_url(*, album_id: str, trans_lang: str) -> str:
    qs = urllib.parse.urlencode(
        {
            "albumId": album_id,
            "transLang": trans_lang,
            "hasVideo": "0",
            "hideUnCategorized": "true",
        },
    )
    return f"{TAGS_PATH}?{qs}"


def _http_get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.wecatalog.cn/",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _launch_browser(p, *, headless: bool = True):
    exe = chromium_executable()
    if exe:
        return p.chromium.launch(headless=headless, executable_path=str(exe))
    for ch in ("chrome", "msedge"):
        try:
            return p.chromium.launch(headless=headless, channel=ch)
        except Exception:
            continue
    raise FileNotFoundError(
        "未找到 chrome-win/chrome.exe，且本机无 Chrome/Edge 供 Playwright（channel）启动",
    )


def fetch_tags_via_browser(seed_url: str, api_url: str) -> dict[str, Any]:
    p = sync_playwright().start()
    browser = _launch_browser(p)
    try:
        page = browser.new_page()
        page.goto(seed_url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(2_000)
        data = page.evaluate(
            """async (apiUrl) => {
                const r = await fetch(apiUrl, { credentials: 'include' });
                return await r.json();
            }""",
            api_url,
        )
        if not isinstance(data, dict):
            raise RuntimeError("fetch 返回非对象")
        return data
    finally:
        browser.close()
        p.stop()


def build_group_tree(result: dict[str, Any]) -> list[dict[str, Any]]:
    all_tags = result.get("allTags") or []
    by_id: dict[Any, dict[str, Any]] = {}
    if isinstance(all_tags, list):
        for t in all_tags:
            if isinstance(t, dict) and t.get("tagId") is not None:
                by_id[t["tagId"]] = t

    groups_in = result.get("tagGroups") or []
    out: list[dict[str, Any]] = []
    if not isinstance(groups_in, list):
        return out

    for g in groups_in:
        if not isinstance(g, dict):
            continue
        child_ids = g.get("childrenTag") or []
        tags: list[dict[str, Any]] = []
        if isinstance(child_ids, list):
            for tid in child_ids:
                if tid in by_id:
                    tags.append(by_id[tid])
        out.append(
            {
                "groupId": g.get("groupId"),
                "groupName": g.get("groupName"),
                "level": g.get("level"),
                "order": g.get("order"),
                "tagCount": g.get("tagCount"),
                "tags": tags,
            },
        )
    return out


def build_tag_name_tree(result: dict[str, Any]) -> list[dict[str, Any]]:
    """仅分组名 + 叶子 tagName 列表（无 id、图片等）。"""
    slim: list[dict[str, Any]] = []
    for g in build_group_tree(result):
        raw_tags = g.get("tags") or []
        names: list[str] = []
        if isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, dict):
                    n = str(t.get("tagName") or "").strip()
                    if n:
                        names.append(n)
        gn = str(g.get("groupName") or "").strip()
        slim.append({"groupName": gn, "tags": names})
    return slim


def fetch_tags(
    album_id: str,
    *,
    trans_lang: str,
    seed_url: str | None,
) -> tuple[dict[str, Any], str | None]:
    """返回 (接口 JSON, 实际使用的浏览器种子 URL；urllib 成功时为 None)。"""
    api_url = tags_api_url(album_id=album_id, trans_lang=trans_lang)
    try:
        data = _http_get_json(api_url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        data = {}

    ok = isinstance(data, dict) and data.get("errcode") in (0, None) and data.get("success") is not False
    r = data.get("result") if isinstance(data, dict) else None
    has_tags = isinstance(r, dict) and isinstance(r.get("allTags"), list) and len(r["allTags"]) > 0

    if ok and has_tags:
        return data, None

    seed = (seed_url or "").strip() or default_shop_seed_url(album_id)
    _log.info(
        "%s",
        pf_kv(
            [("event", "tags.via_browser"), ("seed", pf_trunc(seed, 120))],
            zh="直拉标签接口无数据或失败，改用浏览器会话拉取",
        ),
    )
    return fetch_tags_via_browser(seed, api_url), seed


def main() -> int:
    ap = argparse.ArgumentParser(description="wecatalog 标签/分类树 → JSON")
    ap.add_argument("--album-id", required=True, help="店铺/相册 ID（URL 片段 shopId）")
    ap.add_argument("--trans-lang", default="zh", help="transLang，默认 zh")
    ap.add_argument(
        "--item-id",
        default="",
        help="任选该店铺一件商品的 itemId；与 --album-id 拼成详情页作浏览器种子",
    )
    ap.add_argument(
        "--seed-url",
        default="",
        help="Playwright 先打开的页面（建议该店铺商品详情完整 URL），覆盖 --item-id",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="输出 JSON 路径；完整模式默认 data/wecatalog_tags_<album尾部>.json；仅名称树见 --names-only",
    )
    ap.add_argument(
        "--names-only",
        action="store_true",
        help="只写入 tagName 树：JSON 根为数组 [{groupName, tags:[...]}, ...]，不含接口原始字段",
    )
    ap.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="额外写入日志文件（UTF-8）；不设则仅 stderr",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG（如 urllib 直拉失败再走浏览器等细节）")
    args = ap.parse_args()

    configure_module_logging(__name__, log_file=args.log_file, verbose=args.verbose)

    album_id = args.album_id.strip()
    if not album_id:
        err = {"ok": False, "error": "album-id 为空"}
        _log.error(
            "%s",
            pf_kv([("event", "tags.cli_error"), ("reason", "album_id_empty")], zh="参数错误：店铺 album-id 为空"),
        )
        print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
        return 1

    seed: str | None = None
    su = args.seed_url.strip()
    if su:
        seed = su
    elif args.item_id.strip():
        seed = f"https://www.wecatalog.cn/weshop/goods/{album_id}/{args.item_id.strip()}"

    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "tags.start"),
                ("album_id", album_id),
                ("trans_lang", args.trans_lang.strip() or "zh"),
                ("names_only", args.names_only),
                ("seed_custom", bool(su or args.item_id.strip())),
            ],
            zh="开始拉取微猫店铺标签/分类树",
        ),
    )

    try:
        raw, seed_used = fetch_tags(
            album_id,
            trans_lang=args.trans_lang.strip() or "zh",
            seed_url=seed,
        )
    except Exception as e:
        _log.exception("%s", pf_kv([("event", "tags.fetch_fail"), ("err", str(e))], zh="拉取标签过程异常"))
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1

    if not isinstance(raw, dict):
        _log.error(
            "%s",
            pf_kv([("event", "tags.cli_error"), ("reason", "not_json_object")], zh="接口返回不是 JSON 对象"),
        )
        print(json.dumps({"ok": False, "error": "接口返回非 JSON 对象"}, ensure_ascii=False), file=sys.stderr)
        return 1

    result = raw.get("result")
    if not isinstance(result, dict):
        _log.error(
            "%s",
            pf_kv(
                [("event", "tags.cli_error"), ("reason", "no_result"), ("errcode", raw.get("errcode"))],
                zh="响应里缺少 result 字段",
            ),
        )
        print(
            json.dumps(
                {"ok": False, "error": "响应缺少 result", "raw": raw},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    tail = album_id[-12:] if len(album_id) > 12 else album_id
    out_path = args.out
    if out_path is None:
        out_path = (
            Path("data") / f"wecatalog_tag_names_{tail}.json"
            if args.names_only
            else Path("data") / f"wecatalog_tags_{tail}.json"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.names_only:
        name_tree = build_tag_name_tree(result)
        out_path.write_text(json.dumps(name_tree, ensure_ascii=False, indent=2), encoding="utf-8")
        n_names = sum(len(x.get("tags") or []) for x in name_tree)
        summary = {
            "ok": True,
            "out": str(out_path),
            "names_only": True,
            "groups": len(name_tree),
            "tagNames": n_names,
        }
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "tags.done"),
                    ("out", str(out_path)),
                    ("names_only", 1),
                    ("groups", len(name_tree)),
                    ("tagNames", n_names),
                    ("browser_seed", seed_used or ""),
                ],
                zh="标签名称树已写入文件（仅名称模式）",
            ),
        )
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    payload = {
        "meta": {
            "albumId": album_id,
            "transLang": args.trans_lang.strip() or "zh",
            "api": TAGS_PATH,
            "savedAt": datetime.now(timezone.utc).isoformat(),
            "browserSeedUrl": seed_used,
        },
        "response": raw,
        "tree": build_group_tree(result),
    }

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    n_all = len(result.get("allTags") or [])
    n_grp = len(result.get("tagGroups") or [])
    summary = {
        "ok": True,
        "out": str(out_path),
        "errcode": raw.get("errcode"),
        "allTags": n_all,
        "tagGroups": n_grp,
        "treeGroups": len(payload["tree"]),
    }
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "tags.done"),
                ("out", str(out_path)),
                ("errcode", raw.get("errcode")),
                ("allTags", n_all),
                ("tagGroups", n_grp),
                ("treeGroups", len(payload["tree"])),
                ("browser_seed", seed_used or ""),
            ],
            zh="完整标签 JSON 已写入（含接口原始响应与树）",
        ),
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
