"""上架 / LLM / 抓取 / 微猫 CLI 等共用的日志：时间 [级别] [短标签] 消息，无多余空格。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# 长 logger.name → 短标签（前缀列宽稳定）
_MOD: dict[str, str] = {
    "product_feed_kr.seven17_upload": "upload",
    "product_feed_kr.seven17_category_llm": "category",
    "product_feed_kr.seven17_llm": "llm",
    "product_feed_kr.listing_llm_enrich": "llm",
    "product_feed_kr.store_sqlite": "db",
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

LISTING_LLM_LOGGER_NAME = "product_feed_kr.listing_llm_enrich"
SEVEN17_LLM_LOGGER_NAME = "product_feed_kr.seven17_llm"
STORE_SQLITE_LOGGER_NAME = "product_feed_kr.store_sqlite"

UPLOAD_LOGGER_NAMES: tuple[str, ...] = (
    "product_feed_kr.seven17_upload",
    "product_feed_kr.seven17_category_llm",
    STORE_SQLITE_LOGGER_NAME,
)

LLM_LOGGER_NAMES: tuple[str, ...] = (
    SEVEN17_LLM_LOGGER_NAME,
    LISTING_LLM_LOGGER_NAME,
    STORE_SQLITE_LOGGER_NAME,
)

# 兼容旧名
UPLOAD_LLM_LOGGER_NAMES = UPLOAD_LOGGER_NAMES

SCRAPE_DB_LOGGER_NAMES: tuple[str, ...] = (
    "product_feed_kr.wecatalog_scrape_store",
    STORE_SQLITE_LOGGER_NAME,
)


def configure_llm_logging(
    log_file: Path | None = None,
    *,
    verbose: bool = False,
) -> None:
    """LLM 调度 / enrich / 厂商对比：seven17_llm + listing_llm_enrich + store_sqlite。"""
    for name in LLM_LOGGER_NAMES:
        configure_module_logging(name, log_file=log_file, verbose=verbose)


def configure_pf_stderr(*logger_names: str) -> None:
    """为给定 logger 挂唯一 stderr Handler（幂等）。"""
    for name in logger_names:
        lg = logging.getLogger(name)
        if any(getattr(h, _MARK, False) for h in lg.handlers):
            continue
        h = logging.StreamHandler(sys.stderr)
        h.addFilter(_InjectMod())
        h.setFormatter(PfFormatter())
        h.setLevel(logging.DEBUG)
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
    sh.setLevel(logging.DEBUG)
    setattr(sh, _MARK, True)
    lg.addHandler(sh)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.addFilter(_InjectMod())
        fh.setFormatter(PfFormatter())
        fh.setLevel(logging.DEBUG)
        setattr(fh, _MARK_FILE, True)
        lg.addHandler(fh)
    lg.propagate = False


def configure_scrape_logging(
    log_file: Path | None = None,
    *,
    verbose: bool = False,
    logger_name: str = "product_feed_kr.wecatalog_scrape_store",
) -> None:
    """抓取脚本：scrape + store_sqlite。"""
    names = tuple(dict.fromkeys((logger_name, *SCRAPE_DB_LOGGER_NAMES)))
    for name in names:
        configure_module_logging(name, log_file=log_file, verbose=verbose)


def configure_upload_logging(
    log_file: Path | None = None,
    *,
    verbose: bool = False,
) -> None:
    """上架：seven17_upload + store_sqlite。"""
    for name in UPLOAD_LOGGER_NAMES:
        configure_module_logging(name, log_file=log_file, verbose=verbose)


def pf_trunc(s: Any, max_len: int = 160) -> str:
    t = " ".join(str(s).split())
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


_ITEM_SEP_WIDTH = 72


def log_item_separator(logger: logging.Logger) -> None:
    """每条商品 LLM / 上架处理前打印一行 ``=`` 分隔，便于在终端区分每次操作。"""
    logger.info("%s", "=" * _ITEM_SEP_WIDTH)


def pf_goods_id(rec: dict[str, Any]) -> str:
    """``pf_store_item.goods_id`` 完整值（微猫常为 40 字符，日志勿截断）。"""
    return str(rec.get("goods_id") or "").strip()


def pf_db_row_id_kv(
    rec: dict[str, Any] | None = None,
    *,
    row_id: int | None = None,
) -> list[tuple[str, Any]]:
    """``[DB]`` 日志用：仅输出表主键 ``id``（INTEGER）。"""
    rid = row_id
    if rid is None and rec is not None:
        raw = rec.get("id")
        if raw is not None:
            try:
                rid = int(raw)
            except (TypeError, ValueError):
                rid = None
    if rid is not None:
        return [("id", rid)]
    return []


def pf_store_row_id_kv(
    rec: dict[str, Any] | None = None,
    *,
    album_id: str | None = None,
    goods_id: str | None = None,
    tag_id: int | None = None,
) -> list[tuple[str, Any]]:
    """业务日志用：微猫 ``album_id`` / ``goods_id`` / ``tag_id``（非表主键）。"""
    if rec is not None:
        aid = str(rec.get("album_id") or album_id or "").strip()
        gid = pf_goods_id(rec) if not goods_id else str(goods_id).strip()
        tid_raw = rec.get("tag_id") if tag_id is None else tag_id
    else:
        aid = str(album_id or "").strip()
        gid = str(goods_id or "").strip()
        tid_raw = tag_id
    try:
        tag = int(tid_raw) if tid_raw is not None else 0
    except (TypeError, ValueError):
        tag = 0
    out: list[tuple[str, Any]] = []
    if aid:
        out.append(("album_id", aid))
    if gid:
        out.append(("goods_id", gid))
    out.append(("tag_id", tag))
    return out


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
