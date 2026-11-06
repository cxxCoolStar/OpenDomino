"""setup_file_logging 的 characterization 测试：钉住行为契约，防重构跑偏。

契约来自原 main.py 内联实现：
- 调用后 cwd 下创建 logs/ 目录
- root logger 新增一个 RotatingFileHandler，指向 logs/opendomino.log
- maxBytes=5MB、backupCount=5、encoding=utf-8、Formatter 格式串固定
"""
from __future__ import annotations

import logging
import logging.handlers

import pytest


@pytest.fixture
def clean_root_logger():
    """摘除并复原 root logger 的 handler，避免测试之间互相污染。"""
    root = logging.getLogger()
    original = root.handlers[:]
    for h in original:
        root.removeHandler(h)
    yield
    for h in root.handlers[:]:
        root.removeHandler(h)
    for h in original:
        root.addHandler(h)


def test_setup_file_logging_creates_logs_dir(tmp_path, monkeypatch, clean_root_logger):
    from app.log_setup import setup_file_logging

    monkeypatch.chdir(tmp_path)
    setup_file_logging()

    assert (tmp_path / "logs").is_dir()


def test_setup_file_logging_attaches_rotating_file_handler(tmp_path, monkeypatch, clean_root_logger):
    from app.log_setup import setup_file_logging

    monkeypatch.chdir(tmp_path)
    setup_file_logging()

    root = logging.getLogger()
    rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    handler = rotating[0]
    assert handler.baseFilename.endswith("opendomino.log")
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 5
    assert handler.encoding == "utf-8"


def test_setup_file_logging_formatter_format(tmp_path, monkeypatch, clean_root_logger):
    from app.log_setup import setup_file_logging

    monkeypatch.chdir(tmp_path)
    setup_file_logging()

    handler = next(
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert handler.formatter._fmt == "%(asctime)s %(levelname)s %(name)s %(message)s"
