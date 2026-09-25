"""T-WA-09 随机扰动四发生器验收（04 文档 T-WA-09 验收 1~4）。"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import flush_agg, make_engine
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.disturb import (
    fire_complaint, fire_lucky, register_disturb_jobs, roll_daily_disturb,
)

_DB = "worldsim_wa_disturb"


def _p(row) -> dict:
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])


@pytest.fixture(scope="module")
def disturb_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


@pytest.mark.asyncio
async def test_frequency(disturb_dsn) -> None:
    """验收 1：固定 seed 连跑 60 模拟日，四类命中率与配置概率一致（统计容差 4σ 断言）。"""
    cal, clock, pool, agg = await make_engine(disturb_dsn, dt.datetime(2027, 3, 1, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_disturb_jobs(cal)
        cfg = cal.cfg["triggers"]["disturb"]
        await cal.mark_settled(clock.now_sim())
        start = dt.date(2027, 3, 1)
        for i in range(60):
            clock.set(dt.datetime.combine(start + dt.timedelta(days=i), dt.time(23, 55), tzinfo=LOCAL_TZ))
            await cal.tick()
            await flush_agg(agg, cal, clock.now_sim())
        n_agents = await pool.fetchval("SELECT count(*) FROM agents")
        days = 60
        counts = {}
        for t, lam in (
            ("world.disturb.illness", days * n_agents * float(cfg["illness_prob"])),
            ("world.disturb.weather", days * float(cfg["weather_prob"])),
            ("world.disturb.complaint", days * float(cfg["complaint_prob"])),
            ("world.disturb.lucky", days * float(cfg["lucky_prob"])),
        ):
            counts[t] = await pool.fetchval("SELECT count(*) FROM events WHERE type=$1", t)
            sigma = (lam * (1 - lam / (days * max(n_agents, 1)))) ** 0.5  # 二项 σ（illness 按人日计）
            lo = max(0, lam - 4 * sigma - 1)
            hi = lam + 4 * sigma + 1
            assert lo <= counts[t] <= hi, f"{t} 命中 {counts[t]} 超出期望 {lam:.2f}±4σ（[{lo:.1f},{hi:.1f}]）"
        # 骰子 seed 落 rng_seed（04 §5.3）
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type LIKE 'world.disturb.%' AND rng_seed IS NULL") == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_lucky_amount_and_audit(disturb_dsn) -> None:
    """验收 2：lucky amount_cents 落配置区间、余额守恒含之、注册表 lucky 打 economy 标记（06 §1.2）。"""
    cal, clock, pool, agg = await make_engine(disturb_dsn, dt.datetime(2027, 6, 1, 12, 0, tzinfo=LOCAL_TZ))
    try:
        with (DDL_DIR.parent / "config" / "event_types.yaml").open(encoding="utf-8") as f:
            reg = yaml.safe_load(f)
        entry = next(t for t in reg["types"] if t["type"] == "world.disturb.lucky")
        assert entry["audit_domain"] == "economy", "lucky 含金额必有 economy 审计标记"
        # 其余三类仅在 lucky 含金额时打标记的口径：注册表 audit_condition 注明
        for t in ("world.disturb.illness", "world.disturb.weather", "world.disturb.complaint"):
            e2 = next(x for x in reg["types"] if x["type"] == t)
            assert "lucky" in str(e2.get("audit_condition", "")), "条件注记口径（06 §1.2）"

        cfg = cal.cfg["triggers"]["disturb"]
        lo, hi = int(cfg["lucky_amount_cents"]["min"]), int(cfg["lucky_amount_cents"]["max"])
        now = clock.now_sim()
        bal0 = await pool.fetchval("SELECT balance_cents FROM agents WHERE id='A01'")
        seq = await fire_lucky(cal, now, agent_id="A01", kind="中奖", amount_cents=(lo + hi) // 2)
        await flush_agg(agg, cal, now)
        bal1 = await pool.fetchval("SELECT balance_cents FROM agents WHERE id='A01'")
        assert bal1 - bal0 == (lo + hi) // 2, "金额入账（正）"
        row = await pool.fetchrow("SELECT payload, rng_seed FROM events WHERE seq=$1", seq)
        p = _p(row)
        assert set(p.keys()) == {"agent_id", "kind", "amount_cents"}
        assert lo <= p["amount_cents"] <= hi
        assert row["rng_seed"] is not None
        # 财富/情绪变更链接（01 §1.5/§6.1）
        rows = await pool.fetch("SELECT payload FROM events WHERE type='state.needs_delta'")
        linked = [c for r in rows for c in _p(r)["changes"] if c["cause"] == str(seq) and c["agent_id"] == "A01"]
        needs = {c["need"] for c in linked}
        assert {"wealth", "mood"} <= needs
        assert next(c for c in linked if c["need"] == "mood")["delta"] == float(cfg["lucky_mood_delta"])
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_illness_exemption(disturb_dsn) -> None:
    """验收 3：illness 生效期内工作日工作约束豁免标记为真（请假标记供校验器/排期消费，01 §1.4）。"""
    cal, clock, pool, _ = await make_engine(disturb_dsn, dt.datetime(2027, 7, 5, 12, 0, tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        day = dt.date(2027, 7, 5)  # 周一工作日
        assert cal.is_workday(day)
        from worldsim.world_agent.disturb import fire_illness

        seq = await fire_illness(cal, clock.now_sim(), agent_id="A02", severity=1, days=2)
        assert await cal.is_on_leave("A02", day) and await cal.is_on_leave("A02", day + dt.timedelta(days=1))
        assert not await cal.is_on_leave("A02", day + dt.timedelta(days=2)), "请假到期即恢复"
        row = await pool.fetchrow("SELECT payload FROM events WHERE seq=$1", seq)
        assert set(_p(row).keys()) == {"agent_id", "severity", "days"}
        # 打卡出勤结算消费豁免标记（T-WA-02 内部结算联动）
        cal.register_core_jobs()
        await cal.mark_settled(dt.datetime.combine(day, dt.time(6, 0), tzinfo=LOCAL_TZ))
        clock.set(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(9, 0), tzinfo=LOCAL_TZ))
        await cal.tick()
        marks = await cal.get_state(f"attendance.{(day + dt.timedelta(days=1)).isoformat()}")
        assert marks["A02"] == "leave"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_director_trigger_path(disturb_dsn) -> None:
    """验收 4：trigger='director' 发起 complaint，类型仍 world.disturb.complaint 且计入干预率口径
    （04 §10.1 ④ 分子 = trigger='director' 事件数，06 §1.1）。"""
    cal, clock, pool, _ = await make_engine(disturb_dsn, dt.datetime(2027, 8, 2, 12, 0, tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        now = clock.now_sim()
        n0 = await pool.fetchval("SELECT count(*) FROM events WHERE trigger='director'")
        seq = await fire_complaint(cal, now, floor=3, issue="噪音", trigger="director")
        row = await pool.fetchrow("SELECT type, source, trigger, payload FROM events WHERE seq=$1", seq)
        assert row["type"] == "world.disturb.complaint", "导演发起不改事件类型（06 §1.1）"
        assert row["trigger"] == "director"
        n1 = await pool.fetchval("SELECT count(*) FROM events WHERE trigger='director'")
        assert n1 - n0 == 1, "计入干预率分子（04 §10.1 ④）"
        p = _p(row)
        assert set(p.keys()) == {"floor", "issue"}
    finally:
        await pool.close()
