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
                 agg: Any = None, grader: Any = None, gateway: Any = None) -> None:
        self._pool = pool
        self._cfg = world_cfg
        self._clock = clock
        self._agg = agg
        self._grader = grader
        self._gateway = gateway
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

    @property
    def gateway(self) -> Any:
        return self._gateway

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


# ---- 裁员传闻规则触发（T-WA-06；01 §6.1 规则触发口径；06 §1.2 world.layoff_rumor） ----

RUMOR_CONSUMED_KEY = "layoff_rumor.consumed"  # world_state：已消费的股票 tick seq（防跨周末重复触发）
RUMOR_BOOST_KEY = "layoff_rumor.gossip_boost"  # gossip 意图权重上调标记（02 文档决策侧消费）


async def settle_layoff_rumor(cal: "CalendarEngine", fire_time: dt.datetime) -> int | None:
    """每日触发时点判定：星澜科技最近一个交易日 `r` 跌破阈值（读 triggers.layoff_rumor，
    01 §6.1 持有）→ 落 `world.layoff_rumor`（payload `{drop_pct, scope}` 逐字 06 §1.2，
    `source='world'`、`trigger='world'` 不占干预率；无冷却逐日判定，D-08 已登记）。

    效果结算（01 §6.1）：全公司 gossip 意图权重上调标记（窗口配置化，02 决策侧消费）+
    成就需求全员变更落 `state.needs_delta`（trigger='system'、cause=本事件 seq）。
    """
    cfg = cal.cfg["triggers"]["layoff_rumor"]
    symbol = str(cfg["symbol"])
    row = await cal.pool.fetchrow(
        """
        SELECT seq, payload FROM events WHERE type='economy.stock.tick' AND payload->>'symbol'=$1
          AND sim_time < $2 ORDER BY seq DESC LIMIT 1
        """, symbol, fire_time)
    if row is None:
        return None
    consumed = await cal.get_state(RUMOR_CONSUMED_KEY)
    if consumed is not None and int(consumed) >= int(row["seq"]):
        return None  # 该跌幅已出过传闻（跨周末/连续扫描不重复；D-08 口径=每个过线交易日一条）
    payload = row["payload"]
    r = float((json.loads(payload) if isinstance(payload, str) else dict(payload))["r"])
    threshold = float(cfg["drop_pct_threshold"])
    if r >= -threshold / 100.0:
        return None
    drop_pct = round(-r * 100.0, 2)
    seq = await cal.insert_event(
        type_="world.layoff_rumor",
        payload={"drop_pct": drop_pct, "scope": str(cfg["scope"]),
                 "text_display": f"{symbol} 单日大跌 {drop_pct}%，裁员传闻四起"},
        sim_time=fire_time, rng_seed=cal.clock.tick_of(fire_time),
    )
    await cal.set_state(RUMOR_CONSUMED_KEY, int(row["seq"]))
    # gossip 意图权重上调标记（持续时长配置化；02 文档决策侧消费）
    until = fire_time.date() + dt.timedelta(days=int(cfg["gossip_window_days"]))
    await cal.set_state(RUMOR_BOOST_KEY, {
        "multiplier": float(cfg["gossip_weight_multiplier"]), "until": until.isoformat(),
        "for_event": str(seq),
    })
    # 成就需求全员变更（trigger='system'、cause=本事件 seq，04 §6.5）
    if cal.agg is not None:
        for r2 in await cal.pool.fetch("SELECT id FROM agents ORDER BY id"):
            await cal.agg.apply_needs_delta(
                agent_id=r2["id"], need="achievement", delta=float(cfg["achievement_delta"]),
                cause=str(seq))
    log.warning("裁员传闻触发：%s 跌幅 %.2f%% 超阈值 %.1f%%（seq=%d）", symbol, drop_pct, threshold, seq)
    return seq


def register_layoff_rumor_job(cal: "CalendarEngine") -> None:
    """注册裁员传闻每日判定（触发时点读 triggers.layoff_rumor.trigger_time，01 §6.1 镜像）。"""
    trigger_time = str(cal.cfg["triggers"]["layoff_rumor"]["trigger_time"])

    async def _judge(c: "CalendarEngine", t: dt.datetime) -> None:
        await settle_layoff_rumor(c, t)

    cal.register_job("world.layoff_rumor", trigger_time, lambda d: True, _judge)


# ---- 职场日历（T-WA-07；01 §6.1/§1.3/§1.5/§1.6；06 §1.2 world.promotion_window/world.perf_review） ----

