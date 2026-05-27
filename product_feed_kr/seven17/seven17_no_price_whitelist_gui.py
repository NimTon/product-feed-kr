"""无价格白名单 GUI：按 map.txt 分类勾选并写回 seven17 配置。"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from product_feed_kr.common.seven17_config import seven17_config_path
from product_feed_kr.wecatalog.wecatalog_tag_category_map_builder import parse_category_map_txt

_KEY = "SEVEN17_NO_PRICE_ALLOW_CATEGORIES"


def _map_txt_path() -> Path:
    from product_feed_kr._paths import REPO_ROOT

    return REPO_ROOT / "config" / "wecatalog_tag_category_map.txt"


def _load_map_items() -> list[tuple[str, str]]:
    txt_path = _map_txt_path()
    rows = parse_category_map_txt(txt_path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for group, tag, _path, _anchor in rows:
        g = str(group or "").strip()
        t = str(tag or "").strip()
        if g and t:
            out.append((g, t))
    return out


def _split_specs(raw: str) -> list[str]:
    s = str(raw or "").strip()
    if not s:
        return []
    out: list[str] = []
    cur: list[str] = []
    for ch in s:
        if ch in ",，;\n":
            spec = "".join(cur).strip()
            if spec:
                out.append(spec)
            cur = []
            continue
        cur.append(ch)
    spec = "".join(cur).strip()
    if spec:
        out.append(spec)
    return out


def _parse_pair_spec(spec: str) -> tuple[str, str] | None:
    s = str(spec or "").strip()
    if not s:
        return None
    for sep in ("->", ">", "｜", "|", "/", "／", "＞"):
        if sep in s:
            left, right = s.split(sep, 1)
            g = left.strip()
            t = right.strip()
            if g and t:
                return g, t
            return None
    return None


def _load_current_selected_pairs(
    map_pairs: list[tuple[str, str]],
    cfg_value: str,
) -> set[tuple[str, str]]:
    pairs = set(map_pairs)
    selected: set[tuple[str, str]] = set()
    for spec in _split_specs(cfg_value):
        pair = _parse_pair_spec(spec)
        if pair is not None:
            if pair in pairs:
                selected.add(pair)
            continue
        # 兼容旧写法：仅 tag 名。
        tag = spec.strip()
        if not tag:
            continue
        for g, t in map_pairs:
            if t == tag:
                selected.add((g, t))
    return selected


def _load_config_data() -> tuple[Path, dict]:
    cfg_path = seven17_config_path()
    data: dict = {}
    if cfg_path.is_file():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
    return cfg_path, data


def _save_config_value(path: Path, data: dict, selected_pairs: list[tuple[str, str]]) -> None:
    data[_KEY] = ",".join(f"{g}>{t}" for g, t in selected_pairs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_gui() -> int:
    try:
        map_pairs = _load_map_items()
    except Exception as e:
        messagebox.showerror("读取失败", f"无法读取 map.txt 分类：\n{e}")
        return 1
    cfg_path, cfg_data = _load_config_data()
    current_raw = str(cfg_data.get(_KEY) or "").strip()
    selected_pairs = _load_current_selected_pairs(map_pairs, current_raw)

    root = tk.Tk()
    root.title("无价格白名单分类开关")
    root.geometry("980x680")

    top = ttk.Frame(root, padding=12)
    top.pack(fill="x")
    ttk.Label(top, text=f"配置文件: {cfg_path}").pack(anchor="w")
    ttk.Label(top, text=f"分类来源: {_map_txt_path()}").pack(anchor="w")
    ttk.Label(
        top,
        text="说明：仅对白名单中的 (分组, 标签) 在“无价格”时放行；其余分类无价一律跳过。",
    ).pack(anchor="w", pady=(6, 0))

    ctrl = ttk.Frame(root, padding=(12, 0, 12, 0))
    ctrl.pack(fill="x")
    search_var = tk.StringVar()
    ttk.Label(ctrl, text="搜索").pack(side="left")
    search_entry = ttk.Entry(ctrl, textvariable=search_var, width=45)
    search_entry.pack(side="left", padx=(8, 10))

    canvas_wrap = ttk.Frame(root, padding=12)
    canvas_wrap.pack(fill="both", expand=True)
    canvas = tk.Canvas(canvas_wrap, highlightthickness=0)
    vsb = ttk.Scrollbar(canvas_wrap, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")
    canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_configure(_event: tk.Event) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event: tk.Event) -> None:
        canvas.itemconfigure(canvas_window, width=event.width)

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)

    vars_by_pair: dict[tuple[str, str], tk.BooleanVar] = {}
    for g, t in map_pairs:
        v = tk.BooleanVar(value=(g, t) in selected_pairs)
        vars_by_pair[(g, t)] = v

    row_widgets: list[tuple[tk.Widget, tuple[str, str]]] = []

    def _render_rows(filter_text: str = "") -> None:
        for w, _ in row_widgets:
            w.destroy()
        row_widgets.clear()
        flt = filter_text.strip().lower()
        shown = 0
        for g, t in map_pairs:
            label = f"[{g}] {t}"
            if flt and flt not in label.lower():
                continue
            chk = ttk.Checkbutton(inner, text=label, variable=vars_by_pair[(g, t)])
            chk.pack(anchor="w", fill="x", padx=4, pady=2)
            row_widgets.append((chk, (g, t)))
            shown += 1
        count_var.set(f"已显示 {shown} / 总计 {len(map_pairs)}")

    def _select_visible(val: bool) -> None:
        for _w, pair in row_widgets:
            vars_by_pair[pair].set(val)
        _update_selected_count()

    def _update_selected_count(*_args: object) -> None:
        c = sum(1 for v in vars_by_pair.values() if v.get())
        selected_var.set(f"已勾选 {c} 项")

    def _save() -> None:
        selected = [pair for pair in map_pairs if vars_by_pair[pair].get()]
        try:
            _save_config_value(cfg_path, cfg_data, selected)
        except Exception as e:
            messagebox.showerror("保存失败", f"写入配置失败：\n{e}")
            return
        messagebox.showinfo("保存成功", f"已保存 {_KEY}（{len(selected)} 项）")

    btns = ttk.Frame(root, padding=(12, 0, 12, 12))
    btns.pack(fill="x")
    ttk.Button(btns, text="全选（当前显示）", command=lambda: _select_visible(True)).pack(side="left")
    ttk.Button(btns, text="全不选（当前显示）", command=lambda: _select_visible(False)).pack(side="left", padx=8)
    ttk.Button(btns, text="保存", command=_save).pack(side="right")

    selected_var = tk.StringVar()
    count_var = tk.StringVar()
    status = ttk.Frame(root, padding=(12, 0, 12, 12))
    status.pack(fill="x")
    ttk.Label(status, textvariable=count_var).pack(side="left")
    ttk.Label(status, textvariable=selected_var).pack(side="right")

    def _on_search(*_args: object) -> None:
        _render_rows(search_var.get())
        _update_selected_count()

    search_var.trace_add("write", _on_search)
    search_entry.focus_set()

    _render_rows()
    _update_selected_count()
    root.mainloop()
    return 0


def main() -> int:
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
