"""聚合状态事件机制验收（04 §6.5 / 06 §1.2；T-ADJ-06 机制支撑，波次 2a 交付段）。

口径：每 tick ≤2 条、无变更不落、trigger 取值、internal 无展示键、cause 裸 seq 形态、
缓存列与事件流重建一致（04 §10.1 ⑥ 地基）。
"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio

from worldsim.adjudicator.state_events import (
    StateAggregator,
    rebuild_needs,
    rebuild_relation,
)
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 9, 0, 0, tzinfo=LOCAL_TZ)
SE_AGENT_IDS = ["A20", "A21", "A22"]

_INITIAL_NEEDS = {"hunger": 70, "energy": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70}


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in SE_AGENT_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, 'star', '{}'::jsonb, $3::jsonb, 100000, 'apt.lobby')
            ON CONFLICT (id) DO UPDATE SET needs=$3::jsonb
            """,
            aid, f"测试{aid}", json.dumps(_INITIAL_NEEDS),
        )
    await p.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", SE_AGENT_IDS)
    try:
        yield p
    finally:
        # events append-only（04 §5.2 触发器对 owner 亦生效）：不清事件，用例以 baseline seq 隔离
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", SE_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", SE_AGENT_IDS)
        finally:
            await conn.close()


async def test_flush_cap_two_per_tick(pool) -> None:
    """每 tick ≤2 条：多种 needs 变更 + 多条关系变更合并后恰落 2 条事件。"""
    agg = StateAggregator(pool)
    await agg.apply_needs_delta(agent_id="A20", need="hunger", delta=-5, cause="101")
    await agg.apply_needs_delta(agent_id="A20", need="mood", delta=3, cause="101")
    await agg.apply_needs_delta(agent_id="A21", need="social", delta=-1.5, cause="102")
    await agg.apply_relation_delta(a_id="A20", b_id="A21", delta_affinity=3, delta_tension=-1, cause="103")
    await agg.apply_relation_delta(a_id="A21", b_id="A20", delta_affinity=-4, delta_tension=8, cause="104")
    baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    seqs = await agg.flush(tick=7, sim_now=T0, trigger="autonomous", rng_seed=7)
    assert len(seqs) == 2, "needs+relation 各合并为 1 条，每 tick 至多 2 条"
    rows = await pool.fetch("SELECT type, payload FROM events WHERE seq > $1 ORDER BY seq", baseline)
    assert [r["type"] for r in rows] == ["state.needs_delta", "relation.changed"]
    needs_changes = json.loads(rows[0]["payload"])["changes"]
    rel_changes = json.loads(rows[1]["payload"])["changes"]
    assert len(needs_changes) == 3 and len(rel_changes) == 2, "本 tick 全部变更合并进同一条事件"
    assert {c["need"] for c in needs_changes} == {"hunger", "mood", "social"}
    assert all(c["cause"].isdigit() for c in [*needs_changes, *rel_changes]), "cause 全为裸 seq 数字字符串"
    # SQL 抽查：changes[].cause 可 ::bigint 转换（00 §4 红线 3 回归）
    castable = await pool.fetchval(
        """
        SELECT bool_and((c->>'cause') ~ '^[0-9]+$') FROM events,
        LATERAL jsonb_array_elements(payload->'changes') c
        WHERE seq = ANY($1::bigint[])
        """,
        seqs,
    )
    assert castable is True


async def test_no_change_no_event(pool) -> None:
    """无变更不落：空缓冲 flush 与 clamp 无-op 变更均不产事件。"""
    agg = StateAggregator(pool)
    baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    assert await agg.flush(tick=1, sim_now=T0, trigger="system", rng_seed=1) == []
    # clamp 到值域边界后无实际变化 → 不记录
    await agg.apply_needs_delta(agent_id="A20", need="hunger", delta=999, cause="1")  # 70→100
    change = await agg.apply_needs_delta(agent_id="A20", need="hunger", delta=5, cause="1")  # 已 100，无-op
    assert change is None
    seqs = await agg.flush(tick=1, sim_now=T0, trigger="system", rng_seed=1)
    assert len(seqs) == 1, "仅首次有效变更落库"
    row = await pool.fetchrow("SELECT payload FROM events WHERE seq=$1", seqs[0])
    changes = json.loads(row["payload"])["changes"]
    assert changes == [{"agent_id": "A20", "need": "hunger", "delta": 30.0, "new_value": 100.0, "cause": "1"}]
    assert await pool.fetchval("SELECT count(*) FROM events WHERE seq > $1", baseline) == 1


