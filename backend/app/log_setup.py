"""文件日志配置：RotatingFileHandler 落盘 + 轮转。

任务失败往往只有日志能定位（如 #dccfa430 空响应事故），前台进程日志必须落盘：
控制台照常输出，文件 5MB 轮转保 5 份（约 25MB 上限）。
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_file_logging() -> None:
    """挂一个 5MB×5 的 RotatingFileHandler 到 root logger。"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "opendomino.log", maxBytes=5 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
