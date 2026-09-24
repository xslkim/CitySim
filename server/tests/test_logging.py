"""T-ENV-06 logging 配置验收：级别消费 / 格式串 / handler 去向 / 幂等。"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys

import pytest

from worldsim import logconf


@pytest.fixture(autouse=True)
def _clean_logging():
    """每用例隔离 root logger 与 logconf 模块状态。"""
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    old_flag = logconf._configured
    root.handlers.clear()
    logconf._configured = False
    yield
    root.handlers.clear()
    root.handlers.extend(old_handlers)
    root.setLevel(old_level)
    logconf._configured = old_flag


def test_default_level_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WSIM_LOG_LEVEL", raising=False)
    logconf.setup()
    assert logging.getLogger().level == logging.INFO


def test_debug_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSIM_LOG_LEVEL", "DEBUG")
    logconf.setup()
    assert logging.getLogger().level == logging.DEBUG


def test_invalid_level_falls_back_info_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSIM_LOG_LEVEL", "verbose")
    probe: list[logging.LogRecord] = []

    class _Probe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            probe.append(record)

    child = logging.getLogger("worldsim.logconf")
    child.addHandler(_Probe())
    try:
        logconf.setup()
    finally:
        child.handlers.clear()
    assert logging.getLogger().level == logging.INFO
    assert any(r.levelno == logging.WARNING and "WSIM_LOG_LEVEL" in r.getMessage() for r in probe)


def test_format_string_fields() -> None:
    assert logconf.LOG_FORMAT == "%(asctime)s %(levelname)s %(name)s %(message)s"
    logconf.setup()
    formatter = logging.getLogger().handlers[0].formatter
    record = logging.LogRecord("worldsim.test", logging.INFO, __file__, 1, "hello", (), None)
    out = formatter.format(record)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}\+08:00 INFO worldsim\.test hello", out)


def test_handler_destinations() -> None:
    logconf.setup()
    handlers = logging.getLogger().handlers
    stdout_h = [h for h in handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    file_h = [h for h in handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    assert len(stdout_h) == 1 and stdout_h[0].stream is sys.stdout
    assert len(file_h) == 1
    assert file_h[0].baseFilename.endswith(("var/logs/server.log", "var\\logs\\server.log"))
    assert file_h[0].when == "MIDNIGHT"
    assert file_h[0].encoding == "utf-8"


def test_setup_idempotent_no_duplicate_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSIM_LOG_LEVEL", "INFO")
    logconf.setup()
    logconf.setup()
    logconf.setup()
    assert len(logging.getLogger().handlers) == 2
    # 重复调用仍可更新级别
    monkeypatch.setenv("WSIM_LOG_LEVEL", "DEBUG")
    logconf.setup()
    assert logging.getLogger().level == logging.DEBUG
    assert len(logging.getLogger().handlers) == 2
