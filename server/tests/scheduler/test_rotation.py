"""T-LOD-03 基尼驱动明星轮换（路径一迟滞）与升格衔接验收。

口径：02 文档 T-LOD-03 验收 1~3（滞回带不抖动 / churn ≤2/日 / 超员末位降格 / star 数恒 8 +
test_star_promotion_catchup_reflection 升 star 后 1 模拟日内存在 kind='reflection' 记忆）。

隔离口径：events append-only 不可清库，本模块用私有 scratch 库 + 每用例时间锚点间距 ≥8 天
（超出评分 7 日/2 日窗口），用例间零污染。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.scheduler.rotation import StarRotation, gini_coefficient
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 18, 23, 30, 0, tzinfo=LOCAL_TZ)  # 周日晚（模拟日界前）
IDS = [f"A{20 + i:02d}" for i in range(13)]  # A20~A32 共 13 名合成 agent
DB_NAME = "worldsim_rotation_test"

PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"
DDL_DIR = Path(__file__).resolve().parents[2] / "ddl"

_NEEDS = json.dumps({"energy": 70, "hunger": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70})


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )


class GW:
    """mock 网关桩（chat=MockProvider，embed 同）。"""

    def __init__(self) -> None:
        self._mock = MockProvider()

    def gen_params(self, task_type: str) -> dict:
        return {}

    async def chat(self, task_type, messages, gen_params=None, *, seed=None, agent_id=None, sim_time=None):
        return await self._mock.chat(task_type, messages, gen_params, seed=seed)

    async def embed(self, texts, *, seed=None, agent_id=None, sim_time=None):
        return await self._mock.embed(texts, seed=seed)


@pytest_asyncio.fixture
async def pool():
    _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
    _psql(f"CREATE DATABASE {DB_NAME}")
    _psql("CREATE SCHEMA IF NOT EXISTS partman", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS vector", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman", db=DB_NAME)
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", DB_NAME, "-v", "ON_ERROR_STOP=1",
         "-f", str(DDL_DIR / "schema_v1.sql")],
        check=True, capture_output=True, text=True,
    )
    p = await asyncpg.create_pool(f"postgresql:///{DB_NAME}?host={SOCKET_DIR}", min_size=1, max_size=4)
    for aid in IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, 'background', '{}'::jsonb, $3::jsonb, 0, 'apt.lobby')
            """,
            aid, f"测试{aid}", _NEEDS,
        )
    try:
        yield p
    finally:
        await p.close()
        _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")


async def _reset_tiers(pool, *, stars: list[str], secondary: list[str]) -> None:
    await pool.execute("UPDATE agents SET cognition_tier='background', consecutive_over=0, consecutive_under=0 WHERE id = ANY($1)", IDS)
    if stars:
        await pool.execute("UPDATE agents SET cognition_tier='star' WHERE id = ANY($1)", stars)
    if secondary:
        await pool.execute("UPDATE agents SET cognition_tier='secondary' WHERE id = ANY($1)", secondary)


