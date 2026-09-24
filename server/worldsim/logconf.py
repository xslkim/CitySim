"""全项目统一 logging 初始化入口（T-ENV-06；01 文档 §6 D11 工程新增模块）。

口径：
- 单处 `logging.config.dictConfig` 定义；其余模块只 `logging.getLogger(__name__)`，禁止各处自行 `basicConfig`。
- 级别读 `WSIM_LOG_LEVEL`（04 §1.6；缺省 INFO；非法值回落 INFO 并 warning 留痕）。
- 格式全项目统一：`%(asctime)s %(levelname)s %(name)s %(message)s`，asctime 带 +08:00 时区偏移，UTF-8。
- 去向：默认 stdout + 文件 `var/logs/server.log`（仓库根，gitignored）按日轮转。
- 告警分流（alerts.log）与审计留痕归 08 T-OPS-01/T-AUD 系列，本模块只提供分级底座。
"""

from __future__ import annotations

import datetime as _dt
import logging
import logging.config
import logging.handlers
import os
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_TZ_CST = _dt.timezone(_dt.timedelta(hours=8))  # Asia/Shanghai，与 DB timezone 一致（§6 D4）

_LOG_DIR = Path(__file__).resolve().parents[2] / "var" / "logs"
_configured = False


class ShanghaiFormatter(logging.Formatter):
    """asctime 带 +08:00 时区偏移（如 2026-09-24 10:00:00,123+08:00）。"""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = _dt.datetime.fromtimestamp(record.created, _TZ_CST)
        base = dt.strftime(datefmt) if datefmt else dt.strftime("%Y-%m-%d %H:%M:%S")
        return f"{base},{int(record.msecs):03d}+08:00"


def _resolve_level() -> tuple[str, str | None]:
    """返回 (生效级别, 非法原值或 None)。"""
    raw = os.environ.get("WSIM_LOG_LEVEL", "INFO").strip().upper()
    if raw not in _VALID_LEVELS:
        return "INFO", raw
    return raw, None


def _build_config(level: str) -> dict:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "std": {
                "()": "worldsim.logconf.ShanghaiFormatter",
                "format": LOG_FORMAT,
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "formatter": "std",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "formatter": "std",
                "filename": str(_LOG_DIR / "server.log"),
                "when": "midnight",
                "backupCount": 30,
                "encoding": "utf-8",
                "utc": False,
            },
        },
        "root": {"level": level, "handlers": ["stdout", "file"]},
    }


def setup() -> None:
    """初始化全项目 logging；重复调用幂等（只更新级别，不重复挂 handler）。"""
    global _configured
    level, invalid = _resolve_level()
    if _configured:
        logging.getLogger().setLevel(level)
    else:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        logging.config.dictConfig(_build_config(level))
        _configured = True
    if invalid is not None:
        logging.getLogger(__name__).warning(
            "WSIM_LOG_LEVEL=%r 非法，回落 INFO（合法值：%s）",
            os.environ.get("WSIM_LOG_LEVEL"),
            "/".join(sorted(_VALID_LEVELS - {"NOTSET"})),
        )
