"""T-LOD-04 背景 NPC 驻留规则引擎验收。

口径：02 文档 T-LOD-04 验收 1~3（七用例 + test_residence_zero_llm + agent.move 驻留边界 SQL 口径）。
合成夹具：2 名校外 NPC（room_no NULL）+ 1 名租客对照；M1 8 人世界无校外 NPC，引擎单测覆盖规则全集。
"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.scheduler.residence import (
    ResidenceEngine,
    enterable_for_tenant,
    is_home_node,
)
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

# 2026-10-12 周一；10-17 周六
MON = dt.datetime(2026, 10, 12, 0, 0, 0, tzinfo=LOCAL_TZ)
SAT = dt.datetime(2026, 10, 17, 0, 0, 0, tzinfo=LOCAL_TZ)
NPC_IDS = ["A30", "A31"]
TENANT = "A32"

_NEEDS = json.dumps({"energy": 70, "hunger": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70})


def _world_cfg() -> dict:
    from pathlib import Path

    with (Path(__file__).resolve().parents[2] / "config" / "world.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid, dept, pos in (("A30", "技术部", "corp.tech"), ("A31", "市场部", "corp.market")):
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, room_no, department, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'M', 30, NULL, $3, 'background', '{}'::jsonb, $4::jsonb, 0, $5)
            ON CONFLICT (id) DO UPDATE SET room_no=NULL, department=$3, position=$5
            """,
            aid, f"NPC{aid}", dept, _NEEDS, pos,
        )
    await p.execute(
        """
        INSERT INTO agents (id, name, gender, age, room_no, department, cognition_tier, persona, needs, balance_cents, position)
        VALUES ($1, '租客', 'F', 25, '203', '技术部', 'star', '{}'::jsonb, $2::jsonb, 0, 'apt.L2.203')
        ON CONFLICT (id) DO UPDATE SET room_no='203', position='apt.L2.203'
        """,
        TENANT, _NEEDS,
    )
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", [*NPC_IDS, TENANT])
        finally:
            await conn.close()


def _at(day: dt.datetime, hh: int, mm: int) -> dt.datetime:
    return day.replace(hour=hh, minute=mm)


async def test_workday_1830_move_home_and_0800_back(pool) -> None:
    """用例一/二：工作日 18:30 移位 home.<id>、次日 8:00 返岗公司节点（±1 tick 口径）。"""
    eng = ResidenceEngine(pool, _world_cfg())
    seqs = await eng.evaluate(tick=222, sim_now=_at(MON, 18, 30), rng_seed=222)
    assert len(seqs) == 2
    rows = await pool.fetch(
        """
        SELECT type, trigger, sim_time, location_id, payload::text FROM events
        WHERE type='agent.move' AND location_id LIKE 'home.%' ORDER BY seq
        """,
    )
    assert len(rows) == 2 and all(r["trigger"] == "system" for r in rows)
    # 验收 3（SQL 口径）：sim_time 落在驻留时段边界 ±1 tick（5min）
    for r in rows:
        assert abs((r["sim_time"] - _at(MON, 18, 30)).total_seconds()) <= 300
        payload = json.loads(r["payload"])
        assert set(payload) == {"from", "to", "sim_cost_min"}  # 逐字 06 §1.2
        assert 30 <= payload["sim_cost_min"] <= 45
    assert await pool.fetchval("SELECT position FROM agents WHERE id='A30'") == "home.A30"
    # 次日 8:00 返岗部门节点
    tue = MON + dt.timedelta(days=1)
    seqs2 = await eng.evaluate(tick=318, sim_now=_at(tue, 8, 0), rng_seed=318)
    assert len(seqs2) == 2
    assert await pool.fetchval("SELECT position FROM agents WHERE id='A30'") == "corp.tech"
    assert await pool.fetchval("SELECT position FROM agents WHERE id='A31'") == "corp.market"


async def test_weekend_dwell_all_day(pool) -> None:
    """用例三：周末全天驻留——周六午间仍在 home.*，无移位事件。"""
    eng = ResidenceEngine(pool, _world_cfg())
    await eng.evaluate(tick=1, sim_now=_at(SAT, 10, 0), rng_seed=1)  # 先归位
    assert await pool.fetchval("SELECT position FROM agents WHERE id='A30'") == "home.A30"
    before = await pool.fetchval("SELECT count(*) FROM events WHERE type='agent.move'")
    assert await eng.evaluate(tick=2, sim_now=_at(SAT, 14, 0), rng_seed=2) == []
    assert await eng.evaluate(tick=3, sim_now=_at(SAT, 20, 0), rng_seed=3) == []
    after = await pool.fetchval("SELECT count(*) FROM events WHERE type='agent.move'")
    assert after == before, "周末全天驻留：已驻留后零新增移位"
    assert await pool.fetchval("SELECT position FROM agents WHERE id='A30'") == "home.A30"