async def _touch(pool, agent_id: str, n: int, *, anchor: dt.datetime, days_ago: float, tick0: int) -> None:
    """给 agent 造 n 条出场事件（评分数据源），sim 时刻相对本用例锚点。"""
    for i in range(n):
        sim = anchor - dt.timedelta(days=days_ago, minutes=i)
        await pool.execute(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES ($1, $2, 'agent.think', $3, 'autonomous', $4, 'internal', '{}'::jsonb)
            """,
            tick0 + i, sim, f"agent:{agent_id}", [agent_id],
        )


async def test_hysteresis_no_flap(pool) -> None:
    """用例一：滞回带不抖动——连续次数不足不动（首日轮换 counters=1 < 2/3，零变动）。"""
    anchor = T0
    await _reset_tiers(pool, stars=IDS[:8], secondary=IDS[8:10])
    rot = StarRotation(pool, GW())
    out = await rot.rotation_tick(tick=1, sim_now=anchor)
    assert out["churn"] == 0 and out["promoted"] == [] and out["demoted"] == []
    assert await pool.fetchval("SELECT count(*) FROM agents WHERE cognition_tier='star'") == 8


async def test_churn_cap_two_per_day(pool) -> None:
    """用例二：churn ≤2/日——5 名候选达标 + 8 名在任 star 同达降格线时，当日仅 1 对互换（2 人）。"""
    anchor = T0 + dt.timedelta(days=8)
    await _reset_tiers(pool, stars=IDS[:8], secondary=IDS[8:13])
    await pool.execute("UPDATE agents SET consecutive_over=5 WHERE id = ANY($1)", IDS[8:13])
    await pool.execute("UPDATE agents SET consecutive_under=2 WHERE id = ANY($1)", IDS[:8])
    for aid in IDS[8:13]:
        await _touch(pool, aid, 10, anchor=anchor, days_ago=0.5, tick0=20000)
    for aid in IDS[:8]:
        await _touch(pool, aid, 10, anchor=anchor, days_ago=5, tick0=30000)
    rot = StarRotation(pool, GW())
    out = await rot.rotation_tick(tick=2, sim_now=anchor)
    assert out["churn"] == 2, "降格+补位各计 1，当日恰 1 对互换（04 §4.2 churn ≤2）"
    assert len(out["promoted"]) == 1 and len(out["demoted"]) == 1
    assert await pool.fetchval("SELECT count(*) FROM agents WHERE cognition_tier='star'") == 8


async def test_overfull_demote_lowest(pool) -> None:
    """用例三：超员末位降格——9 名 star 时 score 末位降入 secondary，star 数回到 8。"""
    anchor = T0 + dt.timedelta(days=16)
    await _reset_tiers(pool, stars=IDS[:9], secondary=[])
    for i, aid in enumerate(IDS[:8]):  # 前 8 名高分且互异
        await _touch(pool, aid, 20 + i * 5, anchor=anchor, days_ago=0.5, tick0=40000 + i * 100)
    # IDS[8] = 末位：近 2 日零交互（int=0）、7 日窗全量最高（cold=0）→ score=0 唯一末位
    await _touch(pool, IDS[8], 60, anchor=anchor, days_ago=5, tick0=50000)
    rot = StarRotation(pool, GW())
    out = await rot.rotation_tick(tick=3, sim_now=anchor)
    assert IDS[8] in out["demoted"], f"末位 {IDS[8]} 降格，实际 {out}"
    assert await pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", IDS[8]) == "secondary"
    assert await pool.fetchval("SELECT count(*) FROM agents WHERE cognition_tier='star'") == 8
    # 路径一降格事件 payload 含 score 分解与 gini（internal 键，06 §1.2）
    payload = json.loads(await pool.fetchval(
        "SELECT payload::text FROM events WHERE type='agent.demoted' AND $1 = ANY(actors) ORDER BY seq DESC LIMIT 1",
        IDS[8],
    ))
    assert "score" in payload and "gini" in payload and payload["to_tier"] == "secondary"


async def test_star_count_constant_8(pool) -> None:
    """用例四 + 验收 3（SQL 口径）：任意轮换后 star 数恒 8。"""
    anchor = T0 + dt.timedelta(days=24)
    await _reset_tiers(pool, stars=IDS[:8], secondary=IDS[8:])
    rot = StarRotation(pool, GW())
    for day in range(3):
        await rot.rotation_tick(tick=10 + day, sim_now=anchor + dt.timedelta(days=day))
        n = await pool.fetchval("SELECT count(*) FROM agents WHERE cognition_tier='star'")
        assert n == 8, f"第 {day + 1} 次轮换后 star 数 = {n}（SQL：... WHERE cognition_tier='star' 恒 8）"


async def test_star_promotion_catchup_reflection(pool) -> None:
    """验收 2 指定用例：升 star 后 1 模拟日内存在其 kind='reflection' 记忆（04 §4.3 追赶反思）。"""
    anchor = T0 + dt.timedelta(days=32)
    await _reset_tiers(pool, stars=IDS[:7], secondary=[IDS[7]])
    await pool.execute("UPDATE agents SET consecutive_over=3 WHERE id=$1", IDS[7])
    await _touch(pool, IDS[7], 10, anchor=anchor, days_ago=0.5, tick0=60000)  # 候选高分
    rot = StarRotation(pool, GW())
    out = await rot.rotation_tick(tick=20, sim_now=anchor)
    assert IDS[7] in out["promoted"]
    mem = await pool.fetchrow(
        """
        SELECT kind, content, importance FROM memories
        WHERE agent_id=$1 AND kind='reflection' AND sim_time >= $2 AND sim_time < $3
        """,
        IDS[7], anchor, anchor + dt.timedelta(days=1),
    )
    assert mem is not None, "升 star 后 1 模拟日内存在 kind='reflection' 记忆（04 §10.1 ⑤ 内核侧保证）"
    assert mem["importance"] >= 7
    plan = await pool.fetchval(
        "SELECT count(*) FROM memories WHERE agent_id=$1 AND kind='plan'", IDS[7],
    )
    assert plan >= 1, "追赶反思同时产出当前计划（首个明星层认知循环初始上下文，04 §4.3）"


async def test_gini_coefficient() -> None:
    """基尼系数纯函数：全等 = 0，全集中 → 趋 1。"""
    assert gini_coefficient([5, 5, 5, 5]) == 0.0
    assert gini_coefficient([]) == 0.0 and gini_coefficient([0, 0]) == 0.0
    assert gini_coefficient([0, 0, 0, 10]) > 0.7
