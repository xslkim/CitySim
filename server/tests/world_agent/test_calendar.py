"""T-WA-02 日历引擎验收（04 文档 T-WA-02 验收 1~4）。

私有 scratch 库（events append-only 不可清库，隔离惯例同 tests/adjudicator/test_pipeline.py）。
"""

from __future__ import annotations

import datetime as dt

import asyncpg
import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.calendar import CalendarEngine
from worldsim.world_agent.config import load_world_config

_CAL_DB = "worldsim_wa_calendar"


@pytest.fixture(scope="module")
def cal_dsn():
    dsn = _build_db(_CAL_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_CAL_DB)


class FakeClock:
    """鸭子类型时钟（TimeEngine 能力子集：now_sim/tick_of/current_tick）。"""

    def __init__(self, now: dt.datetime) -> None:
        self._now = now
        self._tick0 = now

    def set(self, now: dt.datetime) -> None:
        self._now = now

    def now_sim(self) -> dt.datetime:
        return self._now

    def tick_of(self, sim_time: dt.datetime) -> int:
        return int((sim_time - self._tick0).total_seconds() // 300)

    @property
    def current_tick(self) -> int:
        return self.tick_of(self._now)


def _engine(pool, now: dt.datetime) -> tuple[CalendarEngine, FakeClock]:
    clock = FakeClock(now)
    return CalendarEngine(pool, load_world_config(), clock), clock


def test_workday_weekend_holiday() -> None:
    """验收 1：三种判定与配置节假日表一致。"""
    cfg = load_world_config()
    cal, _ = _engine(None, dt.datetime(2026, 9, 21, 12, 0, tzinfo=LOCAL_TZ))
    assert cal.is_workday(dt.date(2026, 9, 21))          # 周一
    assert not cal.is_weekend(dt.date(2026, 9, 21))
    assert cal.is_weekend(dt.date(2026, 9, 26))          # 周六
    assert not cal.is_workday(dt.date(2026, 9, 26))
    for entry in cfg["holidays"]["table"]:
        mm, dd = (int(x) for x in entry["start"].split("-"))
        start = dt.date(2026, mm, dd)
        for i in range(int(entry["days"])):
            d = start + dt.timedelta(days=i)
            assert cal.is_holiday(d), f"{entry['name']} 第 {i + 1} 天应为节假日"
            assert not cal.is_workday(d)
        # 节假日区间外紧邻日（非法定周末时）应为非节假日
        after = start + dt.timedelta(days=int(entry["days"]))
        if after.weekday() < 5:
            assert not cal.is_holiday(after)


def test_holiday_flags() -> None:
    """验收 4：节假日当天休市/工作停发标记为真、工作日为假；权重读配置。"""
    cfg = load_world_config()
    cal, _ = _engine(None, dt.datetime(2026, 9, 25, 12, 0, tzinfo=LOCAL_TZ))
    flags = cal.holiday_flags(dt.date(2026, 9, 25))      # 中秋（配置表 09-25）
    assert flags["is_holiday"] and flags["name"] == "中秋"
    assert flags["market_closed"] == cfg["holidays"]["rules"]["market_closed"]
    assert flags["work_events_suspended"] == cfg["holidays"]["rules"]["work_events_suspended"]
    assert flags["social_invite_weight"] == cfg["holidays"]["rules"]["social_invite_weight"]
    work = cal.holiday_flags(dt.date(2026, 9, 21))
    assert not work["is_holiday"] and not work["market_closed"] and not work["work_events_suspended"]
    assert work["social_invite_weight"] == 1.0
    # 作息时段判定（01 §1.4；工作日 10:00 工作段 / 周末同时刻 free）
    assert cal.current_segment(dt.datetime(2026, 9, 21, 10, 0, tzinfo=LOCAL_TZ)) == "work_am"
    assert cal.current_segment(dt.datetime(2026, 9, 26, 10, 0, tzinfo=LOCAL_TZ)) == "free"
    assert cal.current_segment(dt.datetime(2026, 9, 21, 1, 0, tzinfo=LOCAL_TZ)) == "sleep"


@pytest.mark.asyncio
async def test_batch_calendar_dispatches(cal_dsn) -> None:
    """验收 2：注册回调按 (fire_time, 注册序) 执行，跨日排程项无遗漏无重复。"""
    pool = await asyncpg.create_pool(cal_dsn, min_size=1, max_size=2)
    try:
        cal, clock = _engine(pool, dt.datetime(2026, 9, 21, 8, 0, tzinfo=LOCAL_TZ))
        order: list[tuple[str, str]] = []
        cfg = load_world_config()

        async def job_a(engine, fire_time):
            order.append(("a", fire_time.strftime("%m-%d %H:%M")))

        async def job_b(engine, fire_time):
            order.append(("b", fire_time.strftime("%m-%d %H:%M")))

        # 注册序故意与时间序相反（a 在后注册但时刻更早），验证排序键
        cal.register_job("test.job_b", "18:30", cal.is_workday, job_b)
        cal.register_job("test.job_a", "09:00", lambda d: d.day == 1, job_a)  # 每月 1 日
        # 跨 2026-09-30 18:00 → 2026-10-02 12:00（覆盖 10-01 09:00 的 job_a 与多个 18:30）
        # 10-01~03 为国庆节假日（读配置判定，workday 谓词自动排除）
        span_days = [d.strftime("%m-%d") for d in
                     (dt.date(2026, 9, 30) + dt.timedelta(days=i) for i in range(1, 3))]
        fired = await cal.settle_span(
            dt.datetime(2026, 9, 30, 18, 0, tzinfo=LOCAL_TZ),
            dt.datetime(2026, 10, 2, 12, 0, tzinfo=LOCAL_TZ),
        )
        expected_b = [d for d in ("2026-09-30", "2026-10-01", "2026-10-02")
                      if cal.is_workday(dt.date.fromisoformat(d))]  # 国庆节假日自动排除（10-01~03）
        assert ("a", "10-01 09:00") in order
        b_fires = [t for n, t in order if n == "b"]
        assert b_fires == [f"{d[5:]} 18:30" for d in expected_b]
        # 时间序单调
        times = [t for _, t in order]
        assert times == sorted(times)
        # 游标推进后 tick 不重复触发（无重复）
        again = await cal.tick(dt.datetime(2026, 10, 2, 12, 0, tzinfo=LOCAL_TZ))
        assert again == []
        assert fired.count("time.day_summary") == 2  # 10-01 / 10-02 两次日界翻转
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_day_summary_once_per_day(cal_dsn) -> None:
    """验收 3：连跑 3 模拟日恰 3 条 time.day_summary，payload 键仅 {day}，序号连续。"""
    pool = await asyncpg.create_pool(cal_dsn, min_size=1, max_size=2)
    try:
        cal, clock = _engine(pool, dt.datetime(2026, 11, 9, 20, 0, tzinfo=LOCAL_TZ))
        await cal.mark_settled(dt.datetime(2026, 11, 9, 20, 0, tzinfo=LOCAL_TZ))  # 游标对齐，隔离模块内前序测试进度
        baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        for i in range(3):
            clock.set(dt.datetime(2026, 11, 9, 20, 0, tzinfo=LOCAL_TZ) + dt.timedelta(days=i + 1))
            await cal.tick()
        rows = await pool.fetch(
            "SELECT payload FROM events WHERE type='time.day_summary' AND seq > $1 ORDER BY seq", baseline)
        assert len(rows) == 3
        days = []
        for r in rows:
            payload = r["payload"] if isinstance(r["payload"], dict) else __import__("json").loads(r["payload"])
            assert set(payload.keys()) == {"day"}
            days.append(payload["day"])
        assert days == sorted(days) and len(set(days)) == 3
        # 原地重跑（游标不动）不再产生新条
        await cal.tick()
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='time.day_summary' AND seq > $1", baseline) == 3
        # source/trigger 契约（06 §1.2）
        row = await pool.fetchrow("SELECT source, trigger, visibility FROM events WHERE type='time.day_summary' LIMIT 1")
        assert (row["source"], row["trigger"], row["visibility"]) == ("system", "system", "internal")
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_checkin_internal_no_event(cal_dsn) -> None:
    """8:00 打卡为内部出勤状态结算：不产事件（D-03），attendance 标记落 world_state；请假标记得豁免。"""
    pool = await asyncpg.create_pool(cal_dsn, min_size=1, max_size=2)
    try:
        cal, clock = _engine(pool, dt.datetime(2026, 12, 7, 6, 0, tzinfo=LOCAL_TZ))  # 周一
        cal.register_core_jobs()
        await cal.set_state("leave.A01", {"until": "2026-12-08"})  # T-WA-09 请假标记形态
        before = await pool.fetchval("SELECT count(*) FROM events")
        await cal.tick()  # 游标
        clock.set(dt.datetime(2026, 12, 7, 9, 0, tzinfo=LOCAL_TZ))
        fired = await cal.tick()
        assert "attendance.checkin" in fired
        after_rows = await pool.fetch(
            "SELECT type FROM events WHERE seq > $1 AND type <> 'time.day_summary'", before)
        assert after_rows == [], "打卡不得产事件（04 §3.3 v1.2 注）"
        marks = await cal.get_state("attendance.2026-12-07")
        assert marks["A01"] == "leave"
        assert marks["A02"] == "present"
        assert len(marks) == 8  # seed_8 全员
    finally:
        await pool.close()
