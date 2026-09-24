"""T-ADJ-08 WSIM_REPLAY_MODE 与状态一致对账验收。

口径：02 文档 T-ADJ-08 验收 1（replay 下 LLM 调用拦截 / rng_seed 取数 / 状态一致三用例）。
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

from worldsim.adjudicator.state_events import StateAggregator
from worldsim.llm_gateway import LLMGateway, ReplayViolation
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_replay_test"
PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"
ROOT = Path(__file__).resolve().parents[2]
T0 = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=LOCAL_TZ)

sys_path_scripts = str(ROOT / "scripts")


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )


async def test_replay_blocks_llm_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """用例一：WSIM_REPLAY_MODE=replay 时 LLM 网关拒绝一切真实调用（门面 ReplayViolation，04 §5.3）。"""
    monkeypatch.setenv("WSIM_REPLAY_MODE", "replay")
    gw = LLMGateway(None, {}, providers={"mock": MockProvider()}, default_provider="mock")
    with pytest.raises(ReplayViolation):
        await gw.chat("star_decision", [{"role": "user", "content": "x"}], seed=1)
    with pytest.raises(ReplayViolation):
        await gw.embed(["x"], seed=1)
    monkeypatch.setenv("WSIM_REPLAY_MODE", "off")  # 对照：off 时 mock 正常
    result = await gw.chat("star_decision", [{"role": "user", "content": "x"}], seed=1)
    assert result.text


async def test_replay_rng_seed_from_events() -> None:
    """用例二：规则骰子一律从 events.rng_seed 取数（04 §5.3；缺失即报缺，禁止重算）。"""
    import sys

    sys.path.insert(0, sys_path_scripts)
    from replay_check import replay_rng_seed

    assert replay_rng_seed({"seq": 9, "rng_seed": 42}) == 42
    with pytest.raises(ValueError):
        replay_rng_seed({"seq": 9, "rng_seed": None})


@pytest_asyncio.fixture
async def pool():
    _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
    _psql(f"CREATE DATABASE {DB_NAME}")
    _psql("CREATE SCHEMA IF NOT EXISTS partman", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS vector", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman", db=DB_NAME)
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", DB_NAME, "-v", "ON_ERROR_STOP=1",
         "-f", str(ROOT / "ddl" / "schema_v1.sql")],
        check=True, capture_output=True, text=True,
    )
    p = await asyncpg.create_pool(f"postgresql:///{DB_NAME}?host={SOCKET_DIR}", min_size=1, max_size=4)
    needs = json.dumps({"hunger": 70.0, "energy": 70.0, "mood": 70.0, "social": 70.0, "wealth": 70.0, "achievement": 70.0})
    for aid, pos in (("A20", "apt.lobby"), ("A21", "apt.roof")):
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'M', 28, 'star', '{}'::jsonb, $3::jsonb, 0, $4)
            """,
            aid, f"测试{aid}", needs, pos,
        )
    try:
        yield p
    finally:
        await p.close()
        _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")


async def test_replay_state_consistent(pool) -> None:
    """用例三：状态一致——needs/mood 由 state.needs_delta、关系由 relation.changed、位置由 agent.move 链
    重建（04 §6.5/§5.3），与缓存列逐项相等（04 §10.1 ⑥ 口径）。"""
    import sys

    sys.path.insert(0, sys_path_scripts)
    from replay_check import replay_needs, replay_positions, replay_relation

    cutoff = T0 + dt.timedelta(days=1)
    agg = StateAggregator(pool)
    await agg.apply_needs_delta(agent_id="A20", need="hunger", delta=-5.5, cause="1")
    await agg.apply_needs_delta(agent_id="A20", need="mood", delta=16.0, cause="1")
    await agg.apply_needs_delta(agent_id="A21", need="social", delta=-1.25, cause="1")
    await agg.apply_relation_delta(a_id="A20", b_id="A21", delta_affinity=7, delta_tension=-3,
                                   labels_added=["闺蜜"], cause="1")
    seqs = await agg.flush(tick=1, sim_now=T0, trigger="system", rng_seed=1)
    assert len(seqs) == 2  # 聚合事件每 tick ≤2 条
    # 第二批（跨 tick）
    await agg.apply_relation_delta(a_id="A20", b_id="A21", delta_affinity=2, cause=str(seqs[0]))
    await agg.flush(tick=2, sim_now=T0 + dt.timedelta(minutes=5), trigger="system", rng_seed=2)
    # agent.move 链
    await pool.execute(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, location_id, actors, rng_seed, visibility, payload)
        VALUES (3, $1, 'agent.move', 'agent:A20', 'autonomous', 'apt.kitchen', '{A20}', 3, 'public',
                '{"from":"apt.lobby","to":"apt.kitchen","sim_cost_min":10}'::jsonb)
        """,
        T0 + dt.timedelta(minutes=10),
    )
    await pool.execute("UPDATE agents SET position='apt.kitchen' WHERE id='A20'")
    baseline_needs = {"hunger": 70.0, "energy": 70.0, "mood": 70.0, "social": 70.0, "wealth": 70.0, "achievement": 70.0}
    for aid in ("A20", "A21"):
        rebuilt = await replay_needs(pool, aid, baseline_needs, cutoff)
        cache = json.loads(await pool.fetchval("SELECT needs::text FROM agents WHERE id=$1", aid))
        assert {k: round(float(v), 2) for k, v in rebuilt.items()} == {k: round(float(v), 2) for k, v in cache.items()}, aid
    rebuilt_rel = await replay_relation(pool, "A20", "A21", (0, 0, []), cutoff)
    row = await pool.fetchrow("SELECT affinity, tension, labels FROM relations WHERE a_id='A20' AND b_id='A21'")
    assert rebuilt_rel == (int(row["affinity"]), int(row["tension"]), sorted(row["labels"]))
    positions = await replay_positions(pool, {"A20": "apt.lobby", "A21": "apt.roof"}, cutoff)
    assert positions == {"A20": "apt.kitchen", "A21": "apt.roof"}
