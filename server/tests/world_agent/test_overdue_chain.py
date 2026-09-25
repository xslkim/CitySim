"""T-WA-04 欠费链路与债务逾期日结算验收（04 文档 T-WA-04 验收 1~6）。

私有 scratch 库。`relations.yaml` 提供逾期关系行数值（01 §3.2 镜像，测试从配置读）。
场景纪律：欠费链路中"余额充足自动补扣清账"（01 §1.5）为设计行为——构造欠费场景时
须让当事 agent 持续低余额，否则每日扫描会自动清账。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

import worldsim.world_agent.economy as economy_mod
from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import flush_agg, make_engine
from worldsim.relations.relations import load_relations_config
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.economy import register_economy_jobs, register_overdue_jobs

_DB = "worldsim_wa_overdue"

RELATIONS_CFG_PATH = DDL_DIR.parent / "config" / "relations.yaml"


def _p(row) -> dict:
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])


@pytest.fixture(scope="module")
def overdue_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


async def _engine(dsn, now, on_crisis=None):
    cal, clock, pool, agg = await make_engine(dsn, now)
    register_economy_jobs(cal)
    register_overdue_jobs(cal, relations_cfg=load_relations_config(str(RELATIONS_CFG_PATH)),
                          on_crisis=on_crisis)
    return cal, clock, pool, agg


async def _drive_day(cal, clock, day: dt.date, until_hhmm: str = "23:50") -> None:
    h, m = (int(x) for x in until_hhmm.split(":"))
    clock.set(dt.datetime.combine(day, dt.time(h, m), tzinfo=LOCAL_TZ))
    await cal.tick()
    await flush_agg(cal.agg, cal, clock.now_sim())


@pytest.mark.asyncio
async def test_overdue_event_payload(overdue_dsn) -> None:
    """验收 1：低余额 → overdue 键集逐字 06 §1.2、|amount_cents|+overdue_cents=账单额、余额=0。"""
    cal, clock, pool, agg = await _engine(overdue_dsn, dt.datetime(2026, 10, 2, 6, 0, tzinfo=LOCAL_TZ))
    try:
        await pool.execute("UPDATE agents SET balance_cents = 50000 WHERE id='A01'")  # ¥500 < 房租
        tiers = cal.cfg["economy"]["rent"]["by_floor"]
        bill = next(int(t["amount_cents"]) for t in tiers.values() if 2 in [int(f) for f in t["floors"]])  # A01 住 203
        await cal.mark_settled(clock.now_sim())
        await _drive_day(cal, clock, dt.date(2026, 10, 3))  # 账单日
        rows = await pool.fetch(
            "SELECT seq, payload FROM events WHERE type='economy.bill.rent.overdue' AND payload->>'agent_id'='A01'")
        assert len(rows) == 1
        p = _p(rows[0])
        assert set(p.keys()) == {"agent_id", "amount_cents", "overdue_cents", "period"}, "键集逐字 06 §1.2"
        assert -p["amount_cents"] + p["overdue_cents"] == bill
        assert p["period"] == "2026-10"
        assert await pool.fetchval("SELECT balance_cents FROM agents WHERE id='A01'") == 0
        # 挂账落 world_state（D-06）
        state = await cal.get_state("overdue.A01")
        assert state["rent"]["owed_cents"] == p["overdue_cents"]
        # 情绪/财富变更（01 §1.5 on_overdue 镜像）经 needs_delta 链接本事件 seq（裸数字字符串）
        on_ov = cal.cfg["economy"]["overdue"]["on_overdue"]
        delta_rows = await pool.fetch("SELECT payload FROM events WHERE type='state.needs_delta'")
        changes = [c for r in delta_rows for c in _p(r)["changes"] if c["agent_id"] == "A01"]
        linked = {c["need"]: c["delta"] for c in changes if c["cause"] == str(rows[0]["seq"])}
        assert linked.get("mood") == float(on_ov["mood_delta"])
        assert linked.get("wealth") == float(on_ov["wealth_delta"])
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_notice_no_new_settlement(overdue_dsn) -> None:
    """验收 2：次周期未结清 → notice 无 amount_cents 键、overdue_cents=挂账快照；挫败值 +8（配置）。"""
    cal, clock, pool, agg = await _engine(overdue_dsn, dt.datetime(2026, 11, 2, 6, 0, tzinfo=LOCAL_TZ))
    try:
        await pool.execute("UPDATE agents SET balance_cents = 0 WHERE id='A02'")  # 持续无钱（防每日扫描自动清账）
        await cal.set_state("overdue.A02", {"rent": {"owed_cents": 150000, "periods": ["2026-10"]}})
        fru0 = await pool.fetchval("SELECT SUM(frustration) FROM goals WHERE agent_id='A02' AND status='active'")
        await cal.mark_settled(clock.now_sim())
        await _drive_day(cal, clock, dt.date(2026, 11, 3))  # 下个账单日
        rows = await pool.fetch(
            "SELECT payload FROM events WHERE type='economy.bill.rent.notice' AND payload->>'agent_id'='A02'")
        assert len(rows) == 1
        p = _p(rows[0])
        assert "amount_cents" not in p, "notice 无新结算（06 §1.2）"
        assert p["overdue_cents"] == 150000 and p["period"] == "2026-11"
        fru1 = await pool.fetchval("SELECT SUM(frustration) FROM goals WHERE agent_id='A02' AND status='active'")
        delta_cfg = int(cal.cfg["economy"]["overdue"]["next_period_unpaid"]["frustration_delta"])
        n_active = await pool.fetchval("SELECT count(*) FROM goals WHERE agent_id='A02' AND status='active'")
        assert fru1 - fru0 == delta_cfg * n_active
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_no_negative_no_compound(overdue_dsn) -> None:
    """验收 3：任意序列余额不为负、挂账额不随时间增长（不计复利）。"""
    cal, clock, pool, agg = await _engine(overdue_dsn, dt.datetime(2026, 12, 2, 6, 0, tzinfo=LOCAL_TZ))
    try:
        await pool.execute("UPDATE agents SET balance_cents = 10000 WHERE id='A03'")  # ¥100 < 5 层房租
        await cal.mark_settled(clock.now_sim())
        for day in (dt.date(2026, 12, 3), dt.date(2026, 12, 4), dt.date(2026, 12, 5)):
            await _drive_day(cal, clock, day)
        assert await pool.fetchval("SELECT count(*) FROM agents WHERE balance_cents < 0") == 0
        s1 = (await cal.get_state("overdue.A03"))["rent"]["owed_cents"]
        for day in (dt.date(2026, 12, 6), dt.date(2026, 12, 7)):
            await _drive_day(cal, clock, day)
        s2 = (await cal.get_state("overdue.A03"))["rent"]["owed_cents"]
        assert s1 == s2, "挂账不计复利（01 §1.5）"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_forced_move_or_crisis(overdue_dsn, monkeypatch) -> None:
    """验收 4：2 周期未结清 → 有空房强制搬迁最低价位；无空房 → 恰一条 director.intervene(L1)。"""
    cal, clock, pool, agg = await _engine(overdue_dsn, dt.datetime(2027, 1, 2, 6, 0, tzinfo=LOCAL_TZ))
    try:
        # 清场：抹掉模块内前序用例的挂账与余额差异（world_state 为 KV 可清；events append-only 不动）
        await pool.execute("DELETE FROM world_state WHERE key LIKE 'overdue.%'")
        await pool.execute("UPDATE agents SET balance_cents = 5000000")
        await pool.execute("UPDATE agents SET balance_cents = 0 WHERE id='A04'")
        # A04 已挂账 2 个周期（2026-11/2026-12），到 2027-01-03 账单日触发阶梯末级
        await cal.set_state("overdue.A04", {"rent": {"owed_cents": 660000, "periods": ["2026-11", "2026-12"]}})
        await cal.mark_settled(clock.now_sim())
        await _drive_day(cal, clock, dt.date(2027, 1, 3))
        row = await pool.fetchrow("SELECT room_no, position FROM agents WHERE id='A04'")
        tiers = cal.cfg["economy"]["rent"]["by_floor"]
        low_floors = {int(f) for f in min(tiers.values(), key=lambda t: int(t["amount_cents"]))["floors"]}
        assert int(row["room_no"][0]) in low_floors
        assert row["position"] == f"apt.L{row['room_no'][0]}.{row['room_no']}"

        # 无空房分支（monkeypatch 空房查找）：A05 → on_crisis 桩落 director.intervene(L1)
        crisis_calls: list[tuple[str, int]] = []

        async def fake_crisis(c, t, aid, owed):  # 生产接线 = T-DIR-03 intervene L1（唯一入口）
            crisis_calls.append((aid, owed))
            return await c.insert_event(
                type_="director.intervene", payload={"level": "L1", "reason": "退租危机"},
                sim_time=t, source="director", trigger="director", actors=[aid])

        monkeypatch.setattr(economy_mod, "find_vacant_room", lambda c: _no_vacancy())
        cal2, clock2, pool2, agg2 = await make_engine(overdue_dsn, dt.datetime(2027, 2, 2, 6, 0, tzinfo=LOCAL_TZ))
        register_economy_jobs(cal2)
        register_overdue_jobs(cal2, relations_cfg=load_relations_config(str(RELATIONS_CFG_PATH)),
                              on_crisis=fake_crisis)
        await pool2.execute("DELETE FROM world_state WHERE key LIKE 'overdue.%'")
        await pool2.execute("UPDATE agents SET balance_cents = 5000000")
        await pool2.execute("UPDATE agents SET balance_cents = 0 WHERE id='A05'")
        await cal2.set_state("overdue.A05", {"rent": {"owed_cents": 990000, "periods": ["2026-12", "2027-01"]}})
        await cal2.mark_settled(clock2.now_sim())
        before = await pool2.fetchval("SELECT count(*) FROM events WHERE trigger='director'")
        clock2.set(dt.datetime(2027, 2, 3, 23, 50, tzinfo=LOCAL_TZ))
        await cal2.tick()
        assert crisis_calls == [("A05", 990000)]
        assert await pool2.fetchval(
            "SELECT count(*) FROM events WHERE type='director.intervene' AND trigger='director'") - before == 1
        row = await pool2.fetchrow(
            "SELECT payload FROM events WHERE type='director.intervene' ORDER BY seq DESC LIMIT 1")
        assert _p(row)["level"] == "L1"  # 计干预率口径 = trigger='director'（06 §1.1）
        await pool2.close()
    finally:
        await pool.close()


async def _no_vacancy():
    return None


@pytest.mark.asyncio
async def test_debt_overdue_daily(overdue_dsn) -> None:
    """验收 5：逾期债务每日 relation.changed 含对应 changes 行（数值读 relations.yaml 镜像）；还清即止。"""
    cal, clock, pool, agg = await _engine(overdue_dsn, dt.datetime(2027, 2, 20, 6, 0, tzinfo=LOCAL_TZ))
    try:
        rule = load_relations_config(str(RELATIONS_CFG_PATH))["matrix"]["lend_overdue_daily"]
        # seed_8 债务：A01（债主）→A06（欠款人）due 2026-10-21，至 2027-02 已逾期
        debt = await pool.fetchrow("SELECT id, a_id, b_id FROM debts WHERE repaid_cents < amount_cents LIMIT 1")
        assert debt is not None
        await cal.mark_settled(clock.now_sim())
        seq0 = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        await _drive_day(cal, clock, dt.date(2027, 2, 20))
        await _drive_day(cal, clock, dt.date(2027, 2, 21))
        rows = await pool.fetch(
            "SELECT payload FROM events WHERE type='relation.changed' AND seq > $1 ORDER BY seq", seq0)
        hits = [
            c for r in rows for c in _p(r)["changes"]
            if c["a_id"] == debt["a_id"] and c["b_id"] == debt["b_id"]
        ]
        assert len(hits) == 2, "逾期每日一条结算（两日两条）"
        for c in hits:
            assert c["delta_affinity"] == int(rule["delta_affinity"])
            assert c["delta_tension"] == int(rule["delta_tension"])
            assert isinstance(c["cause"], str) and c["cause"].isdigit()
        # 全额结清后停止
        await pool.execute("UPDATE debts SET repaid_cents = amount_cents WHERE id=$1", debt["id"])
        await _drive_day(cal, clock, dt.date(2027, 2, 22))
        rows2 = await pool.fetch(
            "SELECT payload FROM events WHERE type='relation.changed' AND seq > $1", seq0)
        hits2 = [
            c for r in rows2 for c in _p(r)["changes"]
            if c["a_id"] == debt["a_id"] and c["b_id"] == debt["b_id"]
        ]
        assert len(hits2) == 2, "还清即止（01 §3.2）"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_utility_overdue(overdue_dsn) -> None:
    """验收 6：水电欠费落 economy.bill.utility.overdue（键集逐字、economy 审计标记）；
    挂账/结清链路同欠租；全程不走 economy.settle 兜底。"""
    cal, clock, pool, agg = await _engine(overdue_dsn, dt.datetime(2027, 3, 9, 6, 0, tzinfo=LOCAL_TZ))
    try:
        # 注册表对拍：utility.overdue 打 economy 审计标记（06 §1.2 v1.2）
        import yaml

        with (DDL_DIR.parent / "config" / "event_types.yaml").open(encoding="utf-8") as f:
            reg = yaml.safe_load(f)
        entry = next(t for t in reg["types"] if t["type"] == "economy.bill.utility.overdue")
        assert entry["audit_domain"] == "economy"

        await pool.execute("UPDATE agents SET balance_cents = 5000 WHERE id='A07'")  # ¥50 < 水电
        await cal.mark_settled(clock.now_sim())
        seq0 = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        await _drive_day(cal, clock, dt.date(2027, 3, 10))  # 水电账单日
        rows = await pool.fetch(
            """SELECT payload FROM events WHERE type='economy.bill.utility.overdue'
               AND seq > $1 AND payload->>'agent_id'='A07'""", seq0)
        assert len(rows) == 1
        p = _p(rows[0])
        assert set(p.keys()) == {"agent_id", "amount_cents", "overdue_cents", "period"}
        assert p["overdue_cents"] > 0
        assert await pool.fetchval("SELECT count(*) FROM events WHERE type='economy.settle' AND seq > $1", seq0) == 0
        # 结清链路：充值后每日扫描自动补扣清账
        owed = (await cal.get_state("overdue.A07"))["utility"]["owed_cents"]
        await pool.execute("UPDATE agents SET balance_cents = $1 WHERE id='A07'", owed + 10000)
        await _drive_day(cal, clock, dt.date(2027, 3, 11))
        settle_rows = await pool.fetch(
            """SELECT payload FROM events WHERE type='economy.bill.utility' AND seq > $1
               AND payload->>'agent_id'='A07' ORDER BY seq""", seq0)
        cleared = [_p(r) for r in settle_rows if -_p(r)["amount_cents"] == owed]
        assert cleared, "余额充足自动补扣清账（同类型负额结算）"
        assert "utility" not in await cal.get_state("overdue.A07")
        assert await pool.fetchval("SELECT balance_cents FROM agents WHERE id='A07'") == 10000
    finally:
        await pool.close()
