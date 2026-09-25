"""日历引擎：作息/节假日判定 + 跨日批量结算单入口（04 T-WA-02；04 §3.3/§2.1；01 §1.4/§6.1）。

- 判定 API（全模块共用）：`is_workday / is_weekend / is_holiday / holiday_flags / current_segment`，
  只读 sim_time（00 §4 红线 11），参数全部来自 `world.yaml`（T-WA-01 追加段），代码零硬编码阈值。
- 跨日结算单入口 `settle_span(from_sim, to_sim)`：按注册回调表（`register_job`，各 WA 任务注册
  payroll/bill/stock/绩效等）以 (fire_time, 注册序) 顺序结算落区间内的全部排程项；连续段每 tick
  经 `tick()` 驱动、凌晨 batch 段经 `run_batch_calendar()`（04 §3.3，挂 batch 钩子注册表唯一挂载点）
  驱动——两路径同一判定，进度游标落 `world_state`（`calendar.last_settled`），重启不重不漏。
- 日界翻转恰落一条 `time.day_summary`，payload 仅 `{day}`（day = 自世界纪元模拟日序号，
  纪元 = 首次观测 sim 日期，存 `world_state` `calendar.epoch_date`；触发时点为工程默认 D-04），
  `source='system'`、`trigger='system'`（06 §1.2；生产方 = 本模块日界钩子，06 v1.2 注）。
- 8:00 打卡为**内部出勤状态结算，不产事件**（04 §3.3 v1.2 注，D-03 转正）：出勤标记落
  `world_state` `attendance.<YYYY-MM-DD>`，供 T-WA-07 绩效评分（D-10）与校验器消费。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

LAST_SETTLED_KEY = "calendar.last_settled"
EPOCH_KEY = "calendar.epoch_date"

# 作息段键（与 world.yaml schedule.workday 段一致；周末/节假日返回 "free"）
SEGMENT_KEYS = ("morning", "work_am", "lunch", "work_pm", "evening", "sleep")


def _parse_hhmm(s: str) -> tuple[int, int]:
    hh, mm = str(s).split(":")
    return int(hh), int(mm)


def _at(date_: dt.date, hhmm: str, tz: dt.tzinfo) -> dt.datetime:
    h, m = _parse_hhmm(hhmm)
    if h == 24:  # "24:00" = 次日 00:00
        return dt.datetime.combine(date_, dt.time(0, 0), tzinfo=tz) + dt.timedelta(days=1)
    return dt.datetime.combine(date_, dt.time(h, m), tzinfo=tz)


@dataclass(frozen=True)
class CalendarJob:
    """跨日排程项：日期谓词命中且当日 `at` 时刻落入结算区间即触发一次。"""

    name: str
    at: str                                    # "HH:MM"
    on: Callable[[dt.date], bool]              # 日期谓词（读 sim 日期）
    fn: Callable[["CalendarEngine", dt.datetime], Awaitable[None]]


class CalendarEngine:
    """模拟日历判定与跨日结算。`clock` 鸭子类型取 TimeEngine 能力（now_sim/tick_of/current_tick）。"""

    def __init__(self, pool: Any, world_cfg: dict[str, Any], clock: Any, *,
                 agg: Any = None, grader: Any = None) -> None:
        self._pool = pool
        self._cfg = world_cfg
        self._clock = clock
        self._agg = agg
        self._grader = grader
        self._jobs: list[CalendarJob] = []
        self._last_settled: dt.datetime | None = None
        self._holidays = self._expand_holidays(world_cfg.get("holidays", {}))

    # ---- 访问器（各 WA 结算模块消费） ------------------------------------------
    @property
    def pool(self) -> Any:
        return self._pool

    @property
    def cfg(self) -> dict[str, Any]:
        return self._cfg

    @property
    def clock(self) -> Any:
        return self._clock

    @property
    def agg(self) -> Any:
        return self._agg

    # ---- 内部：节假日展开 ---------------------------------------------------

    @staticmethod
    def _expand_holidays(holidays_cfg: dict[str, Any]) -> dict[dt.date, dict[str, Any]]:
        """把 MM-DD+days 表展开为 {date: entry}（跨年以模拟日期的年份锚定；区间跨年末顺延）。"""
        out: dict[dt.date, dict[str, Any]] = {}
        for entry in holidays_cfg.get("table", []):
            mm, dd = (int(x) for x in entry["start"].split("-"))
            for year in range(2020, 2100):  # 模拟日历覆盖区间（工程默认；锚点 2026 前后足够）
                try:
                    start = dt.date(year, mm, dd)
                except ValueError:
                    continue
                for i in range(int(entry["days"])):
                    out.setdefault(start + dt.timedelta(days=i), entry)
        return out

    # ---- 判定 API（供调度器/校验器/股价/扰动/职场日历共用） ---------------------

    def holiday_of(self, date_: dt.date) -> dict[str, Any] | None:
        return self._holidays.get(date_)

    def is_holiday(self, t: dt.date | dt.datetime) -> bool:
        d = t.date() if isinstance(t, dt.datetime) else t
        return d in self._holidays

    def is_weekend(self, t: dt.date | dt.datetime) -> bool:
        d = t.date() if isinstance(t, dt.datetime) else t
        return d.weekday() >= 5

    def is_workday(self, t: dt.date | dt.datetime) -> bool:
        d = t.date() if isinstance(t, dt.datetime) else t
        return not self.is_weekend(d) and not self.is_holiday(d)

    def holiday_flags(self, t: dt.date | dt.datetime) -> dict[str, Any]:
        """节假日生效标记：休市（T-WA-05）/工作事件停发（T-WA-07/08）/社交邀约权重上调（02 消费）。"""
        d = t.date() if isinstance(t, dt.datetime) else t
        entry = self._holidays.get(d)
        rules = (self._cfg.get("holidays") or {}).get("rules") or {}
        active = entry is not None
        return {
            "is_holiday": active,
            "name": (entry or {}).get("name"),
            "market_closed": bool(active and rules.get("market_closed")),
            "work_events_suspended": bool(active and rules.get("work_events_suspended")),
            "social_invite_weight": float(rules.get("social_invite_weight", 1.0)) if active else 1.0,
            "confess_weight": float((entry or {}).get("confess_weight") or 1.0),
        }

    def current_segment(self, sim_now: dt.datetime) -> str:
        """当前作息时段键（01 §1.4）；非工作日返回 'free'。sleep 段跨零点归属当日凌晨。"""
        if not self.is_workday(sim_now):
            return "free"
        workday = (self._cfg.get("schedule") or {}).get("workday") or {}
        minutes = sim_now.hour * 60 + sim_now.minute
        for key in SEGMENT_KEYS:
            window = workday.get(key)
            if not window:
                continue
            (sh, sm), (eh, em) = _parse_hhmm(window[0]), _parse_hhmm(window[1])
            start, end = sh * 60 + sm, (eh * 60 + em) % (24 * 60) if (eh, em) == (24, 0) else eh * 60 + em
            if start < end:
                if start <= minutes < end:
                    return key
            elif minutes >= start or minutes < end:  # 跨零点段（sleep 0:30~6:30）
                return key
        return "free"

    # ---- 通用事件落库（world/system 域；ui.grade 初值经 Grader，04 §6.6） ----------

    async def insert_event(self, *, type_: str, payload: dict[str, Any], sim_time: dt.datetime,
                           source: str = "world", trigger: str = "world",
                           actors: list[str] | None = None, visibility: str = "public",
                           rng_seed: int | None = None, arc_id: str | None = None,
                           location_id: str | None = None,
                           grade_signals: dict[str, bool] | None = None) -> int:
        """world_agent 域事件唯一落库径（本模块内）。payload 键逐字按 06 §1.2 / event_types.yaml。"""
        tick = self._clock.tick_of(sim_time)
        seed = tick if rng_seed is None else rng_seed
        actors = list(actors or [])
        ui: dict[str, Any] | None = None
        if self._grader is not None:
            sig = grade_signals or {}
            ui = {"grade": await self._grader.grade(
                type_=type_, actors=actors, payload=payload, sim_now=sim_time,
                rel_hit=bool(sig.get("rel_hit")), mood_hit=bool(sig.get("mood_hit")),
                followups=bool(sig.get("followups")),
            )}
        return await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, arc_id, location_id, actors, rng_seed, visibility, payload, ui)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb)
            RETURNING seq
            """,
            tick, sim_time, type_, source, trigger, arc_id, location_id, actors, seed, visibility,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(ui, ensure_ascii=False) if ui else None,
        )

    # ---- world_state KV 工具（D-06 持久化位置） --------------------------------

    async def get_state(self, key: str, default: Any = None) -> Any:
        row = await self._pool.fetchrow("SELECT value FROM world_state WHERE key=$1", key)
        if row is None:
            return default
        value = row["value"]
        return json.loads(value) if isinstance(value, str) else value

    async def set_state(self, key: str, value: Any) -> None:
        await self._pool.execute(
            """
            INSERT INTO world_state (key, value, updated_tick) VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_tick=$3, updated_at=now()
            """,
            key, json.dumps(value, ensure_ascii=False), self._clock.current_tick,
        )

    # ---- 排程注册与跨日结算 ----------------------------------------------------

    def register_job(self, name: str, at: str, on: Callable[[dt.date], bool],
                     fn: Callable[["CalendarEngine", dt.datetime], Awaitable[None]]) -> None:
        """注册跨日排程项（同名拒绝重复；回调按 (fire_time, 注册序) 执行，T-WA-02 验收 2）。"""
        if any(j.name == name for j in self._jobs):
            raise ValueError(f"日历排程项 {name!r} 已注册（禁止重复）")
        _parse_hhmm(at)
        self._jobs.append(CalendarJob(name=name, at=at, on=on, fn=fn))
        log.info("日历排程注册：%s @ %s（共 %d 项）", name, at, len(self._jobs))

    @property
    def jobs(self) -> tuple[CalendarJob, ...]:
        return tuple(self._jobs)

    async def _ensure_epoch(self, sim_now: dt.datetime) -> dt.date:
        epoch_raw = await self.get_state(EPOCH_KEY)
        if epoch_raw is None:
            epoch = sim_now.date()
            await self.set_state(EPOCH_KEY, epoch.isoformat())
            return epoch
        return dt.date.fromisoformat(str(epoch_raw))

    async def _last(self, sim_now: dt.datetime) -> dt.datetime:
        if self._last_settled is None:
            raw = await self.get_state(LAST_SETTLED_KEY)
            self._last_settled = dt.datetime.fromisoformat(str(raw)) if raw else sim_now
        return self._last_settled

    async def _emit_day_summary(self, fire_time: dt.datetime) -> int:
        """日界翻转恰一条 time.day_summary（D-04；payload 仅 {day}，06 §1.2）。"""
        epoch = await self._ensure_epoch(fire_time)
        day = (fire_time.date() - epoch).days + 1
        return await self.insert_event(
            type_="time.day_summary", payload={"day": day}, sim_time=fire_time,
            source="system", trigger="system", visibility="internal",
        )

    async def settle_span(self, from_sim: dt.datetime, to_sim: dt.datetime) -> list[str]:
        """结算落区间 (from_sim, to_sim] 的全部排程项；日界翻转（00:00 过线）恰一条 day_summary。

        返回已执行排程项名列表（含 'time.day_summary' 隐式日界项），执行序 = 时间序 + 注册序。
        """
        if to_sim <= from_sim:
            return []
        tz = to_sim.tzinfo
        fired: list[str] = []
        # 收集 (fire_time, 注册序, 名称/回调)
        pending: list[tuple[dt.datetime, int, str, Any]] = []
        day = from_sim.date()
        while day <= to_sim.date():
            midnight = dt.datetime.combine(day, dt.time(0, 0), tzinfo=tz)
            if from_sim < midnight <= to_sim:
                pending.append((midnight, -1, "time.day_summary", None))
            for order, job in enumerate(self._jobs):
                if not job.on(day):
                    continue
                fire = _at(day, job.at, tz)
                if from_sim < fire <= to_sim:
                    pending.append((fire, order, job.name, job.fn))
            day += dt.timedelta(days=1)
        pending.sort(key=lambda x: (x[0], x[1]))
        for fire_time, _, name, fn in pending:
            if name == "time.day_summary":
                await self._emit_day_summary(fire_time)
            else:
                log.debug("日历排程触发：%s @ %s", name, fire_time.isoformat())
                await fn(self, fire_time)
            fired.append(name)
        await self.set_state(LAST_SETTLED_KEY, to_sim.isoformat())
        self._last_settled = to_sim
        return fired

    async def tick(self, sim_now: dt.datetime | None = None) -> list[str]:
        """连续段每 tick 驱动（main after_tick 挂接）：结算上次进度至 now 的跨度。"""
        now = sim_now or self._clock.now_sim()
        return await self.settle_span(await self._last(now), now)

    async def mark_settled(self, sim_time: dt.datetime) -> None:
        """游标直接推进到 sim_time 而不触发任何排程（内核启动对齐/测试隔离用）。"""
        await self.set_state(LAST_SETTLED_KEY, sim_time.isoformat())
        self._last_settled = sim_time

    async def run_batch_calendar(self, ctx: Any) -> None:
        """凌晨 batch 段入口（04 §3.3；挂 batch 钩子注册表唯一挂载点）。"""
        now = ctx.clock.now_sim()
        await self.settle_span(await self._last(now), now)

    # ---- 8:00 打卡：内部出勤状态结算，不产事件（04 §3.3 v1.2 注，D-03 转正） ----------

    def register_core_jobs(self) -> None:
        """注册日历引擎自带排程项（当前仅 8:00 打卡；payroll/stock 等由各 WA 任务注册）。"""
        self.register_job("attendance.checkin", "08:00", self.is_workday, self._settle_checkin)

    async def is_on_leave(self, agent_id: str, date_: dt.date) -> bool:
        """请假标记判定（`world_state` `leave.<agent_id>` = {"until": "YYYY-MM-DD"}，T-WA-09 illness 写入）。"""
        raw = await self.get_state(f"leave.{agent_id}")
        if not raw:
            return False
        try:
            return dt.date.fromisoformat(str(raw["until"])) >= date_
        except (KeyError, ValueError):
            return False

    async def _settle_checkin(self, engine: "CalendarEngine", fire_time: dt.datetime) -> None:
        """工作日 8:00 出勤状态结算：请假（illness 生效期）→ 'leave'，否则 'present'；不落事件。"""
        rows = await self._pool.fetch("SELECT id FROM agents ORDER BY id")
        marks: dict[str, str] = {}
        for r in rows:
            aid = r["id"]
            marks[aid] = "leave" if await self.is_on_leave(aid, fire_time.date()) else "present"
        await self.set_state(f"attendance.{fire_time.date().isoformat()}", marks)
