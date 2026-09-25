"""T-WA-08 晚间排期验收（04 文档 T-WA-08 验收 1~4）。"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import make_engine
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.calendar import queue_extra_overtime, register_evening_jobs

_DB = "worldsim_wa_evening"


def _p(row) -> dict:
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])


@pytest.fixture(scope="module")
def evening_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


async def _drive(cal, clock, start: dt.date, days: int) -> None:
    await cal.mark_settled(clock.now_sim())
    for i in range(days):
        clock.set(dt.datetime.combine(start + dt.timedelta(days=i), dt.time(23, 55), tzinfo=LOCAL_TZ))
        await cal.tick()


@pytest.mark.asyncio
async def test_overtime_frequency(evening_dsn) -> None:
    """验收 1：连跑 4 模拟周，每部门每周次数落配置区间、时段落配置窗口、仅工作日。"""
    cal, clock, pool, _ = await make_engine(evening_dsn, dt.datetime(2027, 11, 1, 6, 0, tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        register_evening_jobs(cal)
        cfg = cal.cfg["triggers"]["overtime"]
        lo, hi = (int(x) for x in cfg["per_dept_per_week"])
        w0, w1 = (str(cfg["window"][0]), str(cfg["window"][1]))
        await _drive(cal, clock, dt.date(2027, 11, 1), 28)  # 2027-11-01 周一，11 月无节假日
        rows = await pool.fetch("SELECT payload, sim_time FROM events WHERE type='world.overtime'")
        assert rows, "应有加班事件"
        # 按 (dept, ISO 周) 分桶计数落区间
        buckets: dict[tuple[str, tuple], int] = {}
        for r in rows:
            p = _p(r)
            iso = r["sim_time"].astimezone(LOCAL_TZ).date().isocalendar()
            buckets[(p["dept"], (iso.year, iso.week))] = buckets.get((p["dept"], (iso.year, iso.week)), 0) + 1
            t = r["sim_time"].astimezone(LOCAL_TZ)
            assert w0 <= t.strftime("%H:%M") <= w1, "时段落配置窗口"
            assert cal.is_workday(t), "仅工作日"
        for (dept, week), n in buckets.items():
            assert lo <= n <= hi, f"{dept} {week} 加班 {n} 次不在 [{lo},{hi}]（01 §6.1）"
        # 覆盖完整性：4 周 × 6 部门每周都应有排期（区间下限 ≥1）
        n_weeks = {week for _, week in buckets}
        assert len(n_weeks) >= 4
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_overtime_payload(evening_dsn) -> None:
    """验收 2：payload 键集合与 06 §1.2 一致；participants 非空且均为该部门员工；请假者豁免。"""
    cal, clock, pool, _ = await make_engine(evening_dsn, dt.datetime(2027, 11, 29, 6, 0, tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        register_evening_jobs(cal)
        # L1 加场排期原语（供 T-DIR-03 复用）：给技术部明天加一场
        tomorrow = dt.date(2027, 11, 30)  # 周二
        await queue_extra_overtime(cal, tomorrow, "tech", "版本冲刺")
        # 请假豁免：A01 明天请假（T-WA-02/T-WA-09 标记形态）
        await cal.set_state("leave.A01", {"until": tomorrow.isoformat()})
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime.combine(tomorrow, dt.time(23, 55), tzinfo=LOCAL_TZ))
        await cal.tick()
        rows = await pool.fetch(
            "SELECT payload FROM events WHERE type='world.overtime' AND sim_time::date=$1", tomorrow)
        by_dept = {_p(r)["dept"]: _p(r) for r in rows}
        assert "技术部" in by_dept, "L1 加场应落库"
        for dept, p in by_dept.items():
            assert {"dept", "reason", "participants"} <= set(p.keys()) <= {
                "dept", "reason", "participants", "text_display"}
            assert p["participants"], "participants 非空"
            for aid in p["participants"]:
                assert await pool.fetchval("SELECT department FROM agents WHERE id=$1", aid) == dept
        assert "A01" not in by_dept["技术部"]["participants"], "当日请假者豁免（D-12）"
        # 加场消费后标记清除（不重复触发）
        assert await cal.get_state("schedule.overtime.extra") in ([], None)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_team_building(evening_dsn) -> None:
    """验收 3：每月第 2 个周六恰一条；收费开启时 amount_cents 存在且余额守恒对账（04 §10.1 ①）通过。"""
    cal, clock, pool, _ = await make_engine(evening_dsn, dt.datetime(2027, 12, 11, 6, 0, tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        register_evening_jobs(cal)
        tb = cal.cfg["triggers"]["team_building"]
        assert int(tb["charge_cents"]) == 0  # 默认不收费（配置开关）
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime(2027, 12, 11, 23, 55, tzinfo=LOCAL_TZ))  # 12 月第 2 个周六
        await cal.tick()
        rows = await pool.fetch(
            "SELECT payload FROM events WHERE type='world.team_building' AND sim_time::date=$1",
            dt.date(2027, 12, 11))
        assert len(rows) == 1
        p = _p(rows[0])
        assert p["dept"] == "all" and "amount_cents" not in p  # 不收费无金额键（06 §1.2）
        assert p["activity"] in [str(x) for x in tb["activities"]]
        t = await pool.fetchval(
            "SELECT sim_time FROM events WHERE type='world.team_building' AND sim_time::date=$1",
            dt.date(2027, 12, 11))
        assert t.astimezone(LOCAL_TZ).strftime("%H:%M") == str(tb["window"][0])

        # 收费开关开启：amount_cents 必有（= -人均×人数总额）且余额守恒
        cal.cfg["triggers"]["team_building"]["charge_cents"] = 5000
        bal0 = await pool.fetchval("SELECT SUM(balance_cents) FROM agents")
        clock.set(dt.datetime(2028, 1, 8, 23, 55, tzinfo=LOCAL_TZ))  # 2028-01 第 2 个周六
        await cal.tick()
        rows2 = await pool.fetch(
            "SELECT payload FROM events WHERE type='world.team_building' ORDER BY seq DESC LIMIT 1")
        p2 = _p(rows2[0])
        n = await pool.fetchval("SELECT count(*) FROM agents")
        assert p2["amount_cents"] == -5000 * n
        bal1 = await pool.fetchval("SELECT SUM(balance_cents) FROM agents")
        assert bal1 - bal0 == p2["amount_cents"], "收费守恒对账（04 §10.1 ①）"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_holiday_mutex(evening_dsn) -> None:
    """验收 4：节假日当天零条 world.overtime / world.team_building（01 §6.1 工作事件停发）。"""
    cal, clock, pool, _ = await make_engine(evening_dsn, dt.datetime(2026, 12, 31, 6, 0, tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        register_evening_jobs(cal)
        holiday = dt.date(2027, 1, 1)  # 元旦（周五——本是工作日）
        assert cal.is_holiday(holiday) and holiday.weekday() < 5
        await queue_extra_overtime(cal, holiday, "tech", "强行加场")  # 加场也不得破互斥
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime.combine(holiday, dt.time(23, 55), tzinfo=LOCAL_TZ))
        await cal.tick()
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type IN ('world.overtime','world.team_building')"
            " AND sim_time::date=$1", holiday) == 0
    finally:
        await pool.close()
