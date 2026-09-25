"""T-DIR-03 干预框架验收（04 文档 T-DIR-03 验收 1~6；01 §6.3；06 §1.1/§1.2）。

私有 scratch 库；干预率上限从 `config/models.yaml` thresholds 段读值（01 T-CFG-02 唯一载体）。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import FakeClock, make_engine
from worldsim.llm_gateway import LLMGateway
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.director.arcs import load_arcs
from worldsim.world_agent.director.intervene import (
    BudgetExhausted, InterventionError, InterventionFramework, RateCapLockdown,
    intervention_rate_7d,
)

_DB = "worldsim_dir_intervene"
NOW = dt.datetime(2028, 5, 11, 12, 0, tzinfo=LOCAL_TZ)  # 周四


def _cap() -> float:
    with (DDL_DIR.parent / "config" / "models.yaml").open(encoding="utf-8") as f:
        return float(yaml.safe_load(f)["thresholds"]["intervention_rate_cap"])


def _p(row) -> dict:
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])


@pytest.fixture(scope="module")
def iv_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


async def _framework(dsn, now=NOW):
    cal, clock, pool, agg = await make_engine(dsn, now)
    gw = LLMGateway(pool, {}, providers={"mock": MockProvider()}, default_provider="mock")
    fw = InterventionFramework(pool, cal, clock, arcs_cfg=load_arcs(),
                               intervention_rate_cap=_cap(), gateway=gw)
    return fw, cal, clock, pool


@pytest.mark.asyncio
async def test_l0_l1_l2_persist(iv_dsn) -> None:
    """验收 1：三级各一例——事件+interventions 行落库且 event_seq 互指；payload 键逐字 06 §1.2。"""
    fw, cal, clock, pool = await _framework(iv_dsn)
    try:
        # 稀释底池：防小样本下干预率滑窗误触上限（01 §6.3 超线只许 L0）
        for _ in range(100):
            await pool.execute(
                """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
                   VALUES (0, $1, 'agent.think', 'agent:A01', 'autonomous', 'internal', '{}')""",
                clock.now_sim())
        # L0：world.* 事件本体即干预记录
        seq0 = await fw.intervene("L0", "disturb_complaint", reason="邻里冲突点火",
                                  params={"floor": 3, "issue": "噪音"})
        ev = await pool.fetchrow("SELECT type, source, trigger FROM events WHERE seq=$1", seq0)
        assert (ev["type"], ev["trigger"]) == ("world.disturb.complaint", "director")
        # L1：director.intervene 落库 + 排期副作用（world_state override）
        seq1 = await fw.intervene("L1", "schedule_overtime", reason="撮合加班",
                                  params={"date": "2028-05-12", "dept": "tech", "reason": "版本冲刺"})
        ev1 = await pool.fetchrow("SELECT type, source, trigger, payload FROM events WHERE seq=$1", seq1)
        assert (ev1["type"], ev1["source"], ev1["trigger"]) == ("director.intervene", "director", "director")
        assert set(_p(ev1).keys()) <= {"level", "arc_id", "reason"} and _p(ev1)["level"] == "L1"
        extra = await cal.get_state("schedule.overtime.extra")
        assert extra and extra[0]["dept"] == "tech"
        # L2：记忆注入（世界观察）
        seq2 = await fw.intervene("L2", "memory_inject", reason="助推",
                                  params={"agent_id": "A03", "content": "听说陈屿最近不太对劲。"})
        mem = await pool.fetchrow(
            "SELECT kind, content, source_event_seq FROM memories WHERE source_event_seq=$1", seq2)
        assert mem is not None and mem["kind"] == "event"
        # interventions 行互指
        for seq, lvl in ((seq0, "L0"), (seq1, "L1"), (seq2, "L2")):
            row = await pool.fetchrow("SELECT level, event_seq, reason FROM interventions WHERE event_seq=$1", seq)
            assert row is not None and row["level"] == lvl
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_no_double_count(iv_dsn) -> None:
    """验收 2：单动作产生的 trigger='director' 事件恰一条（D-17 防干预率重复计数）。"""
    fw, cal, clock, pool = await _framework(iv_dsn, dt.datetime(2028, 7, 6, 12, 0, tzinfo=LOCAL_TZ))
    try:
        n0 = await pool.fetchval("SELECT count(*) FROM events WHERE trigger='director'")
        await fw.intervene("L1", "team_building", reason="黄金档补位", params={"activity": "火锅局"})
        n1 = await pool.fetchval("SELECT count(*) FROM events WHERE trigger='director'")
        assert n1 - n0 == 1
        # 下游世界事件 trigger='world'（不计入分子）
        tb = await pool.fetchrow(
            "SELECT trigger FROM events WHERE type='world.team_building' ORDER BY seq DESC LIMIT 1")
        assert tb["trigger"] == "world"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_l3_impossible(iv_dsn) -> None:
    """验收 3：L3 代码路径不存在——静态扫描无数值改写、公开 API 无 L3 入口、枚举拒绝。"""
    director_dir = Path(__file__).resolve().parents[2] / "worldsim" / "world_agent" / "director"
    for f in director_dir.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        assert "UPDATE relations" not in src and "UPDATE agents" not in src, f"{f.name} 含数值改写（L3 禁令）"
        assert "balance_cents" not in src, f"{f.name} 含余额改写（L3 禁令）"
    fw, cal, clock, pool = await _framework(iv_dsn, dt.datetime(2028, 7, 20, 12, 0, tzinfo=LOCAL_TZ))
    try:
        with pytest.raises(InterventionError):
            await fw.intervene("L3", "set_affinity", reason="永禁", params={})
        # 公开 API 表面无数值改写类函数
        for name in dir(fw):
            assert not name.startswith(("set_relation", "force_", "write_needs")), name
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_rate_cap_lockdown(iv_dsn) -> None:
    """验收 4：干预率超上限 → L1/L2 抛拒绝、L0 放行；回落后解锁（阈值读 models.yaml）。"""
    now = dt.datetime(2028, 8, 10, 12, 0, tzinfo=LOCAL_TZ)
    fw, cal, clock, pool = await _framework(iv_dsn, now)
    try:
        # 构造 7 日窗口内高超干预率：director 事件占比 > cap
        for _ in range(4):
            await pool.execute(
                """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
                   VALUES (0, $1, 'world.announce', 'director', 'director', 'internal', '{}')""", now)
        assert await fw.intervention_rate_7d() >= _cap()
        with pytest.raises(RateCapLockdown):
            await fw.intervene("L1", "schedule_overtime",
                               params={"date": "2028-08-11", "dept": "tech"})
        with pytest.raises(RateCapLockdown):
            await fw.intervene("L2", "memory_inject", params={"agent_id": "A01", "content": "x"})
        seq = await fw.intervene("L0", "disturb_weather", params={"kind": "暴雨"})  # L0 放行
        assert seq
        # 回落解锁：灌入大量非干预事件稀释
        for _ in range(200):
            await pool.execute(
                """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
                   VALUES (0, $1, 'agent.think', 'agent:A01', 'autonomous', 'internal', '{}')""", now)
        assert await fw.intervention_rate_7d() < _cap()
        seq2 = await fw.intervene("L1", "schedule_overtime",
                                  params={"date": "2028-08-11", "dept": "market"})
        assert seq2
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_rate_matches_audit_sql(iv_dsn) -> None:
    """验收 5：intervention_rate_7d() 与 04 §10.1 ④ SQL 同数据集结果相等（与 T-AUD 对账口径）。"""
    now = dt.datetime(2028, 9, 3, 12, 0, tzinfo=LOCAL_TZ)
    fw, cal, clock, pool = await _framework(iv_dsn, now)
    try:
        await pool.execute(
            """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
               VALUES (0, $1, 'world.announce', 'director', 'director', 'internal', '{}')""", now)
        for _ in range(9):
            await pool.execute(
                """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
                   VALUES (0, $1, 'agent.think', 'agent:A02', 'autonomous', 'internal', '{}')""", now)
        # 04 §10.1 ④ 逐字 SQL 口径
        expected = await pool.fetchval(
            """SELECT COUNT(*) FILTER (WHERE trigger='director')::float / COUNT(*)
               FROM events WHERE sim_time > $1""", now - dt.timedelta(days=7))
        assert await intervention_rate_7d(pool, now) == pytest.approx(expected)
        assert await fw.intervention_rate_7d() == pytest.approx(expected)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_budget_enforcement(iv_dsn) -> None:
    """验收 6：弧线 intervention_budget 配额耗尽 → 该弧线新干预经本入口被拒（R3 裁定归本任务）。"""
    now = dt.datetime(2028, 10, 12, 12, 0, tzinfo=LOCAL_TZ)
    fw, cal, clock, pool = await _framework(iv_dsn, now)
    try:
        tpl = next(a for a in load_arcs()["arcs"] if a["arc_id"] == "ARC-A4")  # budget L1:1
        assert int(tpl["intervention_budget"]["L1"]) == 1
        # 构造活跃实例（预算数据经 world_state 共享，04 §3 澄清）
        await cal.set_state("arc.inst.ARC-A4", {
            "arc_id": "ARC-A4", "stage": "S1", "started_at": now.isoformat(),
            "stage_entered_at": now.isoformat(), "budget_used": {}, "status": "active"})
        # 先灌足量非干预事件防误触滑窗上限
        for _ in range(50):
            await pool.execute(
                """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
                   VALUES (0, $1, 'agent.think', 'agent:A03', 'autonomous', 'internal', '{}')""", now)
        await fw.intervene("L1", "schedule_overtime", arc_id="ARC-A4",
                           params={"date": "2028-10-13", "dept": "tech"})
        with pytest.raises(BudgetExhausted):
            await fw.intervene("L1", "schedule_overtime", arc_id="ARC-A4",
                               params={"date": "2028-10-14", "dept": "tech"})
        inst = await cal.get_state("arc.inst.ARC-A4")
        assert inst["budget_used"]["L1"] == 1
    finally:
        await pool.close()