async def test_overtime_exception_no_move(pool) -> None:
    """用例四：加班夜例外不移位——当晚 world.overtime 参与人 18:30 不回家。"""
    eng = ResidenceEngine(pool, _world_cfg())
    await pool.execute(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
        VALUES (1, $1, 'world.overtime', 'world', 'world', '{}', 'public',
                '{"dept":"技术部","reason":"版本冲刺","participants":["A30"]}'::jsonb)
        """,
        _at(MON, 18, 30),
    )
    seqs = await eng.evaluate(tick=222, sim_now=_at(MON, 18, 35), rng_seed=222)
    assert await pool.fetchval("SELECT position FROM agents WHERE id='A30'") == "corp.tech", "加班夜例外"
    assert await pool.fetchval("SELECT position FROM agents WHERE id='A31'") == "home.A31", "非参与人照常移位"
    assert len(seqs) == 1


async def test_tenant_cannot_enter_home_node() -> None:
    """用例五：租客 move 进入 home.* 被拦截（可达性规则供 T-ADJ-03 通用校验消费）。"""
    assert is_home_node("home.A30") and not is_home_node("apt.lobby")
    assert enterable_for_tenant("apt.lobby") is True
    assert enterable_for_tenant("home.A30") is False


async def test_travel_cost_deterministic_in_range() -> None:
    """用例六：赴约折算落在 30~45min 区间且同 rng_seed 可复算（可回放口径）。"""
    eng = ResidenceEngine(None, _world_cfg())
    for seed in range(20):
        for aid in NPC_IDS:
            cost = eng.travel_cost_min(aid, seed)
            assert 30 <= cost <= 45
            assert cost == eng.travel_cost_min(aid, seed), "同 rng_seed 同 agent 同结果"


async def test_invite_location_restriction(pool) -> None:
    """用例七：驻留时段校外 NPC 邀约地点限外部场所/公寓公共区；非驻留时段不限；租客不限。"""
    eng = ResidenceEngine(pool, _world_cfg())
    evening = _at(MON, 20, 0)   # 驻留时段
    daytime = _at(MON, 14, 0)   # 工作时段
    assert await eng.invite_location_check(agent_id="A30", location_id="ext.restaurant", sim_now=evening)
    assert await eng.invite_location_check(agent_id="A30", location_id="apt.kitchen", sim_now=evening)
    assert not await eng.invite_location_check(agent_id="A30", location_id="apt.L2.203", sim_now=evening), "租客房间非公共区"
    assert not await eng.invite_location_check(agent_id="A30", location_id="corp.pantry", sim_now=evening)
    assert await eng.invite_location_check(agent_id="A30", location_id="apt.L2.203", sim_now=daytime), "非驻留时段不限制"
    assert await eng.invite_location_check(agent_id=TENANT, location_id="apt.L2.203", sim_now=evening), "租客不受限"


async def test_residence_zero_llm(pool) -> None:
    """验收 2 指定用例：2 名校外 NPC 跑 1 模拟日驻留规则，全程 llm_calls 零新增（规则引擎不触 LLM）。"""
    eng = ResidenceEngine(pool, _world_cfg())
    baseline = await pool.fetchval("SELECT count(*) FROM llm_calls")
    tick = 0
    for hour in range(0, 24, 1):  # 逐小时评估 1 模拟日（周一）
        tick += 1
        await eng.evaluate(tick=tick, sim_now=_at(MON, hour, 0) + dt.timedelta(minutes=30), rng_seed=tick)
    after = await pool.fetchval("SELECT count(*) FROM llm_calls")
    assert after == baseline, "驻留规则全程零 LLM 调用"
    # 一天下来：18:30 回家一次（06:30~08:00 窗内 07:30 驻留判定已先移位一次 + 08:30 返岗一次）
    moves = await pool.fetch(
        "SELECT payload->>'to' AS to FROM events WHERE type='agent.move' AND 'A31' = ANY(actors) ORDER BY seq",
    )
    targets = [r["to"] for r in moves]
    assert "home.A31" in targets and "corp.market" in targets