async def test_trigger_and_internal_no_display_keys(pool) -> None:
    """trigger 取引发源（autonomous/system），visibility='internal' 且不携带展示文本键。"""
    agg = StateAggregator(pool)
    await agg.apply_needs_delta(agent_id="A20", need="energy", delta=-2, cause="9")
    seqs = await agg.flush(tick=2, sim_now=T0, trigger="system", rng_seed=2)
    row = await pool.fetchrow("SELECT trigger, visibility, source, payload FROM events WHERE seq=$1", seqs[0])
    assert row["trigger"] == "system" and row["source"] == "system"
    assert row["visibility"] == "internal"
    payload = json.loads(row["payload"])
    assert set(payload.keys()) == {"changes"}, "internal 事件不携带展示文本键（06 §1.2 口径块）"
    with pytest.raises(ValueError):
        await agg.flush(tick=2, sim_now=T0, trigger="world", rng_seed=2)
    with pytest.raises(ValueError):
        await agg.apply_needs_delta(agent_id="A20", need="energy", delta=-1, cause="e1089")  # 禁止 'e<seq>' 作值


async def test_cache_columns_and_labels(pool) -> None:
    """缓存列：agents.needs / relations UPSERT、clamp、labels 增删、last_event_seq 留痕。"""
    agg = StateAggregator(pool)
    await agg.apply_relation_delta(
        a_id="A20", b_id="A21", delta_affinity=130, delta_tension=120, cause="55",
        labels_added=["暗恋"],
    )
    row = await pool.fetchrow("SELECT affinity, tension, labels, last_event_seq FROM relations WHERE a_id='A20' AND b_id='A21'")
    assert row["affinity"] == 100 and row["tension"] == 100, "affinity/tension clamp 到值域"
    assert row["labels"] == ["暗恋"] and row["last_event_seq"] == 55
    await agg.apply_relation_delta(
        a_id="A20", b_id="A21", delta_affinity=-250, delta_tension=-200, cause="56",
        labels_removed=["暗恋"], labels_added=["前任"],
    )
    row = await pool.fetchrow("SELECT affinity, tension, labels FROM relations WHERE a_id='A20' AND b_id='A21'")
    assert (row["affinity"], row["tension"]) == (-100, 0)
    assert row["labels"] == ["前任"], "labels 增删语义（加并集、减差集）"


async def test_rebuild_equals_cache(pool) -> None:
    """重建对账（04 §10.1 ⑥ 地基）：初始值 + Σ 事件流 == 缓存列逐项相等。"""
    agg = StateAggregator(pool)
    await agg.apply_needs_delta(agent_id="A22", need="hunger", delta=-4.33, cause="10")
    await agg.apply_needs_delta(agent_id="A22", need="mood", delta=15, cause="11")
    await agg.apply_relation_delta(a_id="A22", b_id="A20", delta_affinity=6, delta_tension=-4, cause="12", labels_added=["师徒"])
    await agg.flush(tick=3, sim_now=T0, trigger="autonomous", rng_seed=3)
    await agg.apply_needs_delta(agent_id="A22", need="hunger", delta=-1, cause="13")
    await agg.apply_relation_delta(a_id="A22", b_id="A20", delta_affinity=-2, delta_tension=9, cause="14")
    await agg.flush(tick=4, sim_now=T0, trigger="system", rng_seed=4)

    rebuilt = await rebuild_needs(pool, "A22", _INITIAL_NEEDS)
    cache = await agg.read_needs("A22")
    assert rebuilt == {k: cache[k] for k in rebuilt}, "needs 重建值 == 缓存列"
    aff, ten, labels = await rebuild_relation(pool, "A22", "A20")
    row = await pool.fetchrow("SELECT affinity, tension, labels FROM relations WHERE a_id='A22' AND b_id='A20'")
    assert (aff, ten, labels) == (row["affinity"], row["tension"], row["labels"]), "关系边重建 == 缓存行"
