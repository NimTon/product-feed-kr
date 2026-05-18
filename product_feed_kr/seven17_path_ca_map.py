"""seven17 后台 itemform 分类下拉：韩文路径 label → ``ca_id`` value。

与 ``wecatalog_tag_category_map``（微猫分组/标签 → 韩文路径，用户维护 txt）分离；
本文件由抓取初始化或 ``seven17_dump_itemform_categories`` 写入，供上架按路径查 id。

默认路径：``data/seven17_path_ca_map.json``（``SEVEN17_PATH_CA_MAP_JSON``）。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from product_feed_kr.pf_log import pf_kv
from product_feed_kr.pf_time import now_cst8_iso
from product_feed_kr.playwright_path import chromium_executable
from product_feed_kr.seven17_adm import login_admin
from product_feed_kr.seven17_config import bool_env, getenv, getenv_required

_log = logging.getLogger(__name__)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def path_ca_map_file() -> Path:
    raw = (getenv("SEVEN17_PATH_CA_MAP_JSON") or "data/seven17_path_ca_map.json").strip()
    p = Path(raw).expanduser()
    return p.resolve() if p.is_absolute() else (_project_root() / p).resolve()


def path_tuple_to_label(path: tuple[str, ...] | list[str] | None) -> str:
    if not path:
        return ""
    return " > ".join(str(x).strip() for x in path if str(x).strip())


def labels_to_path_ca_id(opts: list[dict[str, str]] | None) -> dict[str, str]:
    """从 ca_id 下拉的 option 列表生成 label → value。"""
    if not opts:
        return {}
    out: dict[str, str] = {}
    for o in opts:
        if not isinstance(o, dict):
            continue
        val = str(o.get("value") or "").strip()
        lab = str(o.get("label") or "").strip()
        if not val or not lab or lab == "선택하세요":
            continue
        out[lab] = val
    return out


def _dump_one_select(page: Any, name: str) -> list[dict[str, str]] | None:
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


def fetch_itemform_ca_selects(
    *,
    mb_id: str,
    mb_password: str,
    base: str | None = None,
    headless: bool | None = None,
) -> dict[str, list[dict[str, str]] | None]:
    """登录后台并抓取 itemform 上 ca_id / ca_id2 / ca_id3 下拉选项。"""
    b = (base or getenv("SEVEN17_BASE_URL", "https://www.seven17.kr") or "https://www.seven17.kr").rstrip("/")
    hl = bool_env("SEVEN17_HEADLESS", True) if headless is None else headless
    itemform_url = f"{b}/adm/shop_admin/itemform.php"

    exe = chromium_executable()
    if not exe:
        raise RuntimeError("未找到 Chromium，无法抓取 seven17 分类")

    selects: dict[str, list[dict[str, str]] | None] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=hl, executable_path=str(exe))
        try:
            page = browser.new_page()
            login_admin(
                page,
                base=b,
                mb_id=mb_id,
                mb_password=mb_password,
                redirect_full_url=itemform_url,
            )
            if "login.php" in page.url:
                raise RuntimeError(f"seven17 登录失败，仍在 login.php: {page.url}")
            page.wait_for_selector(
                'form[name="fitemform"], select[name="ca_id"]',
                timeout=90_000,
            )
            for name in ("ca_id", "ca_id2", "ca_id3"):
                selects[name] = _dump_one_select(page, name)
        finally:
            browser.close()
    return selects


def build_path_ca_map_payload(
    selects: dict[str, list[dict[str, str]] | None],
    *,
    itemform_url: str | None = None,
) -> dict[str, Any]:
    path_to_ca_id = labels_to_path_ca_id(selects.get("ca_id"))
    b = (getenv("SEVEN17_BASE_URL", "https://www.seven17.kr") or "https://www.seven17.kr").rstrip("/")
    return {
        "updated_at": now_cst8_iso(),
        "itemform_url": itemform_url or f"{b}/adm/shop_admin/itemform.php",
        "path_to_ca_id": path_to_ca_id,
        "select_counts": {
            k: len(v) if isinstance(v, list) else 0 for k, v in selects.items()
        },
    }


def write_path_ca_map(payload: dict[str, Any], *, out_path: Path | None = None) -> Path:
    path = out_path or path_ca_map_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    invalidate_path_ca_cache()
    return path


def sync_path_ca_map_from_itemform(
    *,
    logger: logging.Logger | None = None,
    out_path: Path | None = None,
) -> int | None:
    """
    登录 itemform 抓取 ca_id 下拉，写入 ``path_to_ca_id`` 映射文件。
    缺少凭据时跳过并返回 None；失败抛错由调用方决定是否中断。
  """
    lg = logger or _log
    if not bool_env("SEVEN17_SYNC_PATH_CA_AT_SCRAPE", True):
        lg.info(
            "%s",
            pf_kv([("event", "scrape.path_ca.skip"), ("reason", "disabled")], zh="已关闭抓取时同步 seven17 路径→ca_id"),
        )
        return None
    try:
        mb_id = getenv_required("SEVEN17_MB_ID")
        mb_password = getenv_required("SEVEN17_MB_PASSWORD")
    except RuntimeError as e:
        lg.warning(
            "%s",
            pf_kv(
                [("event", "scrape.path_ca.skip"), ("reason", "no_credentials"), ("err", str(e)[:200])],
                zh="未配置 seven17 凭据，跳过路径→ca_id 同步（上架时将用已有映射文件或 LLM）",
            ),
        )
        return None

    selects = fetch_itemform_ca_selects(mb_id=mb_id, mb_password=mb_password)
    payload = build_path_ca_map_payload(selects)
    dest = write_path_ca_map(payload, out_path=out_path)
    n = len(payload.get("path_to_ca_id") or {})
    lg.info(
        "%s",
        pf_kv(
            [
                ("event", "scrape.path_ca.synced"),
                ("path", str(dest)),
                ("entries", n),
            ],
            zh="已从 itemform 写入韩文路径→ca_id 映射",
        ),
    )
    return n


@lru_cache(maxsize=1)
def _load_path_to_ca_id() -> dict[str, str]:
    path = path_ca_map_file()
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "path_to_ca_id" in data:
        raw = data["path_to_ca_id"]
    elif isinstance(data, dict):
        raw = {k: v for k, v in data.items() if k not in ("updated_at", "itemform_url", "meta", "select_counts")}
    else:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}


def invalidate_path_ca_cache() -> None:
    _load_path_to_ca_id.cache_clear()


def resolve_ca_id_by_path_label(path_label: str) -> str | None:
    lab = path_label.strip()
    if not lab:
        return None
    return _load_path_to_ca_id().get(lab)


def resolve_ca_id_by_path_tuple(path: tuple[str, ...] | list[str] | None) -> str | None:
    return resolve_ca_id_by_path_label(path_tuple_to_label(path))
