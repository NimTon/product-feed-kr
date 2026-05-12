"""把 ``data/seven17_ca_options.json`` 里 ``ca_id`` 下拉的 label→value 写入
``wecatalog_tag_category_map.json`` 各行的 ``meta.seven17_ca_id``。

路径对齐规则：把映射表里韩文路径 ``["a","b","c"]`` 连成 ``a > b > c``，与后台 option 文案完全一致则写入对应 value。
少数与后台拼写不一致的（分组, 标签）在 ``_MANUAL_CA_ID`` 里写死 ca_id。

用法::

  python -m product_feed_kr.wecatalog_tag_category_map_apply_seven17
  python -m product_feed_kr.wecatalog_tag_category_map_apply_seven17 --dump path/to/seven17_ca_options.json
  python -m product_feed_kr.wecatalog_tag_category_map_apply_seven17 --dry-run -v --log-file data/map17.log
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from product_feed_kr.pf_log import configure_module_logging, pf_kv

_log = logging.getLogger(__name__)

# (分组, 标签) → seven17 ca_id（与 itemform 下拉 value 一致）
_MANUAL_CA_ID: dict[tuple[str, str], str] = {
    ("女鞋专区", "Roger Vivier 女鞋"): "203020",  # 로저비비에（映射表误写 로저비베）
    ("女鞋专区", "罗意威（女鞋）"): "203070",  # 로에비（女鞋 luxury）
    ("Belt专区", "BV Belt"): "5010",  # 보테가 베네타（映射路径误写 발렌시아가）
    ("Belt专区", "YSL Belt"): "50g0",  # 이브생로랑
    ("女士包专区", "바렌티노"): "3070",  # 바렌티노
    ("手表专区", "Franck Muller"): "4010",  # 프랑크 뮬러
    ("手表专区", "卡地亚（AF공장）"): "4020",
    ("手表专区", "HUBLOT（系列）"): "4070",  # 우블롯
    ("New Balance专区", "1000"): "202060",  # 后台无「1000」子类，用 뉴발란스 父级
    ("香水专区", "圣罗兰（香水）"): "80c0",  # 생로랑
    ("乔丹", "AJ1"): "20204030",  # 에어 조던 1 하이
    ("adidas专区", "Pharrell x AD Adistar Jellyfish"): "20202040",  # 젤리피쉬
    ("女装专区", "ALai (女装)"): "102070",  # 알라이
    ("여성옷", "몽클레어"): "a0d0",  # 패딩 > 몽클레르（여성의류下无该类）
    ("여성옷", "ALai (女装)"): "102070",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_label_to_value(dump_path: Path) -> dict[str, str]:
    data = json.loads(dump_path.read_text(encoding="utf-8"))
    selects = data.get("selects") or {}
    opts = selects.get("ca_id")
    if not isinstance(opts, list):
        raise ValueError("dump JSON 缺少 selects.ca_id 数组")
    out: dict[str, str] = {}
    for o in opts:
        if not isinstance(o, dict):
            continue
        lab = (o.get("label") or "").strip()
        val = str(o.get("value") or "").strip()
        if not val or not lab:
            continue
        out[lab] = val
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="按 seven17 分类 dump 写入 map 的 seven17_ca_id")
    ap.add_argument(
        "--dump",
        type=Path,
        default=None,
        help="seven17_dump_itemform_categories 生成的 JSON（默认 data/seven17_ca_options.json）",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印统计，不写回 map",
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

    root = _project_root()
    dump_path = args.dump or (root / "data" / "seven17_ca_options.json")
    map_path = Path(__file__).resolve().with_name("wecatalog_tag_category_map.json")

    if not dump_path.is_file():
        _log.error(
            "%s",
            pf_kv(
                [("event", "map17.error"), ("reason", "dump_not_found"), ("path", str(dump_path))],
                zh="未找到 seven17 分类 dump 文件",
            ),
        )
        return 1

    label_to_v = _load_label_to_value(dump_path)
    rows = json.loads(map_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        _log.error(
            "%s",
            pf_kv(
                [("event", "map17.error"), ("reason", "map_root_not_array"), ("path", str(map_path))],
                zh="映射表 JSON 根不是数组",
            ),
        )
        return 1

    hit_exact = 0
    hit_manual = 0
    missing: list[tuple[str, str, str]] = []

    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        g, t, path = row[0], row[1], row[2]
        if not isinstance(g, str) or not isinstance(t, str) or not isinstance(path, list):
            continue
        joined = " > ".join(str(x) for x in path)
        key = (g, t)
        if key in _MANUAL_CA_ID:
            ca = _MANUAL_CA_ID[key]
            hit_manual += 1
        elif joined in label_to_v:
            ca = label_to_v[joined]
            hit_exact += 1
        else:
            missing.append((g, t, joined))
            continue

        meta: dict = row[3] if len(row) > 3 and isinstance(row[3], dict) else {}
        if len(row) > 3 and not isinstance(row[3], dict):
            _log.error(
                "%s",
                pf_kv(
                    [
                        ("event", "map17.error"),
                        ("reason", "row_meta_not_dict"),
                        ("group", g),
                        ("tag", t),
                    ],
                    zh="某行第四列 meta 不是对象",
                ),
            )
            return 1
        meta = dict(meta)
        meta["seven17_ca_id"] = ca
        row[:] = [g, t, path, meta] if meta else [g, t, path]

    if missing:
        _log.error("%s", pf_kv([("event", "map17.missing"), ("n", len(missing))], zh="有行未能匹配后台分类文案，未写 ca_id"))
        for item in missing:
            _log.warning("%s", pf_kv([("event", "map17.missing_row"), ("row", repr(item))], zh="未匹配的一行"))
        return 2

    if args.dry_run:
        _log.info(
            "%s",
            pf_kv(
                [
                    ("event", "map17.dry_run"),
                    ("exact", hit_exact),
                    ("manual", hit_manual),
                    ("rows", len(rows)),
                ],
                zh="仅 dry-run：统计命中数，未写回文件",
            ),
        )
        return 0

    map_path.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    _log.info(
        "%s",
        pf_kv(
            [
                ("event", "map17.wrote"),
                ("path", str(map_path)),
                ("exact", hit_exact),
                ("manual", hit_manual),
            ],
            zh="已写回 wecatalog_tag_category_map.json（含 seven17_ca_id）",
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
