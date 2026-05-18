"""从 config/wecatalog_tag_category_map.txt 生成 wecatalog_tag_category_map.json。

用法（在仓库根目录）::

    python -m product_feed_kr.wecatalog_tag_category_map_builder
    python -m product_feed_kr.wecatalog_tag_category_map_builder --input path/to/custom.txt -v --log-file data/mapbuild.log
    build_wecatalog_tag_category_map.bat

维护：编辑 **config/wecatalog_tag_category_map.txt**（UTF-8）。每行 ``左 = 右``；新分组用
``数字,主标签 = 韩文路径``（该行 anchor_only）；同组后续 ``子标签 = 路径``。
支持全角 ``＝``；若无 ``=`` 可用 Tab 分隔左右。``#`` 行与以 ``注`` 开头的行忽略。

元组可选第五项 ``tag_id``（整数）仅适合在代码里构造；txt 未解析该项，需要时可扩展。

重新生成 JSON 时，会保留现有文件中已填的 **`meta.seven17_ca_id`**。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from product_feed_kr.pf_log import configure_module_logging, pf_kv

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TXT = _REPO_ROOT / "config" / "wecatalog_tag_category_map.txt"

_SECTION_HEADER_LEFT = re.compile(r"^(\d+)\s*,\s*(.+)$")

_FIX_UTF8 = "Re-save the file as UTF-8 (no 'ANSI' / system code page) in VS Code / Notepad++."
_FIX_SEP = "Use either one ASCII '=' or one Tab between tag and path, e.g. Tag = a > b > c."
_FIX_SECTION = (
    "Start the file with a section header line like: 1,MainAlbumTag = Korean > path. "
    "Every child line must appear after its section header."
)
_FIX_DUP = "Remove or rename one of the two lines so (group, tag) is unique, or merge meta in JSON by hand."


class CategoryMapTxtError(ValueError):
    """Human-readable error for mapping txt / preserved JSON issues."""

    def __init__(
        self,
        summary: str,
        *,
        line: int | None = None,
        source_line: str | None = None,
        fix: str = "",
    ) -> None:
        self.summary = summary
        self.line = line
        self.source_line = source_line
        self.fix = fix
        parts = [f"[wecatalog_tag_category_map] {summary}"]
        if line is not None:
            parts.append(f"  line number: {line}")
        if source_line is not None:
            disp = source_line if len(source_line) <= 260 else source_line[:260] + "..."
            parts.append(f"  line content: {disp!r}")
        if fix:
            parts.append(f"  how to fix: {fix}")
        super().__init__("\n".join(parts))


def _read_mapping_txt_file(inp: Path) -> str:
    if not inp.is_file():
        raise CategoryMapTxtError(
            f"mapping txt not found: {inp}",
            fix=f"Create {_DEFAULT_TXT} or pass --input PATH to your txt.",
        )
    try:
        text = inp.read_text(encoding="utf-8")
    except OSError as e:
        raise CategoryMapTxtError(
            f"cannot read file: {e}",
            fix="Check that the path exists, is readable, and not locked by another program.",
        ) from e
    except UnicodeDecodeError as e:
        raise CategoryMapTxtError(
            "file is not valid UTF-8 (wrong encoding or corrupted bytes).",
            fix=_FIX_UTF8,
        ) from e
    if "\x00" in text:
        raise CategoryMapTxtError(
            "file contains NUL (binary) bytes.",
            fix="Paste into a new UTF-8 text file or export from editor without embedded nulls.",
        )
    return text


def _path_segments_or_raise(path_str: str, *, line: int | None, source_line: str | None) -> list[str]:
    segs = [p.strip() for p in path_str.split(">") if p.strip()]
    if not segs:
        raise CategoryMapTxtError(
            "category path (right of '=') is empty or only '>' separators.",
            line=line,
            source_line=source_line,
            fix="Write a non-empty path, e.g. 의류 > 남성의류. Remove stray '>' with nothing between them.",
        )
    return segs


def parse_category_map_txt(content: str) -> list[tuple[str, str, str, bool] | tuple[str, str, str, bool, int]]:
    """Parse mapping txt into rows for build_json_rows."""
    text = content.lstrip("\ufeff")
    current_group: str | None = None
    out: list[tuple[str, str, str, bool] | tuple[str, str, str, bool, int]] = []
    first_line_for_key: dict[tuple[str, str], int] = {}

    def _register_key(group: str, tag: str, ln: int, raw_display: str) -> None:
        key = (group, tag)
        if key in first_line_for_key:
            prev = first_line_for_key[key]
            raise CategoryMapTxtError(
                f"duplicate mapping for the same (group, tag): group={group!r} tag={tag!r}. "
                f"First occurrence was line {prev}, duplicate is line {ln}.",
                line=ln,
                source_line=raw_display,
                fix=_FIX_DUP,
            )
        first_line_for_key[key] = ln

    for ln_no, raw in enumerate(text.splitlines(), start=1):
        raw_display = raw.rstrip("\r\n")
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("\u6ce8"):
            continue
        normalized = line.replace("\uff1d", "=").replace("\u3000", " ")
        if "=" in normalized:
            left, right = normalized.split("=", 1)
        elif "\t" in raw:
            parts = raw.split("\t", 1)
            if len(parts) != 2:
                raise CategoryMapTxtError(
                    "tab-separated line must have exactly one tab between tag and path.",
                    line=ln_no,
                    source_line=raw_display,
                    fix=_FIX_SEP,
                )
            left, right = parts[0].strip(), parts[1].strip()
        else:
            raise CategoryMapTxtError(
                "line has neither '=' nor a tab between tag and path.",
                line=ln_no,
                source_line=raw_display,
                fix=_FIX_SEP,
            )
        left = left.strip()
        right = right.strip()
        if not right:
            raise CategoryMapTxtError(
                "empty path on the right side of '=' (or after tab).",
                line=ln_no,
                source_line=raw_display,
                fix="Add the Korean site path after '=', e.g. Tag = 의류 > 남성의류.",
            )
        _path_segments_or_raise(right, line=ln_no, source_line=raw_display)

        m = _SECTION_HEADER_LEFT.match(left)
        if m:
            tag_name = m.group(2).strip()
            if not tag_name:
                raise CategoryMapTxtError(
                    "section header has a number but empty tag after comma.",
                    line=ln_no,
                    source_line=raw_display,
                    fix="Use format: 1,MyMainTag = path/to/category",
                )
            current_group = tag_name
            _register_key(current_group, tag_name, ln_no, raw_display)
            out.append((current_group, tag_name, right, True))
        else:
            if current_group is None:
                raise CategoryMapTxtError(
                    "child mapping appears before any section header.",
                    line=ln_no,
                    source_line=raw_display,
                    fix=_FIX_SECTION,
                )
            tag = left.strip()
            if not tag:
                raise CategoryMapTxtError(
                    "empty tag on the left side (inside current group).",
                    line=ln_no,
                    source_line=raw_display,
                    fix="Write the wechat album tag before '=', e.g. Brand Name = path.",
                )
            _register_key(current_group, tag, ln_no, raw_display)
            out.append((current_group, tag, right, False))

    if not out:
        raise CategoryMapTxtError(
            "no mapping rows were parsed (file empty or only comments).",
            fix="Add at least one section line like: 1,MainTag = Korean > path",
        )
    return out


def _split_path(s: str) -> list[str]:
    return [p.strip() for p in s.split(">") if p.strip()]


def _parse_meta_row(row: list) -> dict:
    if len(row) < 4:
        return {}
    m = row[3]
    return m if isinstance(m, dict) else {}


def build_json_rows(
    raw_rows: Sequence[tuple[str, str, str, bool] | tuple[str, str, str, bool, int]],
    *,
    preserve_meta_path: Path | None = None,
) -> list[list]:
    preserved_ca: dict[tuple[str, str], str] = {}
    path = preserve_meta_path or Path(__file__).resolve().with_name("wecatalog_tag_category_map.json")
    if path.is_file():
        try:
            raw_prev = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise CategoryMapTxtError(
                f"existing JSON is invalid and cannot be read to preserve seven17_ca_id: {path}",
                fix=f"Repair JSON syntax (see parser message: {e.msg} at char {e.pos}) or temporarily rename the file.",
            ) from e
        except OSError as e:
            raise CategoryMapTxtError(
                f"cannot read existing JSON for meta merge: {path}: {e}",
                fix="Check permissions or close programs locking the file.",
            ) from e
        if isinstance(raw_prev, list):
            for row in raw_prev:
                if not isinstance(row, list) or len(row) < 3:
                    continue
                meta = _parse_meta_row(row)
                v = meta.get("seven17_ca_id")
                if v is not None and str(v).strip():
                    preserved_ca[(str(row[0]), str(row[1]))] = str(v).strip()

    out: list[list] = []
    seen: set[tuple[str, str]] = set()
    for tup in raw_rows:
        group, tag, path_str, anchor = tup[0], tup[1], tup[2], tup[3]
        tag_id = tup[4] if len(tup) > 4 else None
        key = (group, tag)
        if key in seen:
            raise CategoryMapTxtError(
                f"internal duplicate (group, tag): {key!r}",
                fix=_FIX_DUP,
            )
        seen.add(key)
        segs = _path_segments_or_raise(path_str, line=None, source_line=f"{group!r} / {tag!r} -> {path_str!r}")
        row: list = [group, tag, segs]
        meta: dict = {}
        if anchor:
            meta["anchor_only"] = True
        if tag_id is not None:
            try:
                meta["tag_id"] = int(tag_id)
            except (TypeError, ValueError) as e:
                raise CategoryMapTxtError(
                    f"tag_id must be int-compatible, got {tag_id!r}",
                    fix="Only programmatic tuples may include tag_id; omit in txt.",
                ) from e
        ca_prev = preserved_ca.get((group, tag))
        if ca_prev is not None:
            meta["seven17_ca_id"] = ca_prev
        if meta:
            row.append(meta)
        out.append(row)
    return out


def write_map_from_txt(
    inp: Path | None = None,
    *,
    target: Path | None = None,
) -> tuple[Path, int]:
    """解析 txt 并写入 JSON；返回 (输出路径, 行数)。"""
    src = inp if inp is not None else _DEFAULT_TXT
    text = _read_mapping_txt_file(src)
    raw_rows = parse_category_map_txt(text)
    out_path = target or Path(__file__).resolve().with_name("wecatalog_tag_category_map.json")
    rows = build_json_rows(raw_rows)
    try:
        out_path.write_text(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError as e:
        raise CategoryMapTxtError(
            f"cannot write output JSON: {out_path}: {e}",
            fix="Close editors/locks on wecatalog_tag_category_map.json or pick a writable disk.",
        ) from e
    return out_path, len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="从 txt 生成 wecatalog_tag_category_map.json")
    ap.add_argument(
        "--input",
        type=Path,
        default=None,
        help=f"映射 txt 路径（默认：{_DEFAULT_TXT}）",
    )
    ap.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="额外写入日志文件（UTF-8）",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG")
    args = ap.parse_args()
    configure_module_logging(__name__, log_file=args.log_file, verbose=args.verbose)

    inp = args.input if args.input is not None else _DEFAULT_TXT
    try:
        target, row_count = write_map_from_txt(inp)
    except CategoryMapTxtError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from None

    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "mapbuild.wrote"),
                ("path", str(target)),
                ("rows", row_count),
                ("input", str(inp)),
            ],
            zh="已从 txt 生成并写入分类映射表",
        ),
    )


if __name__ == "__main__":
    main()
