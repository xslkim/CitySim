"""T-LOD-01 三层认知统一接口与时钟兜底排程验收。

口径：02 文档 T-LOD-01 验收 1~2（三层 next_due 间隔 / 到期入队 / 全员 star 分发 + SQL：
跑 1 模拟小时后 agents.next_due_sim 均大于排程前 sim_time）。
"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio

from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.scheduler.lod import (
    STAR_INTERVAL,
    SECONDARY_INTERVAL,
    BackgroundUnit,
    LodScheduler,
    SecondaryUnit,
    StarUnit,
    UnitContext,
    next_due_for,
    unit_for,
)
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=LOCAL_TZ)  # 周一 12:00
LOD_AGENT_IDS = ["A20", "A21", "A22"]


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    tiers = {"A20": "star", "A21": "secondary", "A22": "background"}
    for aid in LOD_AGENT_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, $3, '{}'::jsonb, $4::jsonb, 0, 'apt.lobby')
            ON CONFLICT (id) DO UPDATE SET cognition_tier=$3, next_due_sim=NULL
            """,
            aid, f"测试{aid}", tiers[aid],
            json.dumps({"energy": 70, "hunger": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70}),
        )
    await p.execute("UPDATE agents SET next_due_sim=NULL WHERE id = ANY($1)", LOD_AGENT_IDS)
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", LOD_AGENT_IDS)
        finally:
            await conn.close()


async def test_tier_next_due_intervals() -> None:
    """用例一：三层 next_due 间隔（star=+15min、secondary=+2h、background=凌晨 batch 段=None）。"""
    assert next_due_for("star", T0) == T0 + STAR_INTERVAL == T0 + dt.timedelta(minutes=15)
    assert next_due_for("secondary", T0) == T0 + SECONDARY_INTERVAL == T0 + dt.timedelta(hours=2)
    assert next_due_for("background", T0) is None
    with pytest.raises(ValueError):
        next_due_for("vip", T0)
    # CognitiveUnit 协议分发：三层同接口
    assert unit_for("star", "A01").next_due(T0) == T0 + dt.timedelta(minutes=15)
    assert unit_for("secondary", "A01").next_due(T0) == T0 + dt.timedelta(hours=2)
    assert unit_for("background", "A01").next_due(T0) is None


async def test_collect_due_enqueue_and_writeback(pool) -> None:
    """用例二 + 验收 2（SQL 口径）：到期入队并写回 next_due_sim；跑 1 模拟小时后全员 > 排程前。"""
    lod = LodScheduler(pool)
    # 初始 next_due_sim=NULL → star/secondary 立即到期、background 永不进兜底
    due = await lod.collect_due(tick=0, sim_now=T0)
    assert due == ["A20", "A21"], "background 不进时钟兜底（04 §3.3 单一时点）"
    nd20 = await lod.next_due_of("A20")
    nd21 = await lod.next_due_of("A21")
    assert nd20 == T0 + dt.timedelta(minutes=15) and nd21 == T0 + dt.timedelta(hours=2)
    assert nd20 > T0 and nd21 > T0, "写回 next_due_sim 均大于排程前 sim_time（验收 2 SQL 口径）"
    # 未到期不再出队；到下一兜底时点再次到期
    assert await lod.collect_due(tick=1, sim_now=T0 + dt.timedelta(minutes=5)) == []
    assert await lod.collect_due(tick=3, sim_now=T0 + dt.timedelta(minutes=15)) == ["A20"]
    # 1 模拟小时后：star 已滚动 4 次，next_due_sim 仍恒 > 当前 sim
    sim_end = T0 + dt.timedelta(hours=1)
    rows = await pool.fetch("SELECT id, next_due_sim FROM agents WHERE id = ANY($1) ORDER BY id", LOD_AGENT_IDS)
    for t in range(4, 13):  # tick 4..12（15min~60min）
        await lod.collect_due(tick=t, sim_now=T0 + dt.timedelta(minutes=5 * t))
    rows = await pool.fetch("SELECT id, next_due_sim FROM agents WHERE id = ANY($1) ORDER BY id", LOD_AGENT_IDS)
    by_id = {r["id"]: r["next_due_sim"] for r in rows}
    assert by_id["A20"] > sim_end and by_id["A21"] > sim_end
    assert by_id["A22"] is None


async def test_all_star_dispatch_stub_units(pool) -> None:
    """用例三：全员 star 分发（M1 8 人口径）+ secondary/background 同接口桩由 mock 驱动。"""
    await pool.execute("UPDATE agents SET cognition_tier='star' WHERE id = ANY($1)", LOD_AGENT_IDS)
    await pool.execute("UPDATE agents SET next_due_sim=NULL WHERE id = ANY($1)", LOD_AGENT_IDS)
    lod = LodScheduler(pool)
    assert await lod.collect_due(tick=0, sim_now=T0) == LOD_AGENT_IDS, "全员 star 时分发全体"
    # 同接口桩：secondary 廉价决策 / background 日摘要（mock 驱动，产 EventDraft）
    class GW:
        def __init__(self) -> None:
            self._mock = MockProvider()

        def gen_params(self, task_type: str) -> dict:
            return {}

        async def chat(self, task_type, messages, gen_params=None, *, seed=None, agent_id=None, sim_time=None):
            return await self._mock.chat(task_type, messages, gen_params, seed=seed)

    gw = GW()
    ctx = UnitContext(pool=pool, gateway=gw, sim_now=T0, tick=0, rng_seed=0)
    sec = await SecondaryUnit("A21").run(ctx)
    assert sec and sec[0].type == "agent.think" and sec[0].actors == ["A21"]
    bg = await BackgroundUnit("A22").run(ctx)
    assert bg and bg[0].actors == ["A22"]
    assert await StarUnit("A20").run(ctx) == []  # star 六步循环归 Pipeline，本类持接口
    await pool.execute(
        "UPDATE agents SET cognition_tier=CASE id WHEN 'A20' THEN 'star' WHEN 'A21' THEN 'secondary' ELSE 'background' END WHERE id = ANY($1)",
        LOD_AGENT_IDS,
    )
