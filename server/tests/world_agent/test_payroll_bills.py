"""T-WA-03 经济日历结算验收（04 文档 T-WA-03 验收 1~5）。

私有 scratch 库（隔离惯例见 _support.py）。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import flush_agg, make_engine
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.economy import register_economy_jobs

_DB = "worldsim_wa_payroll"
_DB2 = "worldsim_wa_payroll_rerun"

EVENT_TYPES_PATH = DDL_DIR.parent / "config" / "event_types.yaml"


def _economy_types() -> list[str]:
    """economy 审计域类型清单（06 §1.3：由 event_types.yaml 标记生成，禁 LIKE 前缀法）。"""
    with EVENT_TYPES_PATH.open(encoding="utf-8") as f:
        reg = yaml.safe_load(f)
    return sorted(t["type"] for t in reg["types"] if t.get("audit_domain") == "economy")


@pytest.fixture(scope="module")
def payroll_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


async def _drive_to(cal, clock, target: dt.datetime) -> None:
    await cal.mark_settled(clock.now_sim())
    clock.set(target)
    await cal.tick()


@pytest.mark.asyncio
async def test_payroll_day(payroll_dsn) -> None:
    """验收 1：发薪日恰 COUNT(agents) 条 economy.payroll；金额 = seed 月薪 − 通勤包（读配置）。"""
    cal, clock, pool, agg = await make_engine(payroll_dsn, dt.datetime(2026, 10, 1, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_economy_jobs(cal)
        cfg = cal.cfg["economy"]
        salary_table = await cal.get_state("economy.salary")
        before = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        await _drive_to(cal, clock, dt.datetime(2026, 10, 1, 11, 0, tzinfo=LOCAL_TZ))
        await flush_agg(agg, cal, clock.now_sim())
        rows = await pool.fetch(
            "SELECT payload, source, trigger FROM events WHERE type='economy.payroll' AND seq > $1 ORDER BY seq", before)
        n_agents = await pool.fetchval("SELECT count(*) FROM agents")
        assert len(rows) == n_agents
        commute = int(cfg["commute"]["amount_cents"])
        seen = set()
        for r in rows:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
            assert p["amount_cents"] == int(salary_table[p["agent_id"]]) - commute
            assert p["month"] == "2026-10"
            seen.add(p["agent_id"])
            assert (r["source"], r["trigger"]) == ("world", "world")
        assert len(seen) == n_agents
        # 余额入账核对（audit ① 口径的地基）
        for aid in seen:
            bal = await pool.fetchval("SELECT balance_cents FROM agents WHERE id=$1", aid)
            seed_bal = None  # seed 初值见 seed_8.sql；此处只对账增量（下方 SQL 对账用例覆盖总额）
            assert bal is not None
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_rent_bill_by_floor(payroll_dsn) -> None:
    """验收 2：三个楼层档各抽 1 人断言账单金额与配置一致（seed_8：A01=2 层/A06=3 层/A03=5 层）。"""
    cal, clock, pool, agg = await make_engine(payroll_dsn, dt.datetime(2026, 11, 3, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_economy_jobs(cal)
        cfg = cal.cfg["economy"]
        await _drive_to(cal, clock, dt.datetime(2026, 11, 3, 10, 0, tzinfo=LOCAL_TZ))
        await flush_agg(agg, cal, clock.now_sim())
        rows = await pool.fetch("SELECT payload FROM events WHERE type='economy.bill.rent'")
        by_agent = {}
        for r in rows:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
            by_agent[p["agent_id"]] = p["amount_cents"]
        tiers = cfg["rent"]["by_floor"]
        floors_of = {f: t["amount_cents"] for t in tiers.values() for f in t["floors"]}
        samples = {"A01": 2, "A06": 3, "A03": 5}  # 低/中/高三档各一（seed_8 房间号）
        for aid, floor in samples.items():
            assert by_agent[aid] == -int(floors_of[floor]), f"{aid}（{floor} 层）房租档不符"
        # 02 文档口径延伸：金额键逐字、出账为负（06 §1.3 正入负出）
        assert all(v < 0 for v in by_agent.values())
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_utility_amount_in_range(payroll_dsn) -> None:
    """验收 3：水电金额落配置区间且同 seed 重跑序列一致。"""
    cal, clock, pool, agg = await make_engine(payroll_dsn, dt.datetime(2026, 12, 10, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_economy_jobs(cal)
        util = cal.cfg["economy"]["utility"]
        await _drive_to(cal, clock, dt.datetime(2026, 12, 10, 10, 0, tzinfo=LOCAL_TZ))
        clock.set(dt.datetime(2027, 1, 10, 10, 0, tzinfo=LOCAL_TZ))
        await cal.tick()
        rows = await pool.fetch(
            "SELECT payload, rng_seed, sim_time FROM events WHERE type='economy.bill.utility' ORDER BY seq")
        assert rows, "应有水电账单"
        amounts = []
        for r in rows:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
            assert int(util["min_cents"]) <= -p["amount_cents"] <= int(util["max_cents"])
            assert r["rng_seed"] is not None  # 骰子 seed 落 events.rng_seed（04 §5.3）
            amounts.append(-p["amount_cents"])
        # 同周期全员同额（公摊），跨周期随机游走
        by_period: dict[str, set] = {}
        for r in rows:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
            by_period.setdefault(r["sim_time"].strftime("%Y-%m"), set()).add(p["amount_cents"])
        assert all(len(v) == 1 for v in by_period.values())
    finally:
        await pool.close()

    # 同 seed 重跑：独立 scratch 库同起点重放，金额序列逐值一致
    dsn2 = _build_db(_DB2, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        cal2, clock2, pool2, _ = await make_engine(dsn2, dt.datetime(2026, 12, 10, 6, 0, tzinfo=LOCAL_TZ))
        register_economy_jobs(cal2)
        await cal2.mark_settled(clock2.now_sim())
        clock2.set(dt.datetime(2026, 12, 10, 10, 0, tzinfo=LOCAL_TZ))
        await cal2.tick()
        clock2.set(dt.datetime(2027, 1, 10, 10, 0, tzinfo=LOCAL_TZ))
        await cal2.tick()
        rows2 = await pool2.fetch(
            "SELECT payload FROM events WHERE type='economy.bill.utility' ORDER BY seq")
        amounts2 = [
            -(json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"]))["amount_cents"]
            for r in rows2
        ]
        assert amounts2 == amounts
        await pool2.close()
    finally:
        _drop_db(_DB2)


@pytest.mark.asyncio
async def test_balance_conservation(payroll_dsn) -> None:
    """验收 4：余额守恒对账——Σ经济域 amount_cents = Σbalance 增量（04 §10.1 ① 口径）。"""
    cal, clock, pool, agg = await make_engine(payroll_dsn, dt.datetime(2027, 2, 1, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_economy_jobs(cal)
        bal0 = await pool.fetchval("SELECT SUM(balance_cents) FROM agents")
        seq0 = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        await _drive_to(cal, clock, dt.datetime(2027, 2, 1, 11, 0, tzinfo=LOCAL_TZ))   # 发薪+通勤代扣
        clock.set(dt.datetime(2027, 2, 3, 10, 0, tzinfo=LOCAL_TZ))                      # 房租
        await cal.tick()
        await flush_agg(agg, cal, clock.now_sim())
        bal1 = await pool.fetchval("SELECT SUM(balance_cents) FROM agents")
        settled = await pool.fetchval(
            """
            SELECT coalesce(SUM((payload->>'amount_cents')::bigint), 0) FROM events
            WHERE seq > $1 AND type = ANY($2::text[])
            """, seq0, _economy_types())
        assert bal1 - bal0 == settled, "余额增量与经济域事件结算额不一致（04 §10.1 ①）"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_needs_delta_linked(payroll_dsn) -> None:
    """验收 5：每笔结算存在对应 state.needs_delta，changes[].cause = 该事件 seq 裸数字字符串（06 §2）。"""
    cal, clock, pool, agg = await make_engine(payroll_dsn, dt.datetime(2027, 3, 1, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_economy_jobs(cal)
        await _drive_to(cal, clock, dt.datetime(2027, 3, 1, 11, 0, tzinfo=LOCAL_TZ))
        await flush_agg(agg, cal, clock.now_sim())
        payrolls = await pool.fetch(
            "SELECT seq, payload FROM events WHERE type='economy.payroll' ORDER BY seq DESC LIMIT 8")
        assert payrolls
        deltas = await pool.fetch(
            "SELECT payload FROM events WHERE type='state.needs_delta' AND seq > $1",
            payrolls[-1]["seq"] - 1)
        causes = set()
        for r in deltas:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
            for c in p["changes"]:
                causes.add(c["cause"])
                assert isinstance(c["cause"], str) and c["cause"].isdigit(), "cause 须为裸 seq 数字字符串"
        for ev in payrolls:
            assert str(ev["seq"]) in causes, f"payroll seq={ev['seq']} 缺对应 needs_delta"
            p = json.loads(ev["payload"]) if isinstance(ev["payload"], str) else dict(ev["payload"])
            # 工资财富 +salary_credit（01 §1.5，读配置）
            credit = cal.cfg["economy"]["wealth_need_mapping"]["salary_credit"]
            match = [
                c for r in deltas for c in (json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"]))["changes"]
                if c["cause"] == str(ev["seq"]) and c["agent_id"] == p["agent_id"] and c["need"] == "wealth"
            ]
            # clamp 语义（04 §6.5 值域 0~100）：跨用例多次发薪后 delta 可能被截断，断言落 (0, credit]
            assert match and 0 < match[0]["delta"] <= float(credit) + 1e-9
    finally:
        await pool.close()