LAYOFF_POOL_KEY = "layoff_pool"        # world_state：连续 2C 裁员候选池（编剧弧线 A2/A6 读取，01 §1.5）
STREAK_C_KEY = "perf_review.streak_c"  # world_state：连续 C 计数 {agent_id: n}


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> dt.date:
    """某月第 nth 个 weekday（Python weekday 口径）。"""
    d = dt.date(year, month, 1)
    while d.weekday() != weekday:
        d += dt.timedelta(days=1)
    return d + dt.timedelta(days=7 * (nth - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> dt.date:
    if month == 12:
        d = dt.date(year, 12, 31)
    else:
        d = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    while d.weekday() != weekday:
        d -= dt.timedelta(days=1)
    return d


async def settle_promotion_window(cal: "CalendarEngine", fire_time: dt.datetime) -> list[int]:
    """`world.promotion_window`：每部门恰一条，payload `{dept, slots, candidates[]}` 逐字 06 §1.2
    （无 defense_at 键，D-01 转正）；candidates = 该部门全体 P1（设计未定义，工程默认 D-09）；
    答辩排期（窗口月第 3 个周六 19:00，01 §1.6）经 `world.announce` body 承载。"""
    cfg = cal.cfg["triggers"]["promotion_window"]
    slots = int(cfg["slots_per_dept"])
    defense_at = _nth_weekday_of_month(
        fire_time.year, fire_time.month, int(cfg["defense"]["weekday"]), int(cfg["defense"]["nth"]))
    defense_time = str(cfg["defense"]["time"])
    seqs: list[int] = []
    for dept in cal.cfg["company"]["departments"]:
        candidates = [
            r["id"] for r in await cal.pool.fetch(
                "SELECT id FROM agents WHERE department=$1 AND job_title='P1' ORDER BY id", dept["name"])
        ]
        seq = await cal.insert_event(
            type_="world.promotion_window",
            payload={"dept": dept["name"], "slots": slots, "candidates": candidates},
            sim_time=fire_time, actors=list(candidates), rng_seed=cal.clock.tick_of(fire_time),
        )
        seqs.append(seq)
    # 答辩排期公告（world.announce body 承载，D-01；弧线钩子由 T-DIR-01 A2 消费）
    seqs.append(await cal.insert_event(
        type_="world.announce",
        payload={"title": "晋升评审季", "scope": "company",
                 "body": f"本季度晋升窗口开启，公开答辩定于 {defense_at.isoformat()} {defense_time}（黄金档）。",
                 "text_display": f"晋升窗口开启，答辩 {defense_at.isoformat()} {defense_time}"},
        sim_time=fire_time, rng_seed=cal.clock.tick_of(fire_time),
    ))
    log.info("晋升窗口开启：%d 部门，答辩 %s %s", len(seqs) - 1, defense_at, defense_time)
    return seqs


async def _compute_perf_grades(cal: "CalendarEngine", fire_time: dt.datetime) -> dict[str, str]:
    """S~C 评分（设计未定义，工程默认 D-10）：期间系统指标（出勤/工作事件按期/加班参与）
    加权 → 部门内分层映射（z-score：≥1 → S，≥0.3 → A，≤-1 → C，其余 B；权重读
    triggers.perf_review.grade_weights 镜像）。参评范围默认全员。"""
    w = {k: float(v) for k, v in cal.cfg["triggers"]["perf_review"]["grade_weights"].items()}
    since = fire_time - dt.timedelta(days=30)
    rows = await cal.pool.fetch("SELECT id, department FROM agents ORDER BY id")
    scores: dict[str, float] = {}
    depts: dict[str, str] = {}
    for r in rows:
        aid, depts[aid] = r["id"], r["department"] or ""
        # 出勤率：attendance.<date> 标记（T-WA-02 打卡内部结算）
        marks = await cal.pool.fetch(
            "SELECT key, value FROM world_state WHERE key LIKE 'attendance.%'")
        present = total = 0
        for m in marks:
            day = dt.date.fromisoformat(m["key"].split(".", 1)[1])
            if not (since.date() <= day <= fire_time.date()):
                continue
            v = m["value"] if isinstance(m["value"], dict) else json.loads(m["value"])
            if aid in v:
                total += 1
                present += int(v[aid] == "present")
        attendance = present / total if total else 0.0
        work_cnt = await cal.pool.fetchval(
            """
            SELECT count(*) FROM events WHERE type='agent.work' AND $1 = ANY(actors)
              AND sim_time BETWEEN $2 AND $3
            """, aid, since, fire_time)
        ot_cnt = await cal.pool.fetchval(
            """
            SELECT count(*) FROM events WHERE type='world.overtime'
              AND payload->'participants' ? $1 AND sim_time BETWEEN $2 AND $3
            """, aid, since, fire_time)
        scores[aid] = w["attendance"] * attendance + w["on_time_work"] * min(float(work_cnt), 20.0) / 20.0 \
            + w["overtime_join"] * min(float(ot_cnt), 4.0) / 4.0
    grades: dict[str, str] = {}
    by_dept: dict[str, list[str]] = {}
    for aid, dept in depts.items():
        by_dept.setdefault(dept, []).append(aid)
    for dept, aids in by_dept.items():
        vals = [scores[a] for a in aids]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = var ** 0.5
        for aid in aids:
            z = (scores[aid] - mean) / std if std > 1e-9 else 0.0
            grades[aid] = "S" if z >= 1.0 else ("A" if z >= 0.3 else ("C" if z <= -1.0 else "B"))
    return grades


async def settle_perf_review(cal: "CalendarEngine", fire_time: dt.datetime,
                             *, grades: dict[str, str] | None = None) -> list[int]:
    """`world.perf_review`：全员各一条，payload `{agent_id, manager_id, grade, delta_salary?}` 逐字
    06 §1.2（delta_salary = BIGINT 分、月薪差额绝对值 = 01 §1.5 百分比档 × 当前月薪，D-11 销项口径）；
    涨薪更新月薪状态（world_state economy.salary，D-06），实际入账经下个发薪日 economy.payroll；
    C 级情绪变更落 state.needs_delta；连续 2C 进裁员候选池（world_state，01 §1.5）。"""
    cfg = cal.cfg["triggers"]["perf_review"]
    raise_pct = {k: float(v) for k, v in cal.cfg["economy"]["perf_review_raise_pct"].items()}
    salary_table = dict(await cal.get_state("economy.salary", {}) or {})
    streak_c: dict[str, int] = dict(await cal.get_state(STREAK_C_KEY, {}) or {})
    pool_: list[str] = list(await cal.get_state(LAYOFF_POOL_KEY, []) or [])
    grades = grades if grades is not None else await _compute_perf_grades(cal, fire_time)
    rows = await cal.pool.fetch("SELECT id, department FROM agents ORDER BY id")
    seqs: list[int] = []
    for r in rows:
        aid = r["id"]
        grade = grades.get(aid, "B")
        manager = await cal.pool.fetchval(
            "SELECT id FROM agents WHERE department=$1 AND job_title='M' ORDER BY id LIMIT 1", r["department"])
        # manager_id = 部门经理 NPC（01 §1.3）；8 人小世界无 NPC 经理 → null（工程默认，D-26）
        payload: dict[str, Any] = {"agent_id": aid, "manager_id": manager, "grade": grade}
        pct = raise_pct.get(grade, 0.0)
        if pct > 0:
            cur = int(salary_table[aid])
            delta = int(round(cur * pct / 100.0))
            payload["delta_salary"] = delta
            salary_table[aid] = cur + delta  # 月薪状态更新，下个发薪日生效（06 §1.2 注释口径）
        seq = await cal.insert_event(
            type_="world.perf_review", payload=payload,
            sim_time=fire_time, actors=[aid], rng_seed=cal.clock.tick_of(fire_time),
            grade_signals={"mood_hit": grade == "C"})
        seqs.append(seq)
        if grade == "C":
            await _apply_needs_c(cal, aid, float(cfg["c_mood_delta"]), str(seq))
            streak_c[aid] = int(streak_c.get(aid, 0)) + 1
            if streak_c[aid] >= int(cfg["consecutive_c_for_pool"]) and aid not in pool_:
                pool_.append(aid)
                log.warning("%s 连续 %dC 进裁员候选池", aid, streak_c[aid])
        else:
            streak_c[aid] = 0
    await cal.set_state("economy.salary", salary_table)
    await cal.set_state(STREAK_C_KEY, streak_c)
    await cal.set_state(LAYOFF_POOL_KEY, pool_)
    log.info("绩效评审完成：%d 人（S=%d A=%d B=%d C=%d）", len(seqs),
             sum(1 for g in grades.values() if g == "S"), sum(1 for g in grades.values() if g == "A"),
             sum(1 for g in grades.values() if g == "B"), sum(1 for g in grades.values() if g == "C"))
    return seqs


async def _apply_needs_c(cal: "CalendarEngine", agent_id: str, mood_delta: float, cause: str) -> None:
    if cal.agg is not None and mood_delta:
        await cal.agg.apply_needs_delta(agent_id=agent_id, need="mood", delta=mood_delta, cause=cause)


def register_career_jobs(cal: "CalendarEngine") -> None:
    """注册职场日历排程（T-WA-07）：晋升窗口（季度首月 1 日）+ 绩效评审（每月最后一个周五 16:00）。
    节假日停发工作事件（01 §6.1，T-WA-02 标记消费）。"""
    promo = cal.cfg["triggers"]["promotion_window"]
    perf = cal.cfg["triggers"]["perf_review"]
    quarter_months = {int(m) for m in promo["quarterly_months"]}
    promo_day = int(promo["day"])
    perf_wd = int(perf["monthly_last_weekday"])

    def _promo_on(d: dt.date) -> bool:
        return d.month in quarter_months and d.day == promo_day and not cal.holiday_flags(d)["work_events_suspended"]

    def _perf_on(d: dt.date) -> bool:
        return d == _last_weekday_of_month(d.year, d.month, perf_wd) \
            and not cal.holiday_flags(d)["work_events_suspended"]

    async def _promo(c: "CalendarEngine", t: dt.datetime) -> None:
        await settle_promotion_window(c, t)

    async def _perf(c: "CalendarEngine", t: dt.datetime) -> None:
        await settle_perf_review(c, t)

    cal.register_job("world.promotion_window", "10:00", _promo_on, _promo)
    cal.register_job("world.perf_review", str(perf["time"]), _perf_on, _perf)
