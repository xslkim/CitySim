"""开发期告警通道（08 T-OPS-01；04 §12.1 日志分级 / §12.3 最小监控集 / D1 落文件）。

- API：`alert(level, key, message, context)`；WARN 级同 `key` 15 分钟内聚合为一条（计数合并），
  ERROR 级立即落行；落行为 JSON Lines `{wall_time, level, key, message, context}`。
- 出口：仓库根 `var/logs/alerts.log`（08 文档写"logs/alerts.log"，落点 = 00 §2 var/logs 布局，
  与 logconf 的 var/logs/server.log 同目录——工程对齐，登记 08 偏差表）；gitignored。
- Webhook 到个人 IM defer 上线部署期（D1）；接口预留 sink 替换点。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ALERTS_PATH = Path(__file__).resolve().parents[3] / "var" / "logs" / "alerts.log"
WARN_AGGREGATE_S = 900  # 15 分钟聚合（04 §12.1）
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))

_lock = threading.Lock()
_warn_pending: dict[str, dict[str, Any]] = {}  # key → {first_at, count, message, context}

_path_override: Path | None = None  # 测试注入


def set_path(path: str | Path | None) -> None:
    """告警落点覆盖（测试/演练用；None 恢复默认）。"""
    global _path_override
    with _lock:
        _path_override = Path(path) if path else None
        _warn_pending.clear()


def _alerts_path() -> Path:
    return _path_override or ALERTS_PATH


def _emit(level: str, key: str, message: str, context: dict[str, Any] | None) -> None:
    line = {
        "wall_time": dt.datetime.now(LOCAL_TZ).isoformat(timespec="milliseconds"),
        "level": level, "key": key, "message": message, "context": context or {},
    }
    path = _alerts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def flush_warnings() -> int:
    """把聚合窗内 WARN 落盘（计数合并）；返回落盘条数。由调用方/测试在窗口边界触发。"""
    with _lock:
        pending = dict(_warn_pending)
        _warn_pending.clear()
    for key, agg in pending.items():
        msg = f"{agg['message']}（15 分钟内聚合 ×{agg['count']}，04 §12.1）"
        _emit("WARN", key, msg, agg["context"])
    return len(pending)


def alert(level: str, key: str, message: str, context: dict[str, Any] | None = None) -> None:
    """统一告警入口：ERROR 立即落行；WARN 同 key 15 分钟聚合（flush_warnings 落盘）。"""
    level = level.upper()
    if level in ("ERROR", "CRITICAL"):
        flush_warnings()  # 时序保持：立即落行前先落聚合 WARN（04 §12.1 分级并存）
    if level == "WARN":
        with _lock:
            agg = _warn_pending.setdefault(key, {"count": 0, "message": message,
                                                 "context": context or {}})
            agg["count"] += 1
        log.warning("告警聚合[%s] %s", key, message)
        return
    _emit(level, key, message, context)
    (log.error if level == "ERROR" else log.info)("告警[%s] %s", key, message)
