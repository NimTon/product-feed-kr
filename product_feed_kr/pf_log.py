"""上架 / LLM / 抓取 / 微猫 CLI 等共用的日志：时间 [级别] [短标签] 消息，无多余空格。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# 长 logger.name → 短标签（前缀列宽稳定）
_MOD: dict[str, str] = {
    "product_feed_kr.seven17_upload": "upload",
    "product_feed_kr.listing_llm_enrich": "llm",
    "product_feed_kr.wecatalog_scrape_store": "scrape",
    "product_feed_kr.wecatalog_fetch_tags": "tags",
    "product_feed_kr.wecatalog_tag_category_map_apply_seven17": "map17",
    "product_feed_kr.wecatalog_tag_category_map_builder": "mapbuild",
}


class _InjectMod(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        short = _MOD.get(record.name, record.name.rpartition(".")[2] or record.name or "?")
        record.pf_tag = short.upper()
        return True


class PfFormatter(logging.Formatter):
    _LEVEL_CN = {
        "DEBUG": "调试",
        "INFO": "信息",
        "WARNING": "警告",
        "ERROR": "错误",
        "CRITICAL": "严重",
    }

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname_cn)s] [%(pf_tag)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        record.levelname_cn = self._LEVEL_CN.get(record.levelname, record.levelname)
        return super().format(record)


_MARK = "_pf_stderr"
_MARK_FILE = "_pf_filelog"


def configure_pf_stderr(*logger_names: str) -> None:
    """为给定 logger 挂唯一 stderr Handler（幂等）。"""
    for name in logger_names:
        lg = logging.getLogger(name)
        if any(getattr(h, _MARK, False) for h in lg.handlers):
            continue
        h = logging.StreamHandler(sys.stderr)
        h.addFilter(_InjectMod())
        h.setFormatter(PfFormatter())
        setattr(h, _MARK, True)
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False


def configure_module_logging(
    logger_name: str,
    *,
    log_file: Path | None = None,
    verbose: bool = False,
) -> None:
    """单模块：stderr + 可选 UTF-8 文件；格式与 upload/llm 一致；verbose 时 DEBUG。"""
    lg = logging.getLogger(logger_name)
    lg.handlers.clear()
    lg.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stderr)
    sh.addFilter(_InjectMod())
    sh.setFormatter(PfFormatter())
    setattr(sh, _MARK, True)
    lg.addHandler(sh)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.addFilter(_InjectMod())
        fh.setFormatter(PfFormatter())
        setattr(fh, _MARK_FILE, True)
        lg.addHandler(fh)
    lg.propagate = False


def configure_scrape_logging(
    log_file: Path | None = None,
    *,
    verbose: bool = False,
    logger_name: str = "product_feed_kr.wecatalog_scrape_store",
) -> None:
    """抓取脚本：同 `configure_module_logging`。"""
    configure_module_logging(logger_name, log_file=log_file, verbose=verbose)


def pf_trunc(s: Any, max_len: int = 160) -> str:
    t = " ".join(str(s).split())
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def pf_kv(pairs: list[tuple[str, Any]], *, val_max: int = 220, zh: str | None = None) -> str:
    """空格分隔 `k=v`，便于复制与 grep；跳过 None。

    ``zh`` 非空时在最前插入 ``说明=…``，便于终端阅读；原有键（如 ``event``）保留便于 grep。
    """
    if zh:
        pairs = [("说明", zh), *pairs]
    out: list[str] = []
    for k, v in pairs:
        if v is None:
            continue
        if isinstance(v, bool):
            sv = "1" if v else "0"
        elif isinstance(v, (int, float)):
            sv = str(v)
        else:
            sv = pf_trunc(v, val_max)
        if not sv and v != "" and v is not False and v != 0:
            continue
        out.append(f"{k}={sv}")
    return " ".join(out)
