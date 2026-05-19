"""进程内常驻循环（单日志文件）；供 scrape / upload / llm 入口默认使用。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from product_feed_kr.pf_log import pf_kv
from product_feed_kr.process_singleton import EXIT_SINGLETON_CONFLICT
from product_feed_kr.seven17_config import EXIT_RESTART_FRESH_DATA


def default_log_path(basename: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    d = root / "data" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return d / f"{basename}_{ts}.log"


def run_forever(
    run_once: Callable[[], int],
    *,
    task_label: str,
    logger: logging.Logger,
    on_restart_fresh: Callable[[], None] | None = None,
    round_delay_sec: float = 0.0,
    fatal_codes: dict[int, float] | None = None,
) -> int:
    """``fatal_codes``：退出码 → 等待秒数后终止进程（不继续循环）。"""
    round_n = 0
    while True:
        round_n += 1
        try:
            code = run_once()
        except KeyboardInterrupt:
            return 130
        if code == EXIT_SINGLETON_CONFLICT:
            return code

        if fatal_codes and code in fatal_codes:
            wait = fatal_codes[code]
            logger.warning(
                "%s",
                pf_kv(
                    [("event", "run.fatal"), ("task", task_label), ("exit", code),
                     ("wait_sec", wait), ("round", round_n)],
                    zh=f"遇到致命退出码 {code}，等待 {wait:.0f}s 后退出",
                ),
            )
            if wait > 0:
                time.sleep(wait)
            return code

        if code == EXIT_RESTART_FRESH_DATA:
            if on_restart_fresh is not None:
                on_restart_fresh()
            logger.info(
                "%s",
                pf_kv(
                    [("event", "run.restart"), ("task", task_label), ("round", round_n)],
                    zh="达阈值，开始下一轮",
                ),
            )
        else:
            logger.info(
                "%s",
                pf_kv(
                    [("event", "run.round_done"), ("task", task_label), ("exit", code), ("round", round_n)],
                    zh="本轮结束",
                ),
            )
        if round_delay_sec > 0:
            logger.info(
                "%s",
                pf_kv(
                    [
                        ("event", "run.round_sleep"),
                        ("task", task_label),
                        ("sec", round_delay_sec),
                        ("round", round_n),
                    ],
                    zh="轮次间隔休眠",
                ),
            )
            time.sleep(round_delay_sec)
