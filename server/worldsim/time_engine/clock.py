"""时钟引擎：锚点换算、暂停/恢复、故障追平、batch 段、tick 主循环（02 T-TIME-01/02/03；04 §3.2/§3.3）。

- 锚点持久化在 `world_state` 键 `clock.anchor`：`{anchor_sim, anchor_wall, ratio, tick0_sim, paused_at?}`。
  continuous 段 `sim_time = anchor_sim + (wall_now − anchor_wall) × ratio`；跨段先把旧段积分进
  `anchor_sim` 再换比（`set_ratio`，04 §3.2）。
- 暂停写 `paused_at`，恢复时新锚点 `anchor_wall=now`、`anchor_sim` 不变——暂停期间模拟时间不流动。
- tick = 模拟 5 分钟（04 §2.2），网格绝对对齐：`sim_of_tick(n) = tick0_sim + n×300s`。
  认知排程/冷却/衰减只读 sim_time（00 §4 红线 11），本模块一切排程接口只出 sim_time 域。
- 进程重启从 `world_state` 锚点恢复（04 §12.2）；seed 冷启动占位锚点（updated_tick=0）首启重锚：
  保留叙事起点 `anchor_sim`、`anchor_wall` 换为当前真实时刻。
- 追平两档（04 §3.2）：停机 <2 真实小时 → `min(ratio×2, max_catchup_ratio)` 连跑；
  ≥2 真实小时 → 落后部分按 batch 模式补齐（`batch.batch_advance`，不追赶黄金档 1:1 对齐）。
  全程落 `time.paused/resumed/catchup.start/catchup.end/batch_advanced`（source/trigger=system，
  visibility=internal，payload 逐字按 06 §1.2 / config/event_types.yaml）。
- 依赖方向单向（04 §2.1）：本模块不依赖业务模块；batch 摘要器/编剧插队均注入。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .speed_table import Segment, SpeedTable, SpeedTableReloader

log = logging.getLogger(__name__)

TICK_SIM_SECONDS = 300  # tick = 模拟 5 分钟（04 §2.2，00 §4 红线 10）
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))  # Asia/Shanghai，与 DB timezone 一致
ANCHOR_KEY = "clock.anchor"

# 追平换挡线：停机 <2 真实小时走连跑档，≥2 走 batch 档（04 §3.2 逐字）
CATCHUP_CONTINUOUS_MAX_WALL = dt.timedelta(hours=2)

WallNowFn = Callable[[], dt.datetime]
SleepFn = Callable[[float], Awaitable[None]]


@dataclass
class _Anchor:
    anchor_sim: dt.datetime
    anchor_wall: dt.datetime
    ratio: float
    tick0_sim: dt.datetime
    paused_at: dt.datetime | None = None


@dataclass(frozen=True)
class CatchupPlan:
    """追平计划（04 §3.2 两档）。"""

    mode: str               # 'continuous' | 'batch'
    from_sim: dt.datetime
    to_sim: dt.datetime
    ratio: float | None = None        # continuous 档追平压缩比
    sim_hours: float | None = None    # batch 档补齐时长


def _iso(t: dt.datetime) -> str:
    return t.astimezone(LOCAL_TZ).isoformat()


class TimeEngine:
    """模拟时钟。`pool` = asyncpg pool；`wall_now`/`sleep` 可注入（测试确定性）。"""

    def __init__(
        self,
        pool: Any,
        speed_table: SpeedTable,
        *,
        wall_now: WallNowFn | None = None,
        unthrottled: bool = False,
    ) -> None:
        self._pool = pool
        self._st = speed_table
        self._wall_now: WallNowFn = wall_now or (lambda: dt.datetime.now(LOCAL_TZ))
        self._unthrottled = unthrottled
        self._a: _Anchor | None = None
        self._batch_mode = False

    # ---- 启动 / 锚点 ----------------------------------------------------

    @classmethod
    async def start(cls, pool: Any, speed_table: SpeedTable, **kw: Any) -> "TimeEngine":
        eng = cls(pool, speed_table, **kw)
        await eng._load_or_init_anchor()
        return eng

    async def _load_or_init_anchor(self) -> None:
        row = await self._pool.fetchrow("SELECT value, updated_tick FROM world_state WHERE key=$1", ANCHOR_KEY)
        now = self._wall_now().astimezone(LOCAL_TZ)
        if row is None:
            # 无锚点（裸 schema 库）：以当前真实时刻为叙事起点建锚
            self._a = _Anchor(anchor_sim=now, anchor_wall=now, ratio=self._segment_ratio(now) or 1.0, tick0_sim=now)
            await self._persist_anchor()
            log.info("clock.anchor 初始化：anchor_sim=%s", _iso(now))
            return
        raw = row["value"]
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
        anchor = _Anchor(
            anchor_sim=dt.datetime.fromisoformat(value["anchor_sim"]).astimezone(LOCAL_TZ),
            anchor_wall=dt.datetime.fromisoformat(value["anchor_wall"]).astimezone(LOCAL_TZ),
            ratio=float(value["ratio"]),
            tick0_sim=dt.datetime.fromisoformat(value["tick0_sim"]).astimezone(LOCAL_TZ)
            if "tick0_sim" in value
            else dt.datetime.fromisoformat(value["anchor_sim"]).astimezone(LOCAL_TZ),
            paused_at=dt.datetime.fromisoformat(value["paused_at"]).astimezone(LOCAL_TZ)
            if value.get("paused_at")
            else None,
        )
        self._a = anchor
        if row["updated_tick"] == 0:
            # seed 冷启动占位（README：内核首启重锚）——保留叙事起点 anchor_sim，锚定当前真实时刻
            anchor.anchor_wall = now
            ratio = self._segment_ratio(now)
            if ratio is not None:
                anchor.ratio = ratio
            await self._persist_anchor()
            log.info("clock.anchor 首启重锚：anchor_sim=%s anchor_wall=%s", _iso(anchor.anchor_sim), _iso(now))
        else:
            log.info("clock.anchor 重启恢复：anchor_sim=%s ratio=%s", _iso(anchor.anchor_sim), anchor.ratio)

    async def _persist_anchor(self) -> None:
        a = self._require_anchor()
        value = {
            "anchor_sim": _iso(a.anchor_sim),
            "anchor_wall": _iso(a.anchor_wall),
            "ratio": a.ratio,
            "tick0_sim": _iso(a.tick0_sim),
        }
        if a.paused_at is not None:
            value["paused_at"] = _iso(a.paused_at)
        await self._pool.execute(
            """
            INSERT INTO world_state (key, value, updated_tick) VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_tick=$3
            """,
            ANCHOR_KEY, json.dumps(value, ensure_ascii=False), self.current_tick,
        )

    def _require_anchor(self) -> _Anchor:
        if self._a is None:
            raise RuntimeError("TimeEngine 未 start()")
        return self._a

    def _segment_ratio(self, wall: dt.datetime) -> float | None:
        seg = self._st.segment_at_min(wall.astimezone(LOCAL_TZ).hour * 60 + wall.astimezone(LOCAL_TZ).minute)
        return seg.ratio if seg.mode == "continuous" else None

    # ---- 换算（T-TIME-01） ----------------------------------------------

    def now_sim(self) -> dt.datetime:
        """当前模拟时刻；暂停期间不流动（冻结在 paused_at 对应的 sim）。"""
        a = self._require_anchor()
        wall = a.paused_at if a.paused_at is not None else self._wall_now().astimezone(LOCAL_TZ)
        return a.anchor_sim + (wall - a.anchor_wall) * a.ratio

    def tick_of(self, sim_time: dt.datetime) -> int:
        """sim_time → tick 序号（tick = 模拟 5 分钟，网格绝对对齐）。"""
        a = self._require_anchor()
        return int((sim_time - a.tick0_sim).total_seconds() // TICK_SIM_SECONDS)

    def sim_of_tick(self, tick: int) -> dt.datetime:
        a = self._require_anchor()
        return a.tick0_sim + dt.timedelta(seconds=tick * TICK_SIM_SECONDS)

    @property
    def current_tick(self) -> int:
        return self.tick_of(self.now_sim())

    @property
    def paused(self) -> bool:
        return self._require_anchor().paused_at is not None

    @property
    def batch_mode(self) -> bool:
        return self._batch_mode

    @property
    def ratio(self) -> float:
        return self._require_anchor().ratio

    # ---- 换比 / 暂停 / 恢复 ----------------------------------------------

    async def set_ratio(self, new_ratio: float) -> None:
        """跨段积分换比：先把旧段积分进 anchor_sim，再换比（04 §3.2）。"""
        a = self._require_anchor()
        sim_now = self.now_sim()
        a.anchor_sim = sim_now
        a.anchor_wall = (a.paused_at if a.paused_at is not None else self._wall_now().astimezone(LOCAL_TZ))
        a.ratio = float(new_ratio)
        await self._persist_anchor()

    async def pause(self, reason: str) -> None:
        """致命依赖故障暂停：先把冻结点 sim 积分进 anchor_sim，再写 paused_at（04 §3.2 step1）。幂等。"""
        a = self._require_anchor()
        if a.paused_at is not None:
            return
        frozen = self.now_sim()
        a.anchor_sim = frozen
        a.paused_at = self._wall_now().astimezone(LOCAL_TZ)
        a.anchor_wall = a.paused_at
        await self._persist_anchor()
        await self.emit_time_event("time.paused", {"reason": reason, "at_sim": _iso(self.now_sim())})
        log.warning("时钟暂停：%s（at_sim=%s）", reason, _iso(self.now_sim()))

    async def resume(self, reason: str) -> tuple[dt.timedelta, dt.datetime]:
        """恢复：新锚点 anchor_wall=now、anchor_sim 不变。返回 (停机时长, 计划 sim 位置)。"""
        a = self._require_anchor()
        if a.paused_at is None:
            return dt.timedelta(0), self.now_sim()
        now = self._wall_now().astimezone(LOCAL_TZ)
        downtime = now - a.paused_at
        planned = self._planned_sim(a.paused_at, now)
        a.anchor_wall = now
        a.paused_at = None
        ratio = self._segment_ratio(now)
        if ratio is not None:
            a.ratio = ratio
        await self._persist_anchor()
        await self.emit_time_event("time.resumed", {"reason": reason, "at_sim": _iso(self.now_sim())})
        log.info("时钟恢复：停机 %.1fs，计划位置 %s", downtime.total_seconds(), _iso(planned))
        return downtime, planned

    def _planned_sim(self, w0: dt.datetime, w1: dt.datetime) -> dt.datetime:
        """无故障情形下到 w1 的计划 sim 位置：暂停点 sim + 停机期各段 sim 推进量积分。"""
        planned = self.now_sim()  # 暂停中 = 暂停点 sim
        cursor = w0
        sim_seconds = 0.0
        while cursor < w1:
            seg = self._st.segment_at_min(cursor.hour * 60 + cursor.minute)
            boundary = cursor.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=seg.end_min)
            seg_end = min(boundary, w1)
            hours = (seg_end - cursor).total_seconds() / 3600
            if seg.mode == "continuous":
                sim_seconds += hours * 3600 * float(seg.ratio)  # type: ignore[arg-type]
            else:
                # batch 段：每次进入段结算一次 sim_hours_per_run，按段时长比例摊入积分
                span = (seg.end_min - seg.start_min) / 3600
                sim_seconds += hours / span * float(seg.sim_hours_per_run) * 3600  # type: ignore[arg-type]
            cursor = seg_end
        return planned + dt.timedelta(seconds=sim_seconds)

    # ---- 追平（T-TIME-03） ------------------------------------------------

    def plan_catchup(self, downtime: dt.timedelta, planned_sim: dt.datetime) -> CatchupPlan:
        """两档换挡（04 §3.2）：<2 真实小时连跑档；≥2 真实小时 batch 档。"""
        a = self._require_anchor()
        from_sim = self.now_sim()
        if downtime < CATCHUP_CONTINUOUS_MAX_WALL:
            ratio = min(a.ratio * 2, self._st.max_catchup_ratio)
            return CatchupPlan(mode="continuous", from_sim=from_sim, to_sim=planned_sim, ratio=ratio)
        lag_hours = max(0.0, (planned_sim - from_sim).total_seconds() / 3600)
        return CatchupPlan(mode="batch", from_sim=from_sim, to_sim=planned_sim, sim_hours=lag_hours)

    async def begin_catchup(self, plan: CatchupPlan) -> None:
        await self.emit_time_event(
            "time.catchup.start",
            {"mode": plan.mode, "from_sim": _iso(plan.from_sim), "to_sim": _iso(plan.to_sim)},
        )
        if plan.mode == "continuous":
            await self.set_ratio(plan.ratio)  # type: ignore[arg-type]
        log.info("追平开始：mode=%s %s → %s", plan.mode, _iso(plan.from_sim), _iso(plan.to_sim))

    async def finish_catchup(self, plan: CatchupPlan) -> None:
        if plan.mode == "continuous":
            ratio = self._segment_ratio(self._wall_now()) or self._require_anchor().ratio
            await self.set_ratio(ratio)
        await self.emit_time_event(
            "time.catchup.end",
            {"mode": plan.mode, "from_sim": _iso(plan.from_sim), "to_sim": _iso(plan.to_sim)},
        )
        log.info("追平结束：mode=%s at_sim=%s", plan.mode, _iso(self.now_sim()))

    async def run_catchup(
        self,
        plan: CatchupPlan,
        *,
        agent_ids: tuple[str, ...] = (),
        summarize: Callable[[str, float], Awaitable[None]] | None = None,
        drive: Callable[[CatchupPlan], Awaitable[None]] | None = None,
    ) -> None:
        """追平闭环：start →（连跑驱动 / batch 补齐）→ end。连跑档由调用方注入 drive 驱动 tick。"""
        await self.begin_catchup(plan)
        try:
            if plan.mode == "batch":
                if summarize is None:
                    raise ValueError("batch 档追平需要 summarize 摘要器（04 §3.2/§3.3）")
                from .batch import batch_advance  # 局部导入避免环（batch 不 import 本模块）

                await batch_advance(self, agent_ids, plan.sim_hours or 0.0, summarize=summarize)
            elif drive is not None:
                await drive(plan)
        finally:
            await self.finish_catchup(plan)

    # ---- batch 段 ----------------------------------------------------------

    async def enter_batch_mode(self, sim_hours: float) -> None:
        """进入 batch 段：暂停 tick 排程，锚点一次性推进 sim_hours（04 §3.3 enter_batch_mode）。"""
        a = self._require_anchor()
        self._batch_mode = True
        a.anchor_sim = self.now_sim() + dt.timedelta(hours=sim_hours)
        a.anchor_wall = self._wall_now().astimezone(LOCAL_TZ)
        await self._persist_anchor()

    async def exit_batch_mode(self) -> None:
        """离开 batch 段：重锚（清漂移），恢复 tick 排程（04 §3.3 exit_batch_mode）。"""
        a = self._require_anchor()
        a.anchor_sim = self.now_sim()
        a.anchor_wall = self._wall_now().astimezone(LOCAL_TZ)
        self._batch_mode = False
        await self._persist_anchor()

    # ---- 事件 ----------------------------------------------------------

    async def emit_time_event(self, type_: str, payload: dict[str, Any]) -> int:
        """落 time.* 系统事件（source/trigger=system、visibility=internal，06 §1.2 注册表口径）。"""
        sim_now = self.now_sim()
        return await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
            VALUES ($1, $2, $3, 'system', 'system', 'internal', $4::jsonb)
            RETURNING seq
            """,
            self.tick_of(sim_now), sim_now, type_, json.dumps(payload, ensure_ascii=False),
        )

    # ---- tick 主循环（04 §2.2 clock.run） ---------------------------------

    async def run(
        self,
        *,
        on_tick: Callable[[int], None],
        stop: asyncio.Event,
        target_sim: dt.datetime | None = None,
        sleep: SleepFn | None = None,
        drain_event: asyncio.Event | None = None,
        on_enter_batch: Callable[[float], None] | None = None,
        reloader: SpeedTableReloader | None = None,
    ) -> None:
        """时钟主循环：paced（按变速表节拍）或 unthrottled（试跑模式，尽快递 tick）。

        - 暂停/batch 段内不排程新 tick（04 §3.1/§3.3）。
        - 真实时刻跨段边界：continuous 段间仅换比（积分进 anchor_sim）；进入 batch 段回调
          `on_enter_batch(sim_hours_per_run)` 一次（由裁决协程消费后驱动 batch_advance）。
        - SIGHUP 热更经 `reloader` 在段边界生效（04 §12.4）；unthrottled 试跑模式不做段切换。
        """
        sleep_fn: SleepFn = sleep or asyncio.sleep  # type: ignore[assignment]
        last_seg_start: int | None = None
        batch_signalled = False
        try:
            while not stop.is_set():
                if target_sim is not None and self.now_sim() >= target_sim:
                    break
                if self.paused or self._batch_mode:
                    await sleep_fn(0.05 if self._unthrottled else 0.5)
                    continue
                wall = self._wall_now().astimezone(LOCAL_TZ)
                seg = self._st.segment_at_min(wall.hour * 60 + wall.minute)
                if not self._unthrottled:
                    if last_seg_start is not None and seg.start_min != last_seg_start:
                        batch_signalled = False
                        if reloader is not None and reloader.maybe_reload_at_boundary():
                            self._st = reloader.current
                            seg = self._st.segment_at_min(wall.hour * 60 + wall.minute)
                        if seg.mode == "continuous":
                            await self.set_ratio(seg.ratio)  # type: ignore[arg-type]  # 跨段积分换比
                    if seg.mode == "batch":
                        if not batch_signalled and on_enter_batch is not None:
                            on_enter_batch(float(seg.sim_hours_per_run))  # type: ignore[arg-type]
                            batch_signalled = True
                        await sleep_fn(0.5)  # batch 段暂停 tick 排程（04 §3.1）
                        last_seg_start = seg.start_min
                        continue
                last_seg_start = seg.start_min
                if not self._unthrottled and seg.ratio is not None and seg.ratio != self.ratio:
                    await self.set_ratio(seg.ratio)
                next_tick = self.current_tick + 1
                next_sim = self.sim_of_tick(next_tick)
                if not self._unthrottled:
                    ratio = self.ratio
                    while not stop.is_set():
                        lag = (next_sim - self.now_sim()).total_seconds()
                        if lag <= 0:
                            break
                        await sleep_fn(min(0.5, lag / ratio if ratio > 0 else lag))
                await self._advance_to(next_sim)
                on_tick(next_tick)
                if drain_event is not None:
                    drain_event.clear()
                    await drain_event.wait()
        finally:
            await self._persist_anchor()

    async def _advance_to(self, sim_time: dt.datetime) -> None:
        """网格绝对对齐推进：anchor_sim 钉到目标网格（消除 paced 漂移）。"""
        a = self._require_anchor()
        a.anchor_sim = sim_time
        a.anchor_wall = self._wall_now().astimezone(LOCAL_TZ)
        await self._persist_anchor()
