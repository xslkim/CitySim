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
DEFAULT_SILENT_MINUTES = 30  # R3 #5：决策静默线（模拟分钟；agent 清醒且非 batch 时段口径）

# 角色行为事件口径（与观测端 fetch_stall / 内核看门狗同口径，三处一致才不误报）
AGENT_EVENT_LIKE = "type LIKE 'agent.%' OR type LIKE 'dialogue.%' OR type LIKE 'social.%'"


class DecisionSilentProbe:
    """R3 #5 探针 v2：决策活性维度（"脑死亡不报警"盲区修复）。

    旧 world_stalled 只看 watermark 推进——时钟走、零事件零 LLM 零 ERROR 时探针恒 false
    （round3 实案：batch 后裁决循环挂起 55 分钟，看门狗 55 分钟后才凑够 30 tick 阈值）。
    判定：非 batch 段且 agent 清醒（作息表外）时，`clock.current_tick - 最近角色行为事件
    tick` 折模拟分钟 > silent_minutes → decision_silent：kernel.log WARN + alerts.log 聚合
    留痕 + world_state `world.stalled` 标记（stalled/reason/since_tick）。**只报告不动作**
    （probe 误报前科见 G 项裁决；自动拉起/降级决策 backlog）。
    睡眠窗/batch 段的静默是正常口径 → 清除标记不告警。
    """

    STALL_KEY = "world.stalled"

    def __init__(
        self,
        pool: Any,
        *,
        clock: Any,  # TimeEngine 鸭子类型：current_tick/batch_mode/now_sim
        is_quiet_fn: Callable[[dt.datetime], bool],  # 作息安静判定（main 注入 needs_engine.is_sleeping）
        silent_minutes: int = DEFAULT_SILENT_MINUTES,
        on_stall: Callable[[str], Awaitable[None]] | None = None,  # 附加通道（测试断言用）
    ) -> None:
        self._pool = pool
        self._clock = clock
        self._is_quiet = is_quiet_fn
        self._silent_minutes = int(silent_minutes)
        self._on_stall = on_stall
        self.stalled = False
        self.stalled_reason: str | None = None

    async def _set_marker(self, stalled: bool, reason: str | None, since_tick: int | None) -> None:
        import json as _json
        await self._pool.execute(
            """
            INSERT INTO world_state (key, value, updated_tick, updated_at)
            VALUES ($1, $2::jsonb, $3, now())
            ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_tick=$3, updated_at=now()
            """,
            self.STALL_KEY, _json.dumps({
                "stalled": stalled, "reason": reason, "since_tick": since_tick,
                "checked_at": dt.datetime.now(LOCAL_TZ).isoformat()}, ensure_ascii=False),
            int(self._clock.current_tick))

    async def check_once(self) -> dict[str, Any]:
        """探测一轮：返回 {stalled, reason, gap_ticks}；首次越线 WARN + 标记，恢复 INFO + 清标。"""
        sim_now = self._clock.now_sim()
        if self._clock.batch_mode or self._is_quiet(sim_now):
            if self.stalled:  # 安静时段静默属正常口径（睡眠/batch）→ 清除
                self.stalled = False
                self.stalled_reason = None
                log.info("决策活性探针：进入安静时段（睡眠/batch），清除 decision_silent 标记")
                await self._set_marker(False, None, None)
            return {"stalled": False, "reason": None, "gap_ticks": 0}
        agent_tick = await self._pool.fetchval(
            f"SELECT max(tick) FROM events WHERE {AGENT_EVENT_LIKE}")
        gap = int(self._clock.current_tick) - int(agent_tick or 0)
        gap_min = gap * 5  # tick = 模拟 5 分钟（04 §2.2）
        if gap_min > self._silent_minutes:
            reason = (f"decision_silent：agent 清醒且非 batch 时段，已 {gap} tick"
                      f"（≈{gap_min} 模拟分钟）无角色行为事件（阈值 {self._silent_minutes} 模拟分钟）")
            if not self.stalled:
                self.stalled = True
                self.stalled_reason = reason
                log.warning("决策活性探针：%s → world_stalled=true（reason=decision_silent）", reason)
                alert("WARN", "world.decision_silent", reason,
                      {"gap_ticks": gap, "since_tick": int(agent_tick or 0)})
                if self._on_stall is not None:
                    await self._on_stall(reason)
            await self._set_marker(True, "decision_silent", int(agent_tick or 0))
            return {"stalled": True, "reason": self.stalled_reason, "gap_ticks": gap}
        if self.stalled:
            self.stalled = False
            self.stalled_reason = None
            log.info("决策活性探针：角色行为恢复（gap=%d tick）→ 清除 decision_silent", gap)
        await self._set_marker(False, None, None)
        return {"stalled": False, "reason": None, "gap_ticks": gap}

    async def run(self, stop: asyncio.Event) -> None:  # pragma: no cover - 常驻循环
        while not stop.is_set():
            try:
                await self.check_once()
            except Exception:  # noqa: BLE001
                log.exception("决策活性探针异常（下轮继续）")
            await asyncio.sleep(PROBE_INTERVAL_S)


LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


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
