"""爬取前从 txt 生成 JSON；发现未映射的 (分组, 标签) 时追加到 txt 并打日志提醒。"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from product_feed_kr.pf_log import pf_kv
from product_feed_kr.pf_time import now_cst8_iso
from product_feed_kr.wecatalog_tag_category_map_builder import (
    CategoryMapTxtError,
    _DEFAULT_TXT,
    _SECTION_HEADER_LEFT,
    build_json_rows,
    parse_category_map_txt,
    write_map_from_txt,
)
from product_feed_kr.wecatalog_tag_mapping import invalidate_mapping_cache, resolve_category_path

_log = logging.getLogger(__name__)

PLACEHOLDER_PATH = "（待补全）"
_AUTO_MARKER = "# --- 以下由 scrape 自动追加（请将「（待补全）」改为韩文路径，> 分隔）---"


def rebuild_map_json_from_txt(*, txt_path: Path | None = None) -> int:
    """从 config txt 生成 ``wecatalog_tag_category_map.json`` 并刷新内存缓存。返回 JSON 行数。"""
    _, n = write_map_from_txt(txt_path)
    invalidate_mapping_cache()
    return n


def init_map_from_txt_at_scrape(logger: logging.Logger | None = None) -> int | None:
    """每次抓取开始前调用：用 txt 初始化/刷新 map JSON。"""
    lg = logger or _log
    try:
        n = rebuild_map_json_from_txt()
    except CategoryMapTxtError as e:
        lg.error(
            "%s",
            pf_kv(
                [("event", "scrape.map.init_fail"), ("err", str(e)[:400])],
                zh="分类映射 txt 解析失败，未更新 JSON；请修正 config/wecatalog_tag_category_map.txt",
            ),
        )
        return None
    lg.info(
        "%s",
        pf_kv(
            [
                ("event", "scrape.map.init"),
                ("rows", n),
                ("txt", str(_DEFAULT_TXT)),
            ],
            zh="已从 txt 生成 wecatalog_tag_category_map.json",
        ),
    )
    return n


def iter_leaf_tags_from_groups(
    groups: list[dict[str, Any]],
) -> list[tuple[str, str, int]]:
    """从 commodity/tags 的 ``build_group_tree`` 结果提取 (groupName, tagName, tagId)。"""
    out: list[tuple[str, str, int]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        gname = str(g.get("groupName") or "").strip()
        if not gname:
            continue
        raw_tags = g.get("tags") or []
        if not isinstance(raw_tags, list):
            continue
        for t in raw_tags:
            if not isinstance(t, dict):
                continue
            tname = str(t.get("tagName") or "").strip()
            if not tname:
                continue
            try:
                tid = int(t["tagId"])
            except (TypeError, ValueError, KeyError):
                continue
            out.append((gname, tname, tid))
    return out


def find_unmapped_tags(
    groups: list[dict[str, Any]],
) -> list[tuple[str, str, int]]:
    """当前 JSON 映射中无 ``shop_category_path`` 的 (分组, 标签, tag_id)。"""
    missing: list[tuple[str, str, int]] = []
    for gname, tname, tid in iter_leaf_tags_from_groups(groups):
        if resolve_category_path(gname, tname) is None:
            missing.append((gname, tname, tid))
    return missing


def _keys_in_txt(content: str) -> set[tuple[str, str]]:
    try:
        rows = parse_category_map_txt(content)
    except CategoryMapTxtError:
        return set()
    return {(r[0], r[1]) for r in rows}


def _groups_in_txt(content: str) -> set[str]:
    try:
        rows = parse_category_map_txt(content)
    except CategoryMapTxtError:
        return set()
    return {r[0] for r in rows}


def _max_section_num(content: str) -> int:
    n = 0
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("注"):
            continue
        left = line.replace("＝", "=").split("=", 1)[0].strip()
        m = _SECTION_HEADER_LEFT.match(left)
        if m:
            try:
                n = max(n, int(m.group(1)))
            except ValueError:
                pass
    return n


def append_missing_tags_to_map_txt(
    missing: list[tuple[str, str, int]],
    *,
    txt_path: Path | None = None,
) -> int:
    """
    将尚未出现在 txt 中的 (分组, 标签) 追加到 ``config/wecatalog_tag_category_map.txt``。
    返回本次新增映射行数（不含注释/分组锚点行可单独计：返回子标签行 + 新分组锚点行）。
    """
    path = txt_path or _DEFAULT_TXT
    if not missing:
        return 0

    if path.is_file():
        text = path.read_text(encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            "# 格式：左边 = 右边；每组首行 N,主标签 = 路径（anchor_only）\n"
            "# 同组后续：子标签 = 路径\n"
        )

    existing = _keys_in_txt(text)
    groups_known = _groups_in_txt(text)
    to_add = [(g, t, tid) for g, t, tid in missing if (g, t) not in existing]
    if not to_add:
        return 0

    by_group: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for g, t, tid in to_add:
        by_group[g].append((t, tid))

    next_sec = _max_section_num(text)
    new_lines: list[str] = []
    if _AUTO_MARKER not in text:
        new_lines.extend(
            [
                "",
                _AUTO_MARKER,
                f"# 生成时间 {now_cst8_iso()} — 补全后保存；下次 scrape 开始时会重新生成 JSON",
            ],
        )

    added = 0
    for gname in sorted(by_group.keys()):
        items = sorted(by_group[gname], key=lambda x: x[0])
        if gname not in groups_known:
            next_sec += 1
            new_lines.append(f"{next_sec},{gname} = {PLACEHOLDER_PATH}")
            groups_known.add(gname)
            existing.add((gname, gname))
            added += 1
        for tname, tid in items:
            if (gname, tname) in existing:
                continue
            suffix = f"  # tag_id={tid}"
            new_lines.append(f"{tname} = {PLACEHOLDER_PATH}{suffix}")
            existing.add((gname, tname))
            added += 1

    if not new_lines:
        return 0

    suffix_block = "\n".join(new_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    text += suffix_block + "\n"
    path.write_text(text, encoding="utf-8")
    return added


def sync_unmapped_tags_after_tags(
    groups: list[dict[str, Any]],
    logger: logging.Logger | None = None,
) -> tuple[int, list[tuple[str, str, int]]]:
    """
    对比 API 标签树与当前映射：缺失项写入 txt，打 WARNING 列出清单。
    返回 (本次追加到 txt 的行数, 未映射列表)。
    """
    lg = logger or _log
    missing = find_unmapped_tags(groups)
    if not missing:
        lg.info(
            "%s",
            pf_kv([("event", "scrape.map.ok")], zh="微猫全部分组/标签均已有分类映射"),
        )
        return 0, []

    added = append_missing_tags_to_map_txt(missing)
    if added > 0:
        try:
            rebuild_map_json_from_txt()
            lg.info(
                "%s",
                pf_kv(
                    [("event", "scrape.map.rebuild"), ("txt_appended", added)],
                    zh="已追加待补全行到 txt 并重新生成 JSON（占位路径）",
                ),
            )
        except CategoryMapTxtError as e:
            lg.error(
                "%s",
                pf_kv(
                    [("event", "scrape.map.rebuild_fail"), ("err", str(e)[:400])],
                    zh="追加 txt 后生成 JSON 失败，请手工运行 build_wecatalog_tag_category_map.bat",
                ),
            )

    lg.warning(
        "%s",
        pf_kv(
            [
                ("event", "scrape.map.unmapped"),
                ("unmapped", len(missing)),
                ("txt_appended", added),
                ("txt", str(_DEFAULT_TXT)),
            ],
            zh="以下分组/标签尚无映射，已写入 txt 请补全韩文路径（> 分隔）",
        ),
    )
    show_max = 40
    for gname, tname, tid in missing[:show_max]:
        lg.warning(
            "%s",
            pf_kv(
                [
                    ("event", "scrape.map.unmapped_row"),
                    ("group", gname),
                    ("tag", tname),
                    ("tag_id", tid),
                ],
                zh="未映射",
            ),
        )
    if len(missing) > show_max:
        lg.warning(
            "%s",
            pf_kv(
                [
                    ("event", "scrape.map.unmapped_more"),
                    ("hidden", len(missing) - show_max),
                ],
                zh="未映射条目过多，其余见 config/wecatalog_tag_category_map.txt 末尾自动追加区",
            ),
        )

    return added, missing
