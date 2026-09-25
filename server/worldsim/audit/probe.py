"""致命故障探测与暂停/恢复编排（08 T-OPS-03；04 §3.2 三档流程 / §12.2 runbook 故障表）。

- 探测循环每 30s（04 §3.2）：主库 = 轻量 `SELECT 1`；全 provider 熔断 = 网关 breaker 全开
  （03 侧 FailoverBreaker 接口注入）；磁盘 = 数据目录所在卷可用空间（阈值配置项占位，
  `models.yaml thresholds.disk.pause_free_pct`，默认 5%——08 偏差表 D4 登记；"磁盘 >80%" 是
  告警线不等同暂停线）。
- 命中即经注入的 `pause_cb` 走 TIME 的 `clock.pause` 路径（本模块不直接写 paused_at，D8）；
  恢复后 `resume_cb`（TIME 侧追平两档）。暂停/恢复/追平事件由 TIME 侧写（02 文档）。
- 告警经 T-OPS-01 `alerts.alert`（ERROR 立即，04 §12.1）。
- 24h 降速升级消费（04 §8.4 升级线）：`consume_throttle_escalation` 读 03 判级器
  `ThrottleState.needs_clock_pause` 标志位 → 同一 pause_cb 路径。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..audit.alerts import alert

log = logging.getLogger(__name__)

PROBE_INTERVAL_S = 30.0  # 04 §3.2 探测循环
DEFAULT_DISK_PAUSE_FREE_PCT = 5.0  # 磁盘暂停线默认 5%（08 D4 工程默认；告警线 80% 见 §12.3 另口径）


async def consume_throttle_escalation(
    state: Any, pause_cb: Callable[[str], Awaitable[None]], *,
    reason: str = "成本熔断降速 24 真实小时未恢复（04 §8.4 升级线）",
) -> bool:
    """24h 降速升级消费（T-OPS-02 编排 → T-OPS-03 通道）：标志位置位 → clock.pause 一次。"""
    if getattr(state, "needs_clock_pause", False):
        alert("ERROR", "cost.throttle_24h", reason, {"level": getattr(state, "level", "?")})
        await pause_cb(reason)
        return True
    return False


class FatalProbe:
    """三类致命故障探测器（主库/全 provider 熔断/磁盘满）。"""

    def __init__(
        self,
        pool: Any,
        *,
        pause_cb: Callable[[str], Awaitable[None]],
        resume_cb: Callable[[str], Awaitable[None]] | None = None,
        providers_ok: Callable[[], bool] | None = None,  # None = 不检查（mock 模式无 provider 维度）
        disk_path: str | None = None,
        disk_pause_free_pct: float = DEFAULT_DISK_PAUSE_FREE_PCT,
        interval_s: float = PROBE_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        disk_usage: Callable = shutil.disk_usage,
    ) -> None:
        self._pool = pool
        self._pause_cb = pause_cb
        self._resume_cb = resume_cb
        self._providers_ok = providers_ok
        self._disk_path = disk_path or os.path.expanduser("~/pgsql/data")
        self._disk_pause_free_pct = disk_pause_free_pct
        self.interval_s = interval_s
        self._sleep = sleep
        self._disk_usage = disk_usage
        self.paused = False
        self.active_faults: list[str] = []

    async def _db_ok(self) -> bool:
        try:
            return int(await self._pool.fetchval("SELECT 1")) == 1
        except Exception:  # noqa: BLE001 - 任何异常即主库不可用
            return False

    async def check_once(self) -> list[str]:
        """探测一轮：返回故障清单；首个故障即触发 pause，全清触发 resume。"""
        faults: list[str] = []
        if not await self._db_ok():
            faults.append("db")
        if self._providers_ok is not None and not self._providers_ok():
            faults.append("providers")
        try:
            usage = self._disk_usage(self._disk_path)
            free_pct = usage.free / usage.total * 100
            if free_pct < self._disk_pause_free_pct:
                faults.append("disk")
        except FileNotFoundError:
            faults.append("disk")
        self.active_faults = faults
        if faults and not self.paused:
            self.paused = True
            reason = f"致命故障暂停：{','.join(faults)}（04 §3.2）"
            alert("ERROR", f"probe.{faults[0]}", reason, {"faults": faults})
            await self._pause_cb(reason)
        elif not faults and self.paused:
            self.paused = False
            log.info("故障清除 → 恢复（04 §3.2 追平档由 TIME 执行）")
            if self._resume_cb is not None:
                await self._resume_cb("故障清除恢复")
        return faults

    async def run(self, stop: asyncio.Event) -> None:  # pragma: no cover - 常驻循环
        """30s 探测循环（04 §3.2）。"""
        while not stop.is_set():
            try:
                await self.check_once()
            except Exception:  # noqa: BLE001
                log.exception("探测异常（下轮继续）")
            await self._sleep(self.interval_s)
