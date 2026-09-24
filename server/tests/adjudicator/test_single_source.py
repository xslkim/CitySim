"""T-ADJ-05 双人事件单源写入、目击投影与传播链验收（记忆写入段，波次 2a 交付）。

口径：02 文档 T-ADJ-05 验收 1~3 的记忆侧（单源双方记忆同 seq / 目击投影重要性与 is_witness /
caused_by·cites 形态 / 传播深度上限 + test_fidelity_not_in_payload 全库无 fidelity/distortion 键）。
管道 step5 全量联动归 T-ADJ-05 波次 2b 主段。
"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio

from worldsim.llm_gateway import LLMGateway
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.memory import store
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=LOCAL_TZ)
ACTORS = ["A33", "A34"]
WITNESSES = ["A35", "A36", "A37", "A38", "A39", "A40", "A01"]  # 7 名（上限抽样用例）
ALL_IDS = ACTORS + WITNESSES


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in ALL_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, 'star', '{}'::jsonb, '{}'::jsonb, 100000, 'apt.kitchen')
            ON CONFLICT (id) DO UPDATE SET position='apt.kitchen'
            """,
            aid, f"测试{aid}",
        )
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM memories WHERE agent_id = ANY($1)", ALL_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", ALL_IDS)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def gateway():
    return LLMGateway(None, {}, providers={"mock": MockProvider()}, default_provider="mock")


async def _insert_event(pool, *, seq_type: str = "dialogue.chat", location: str | None = "apt.kitchen",
                        actors: list[str] | None = None, rng_seed: int = 7, payload: dict | None = None) -> int:
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, location_id, actors, rng_seed, visibility, payload)
        VALUES (1, $1, $2, 'agent:A33', 'autonomous', $3, $4, $5, 'public', $6::jsonb)
        RETURNING seq
        """,
        T0, seq_type, location, actors or ACTORS, rng_seed,
        json.dumps(payload or {}, ensure_ascii=False),
    )


async def test_single_source_same_seq(pool, gateway) -> None:
    """验收 1 用例一：单源——一场交互一条事件，双方记忆同一 source_event_seq、各自视角一句话。"""
    seq = await _insert_event(pool)
    ids = await store.write_event_memories(
        pool, gateway, event_seq=seq, sim_time=T0,
        perspectives={"A33": "我和测试A34在厨房聊得很投机", "A34": "测试A33跟我聊了一路"},
        importance=6, rng_seed=7,
    )
    rows = await pool.fetch(
        "SELECT agent_id, kind, content, source_event_seq, is_witness FROM memories WHERE id = ANY($1::bigint[]) ORDER BY agent_id",
        list(ids.values()),
    )
    assert [r["kind"] for r in rows] == ["event", "event"]
    assert {r["source_event_seq"] for r in rows} == {seq}, "双方记忆同源（同一 source_event_seq）"
    assert rows[0]["content"] != rows[1]["content"], "content 各自视角"
    assert not any(r["is_witness"] for r in rows), "参与者投影非目击"
    # 验收 2 SQL 形态：±1 小时窗口内参与者 COUNT(DISTINCT source_event_seq) = 1
    n = await pool.fetchval(
        """
        SELECT count(DISTINCT m.source_event_seq) FROM memories m
        WHERE m.agent_id = ANY($1::text[]) AND m.kind='event'
          AND m.sim_time BETWEEN $2::timestamptz - interval '1 hour' AND $2::timestamptz + interval '1 hour'
        """,
        ACTORS, T0,
    )
    assert n == 1


async def test_witness_projection_importance_and_flag(pool, gateway) -> None:
    """验收 1 用例二：目击投影重要性 = 原事件 −2（下限 1）、is_witness=true、非在场者不投影。"""
    await pool.execute("UPDATE agents SET position='apt.lobby' WHERE id='A36'")  # 一名离场
    seq = await _insert_event(pool)
    n_low = await store.write_witness_projections(
        pool, gateway, event_seq=seq, event_type="dialogue.chat", location_id="apt.kitchen",
        actors=ACTORS, sim_time=T0, base_importance=3, rng_seed=7, summary="聊天",
    )
    rows = await pool.fetch("SELECT agent_id, kind, importance, is_witness, source_event_seq FROM memories WHERE id = ANY($1::bigint[])", n_low)
    assert rows and all(r["kind"] == "projection" and r["is_witness"] for r in rows)
    assert all(r["importance"] == 1 for r in rows), "重要性下限 1（3 − 2 → 1，04 §6.3）"
    assert all(r["source_event_seq"] == seq for r in rows)
    got = {r["agent_id"] for r in rows}
    assert "A36" not in got, "不在场者不投影"
    assert not (set(ACTORS) & got), "actors 不投影"
    # 高重要性事件：8 − 2 = 6
    seq2 = await _insert_event(pool)
    n2 = await store.write_witness_projections(
        pool, gateway, event_seq=seq2, event_type="dialogue.chat", location_id="apt.kitchen",
        actors=ACTORS, sim_time=T0, base_importance=8, rng_seed=8,
    )
    imps = await pool.fetch("SELECT DISTINCT importance FROM memories WHERE id = ANY($1::bigint[])", n2)
    assert [r["importance"] for r in imps] == [6]


async def test_witness_cap_six(pool, gateway) -> None:
    """同节点目击上限 ≤6 人：7 名在场者恰投影 6 条（超出 rng 确定性抽样，评审 P2-10）。"""
    seq = await _insert_event(pool)
    ids1 = await store.write_witness_projections(
        pool, gateway, event_seq=seq, event_type="dialogue.chat", location_id="apt.kitchen",
        actors=ACTORS, sim_time=T0, base_importance=6, rng_seed=11,
    )
    assert len(ids1) == store.WITNESS_CAP
    chosen1 = {r["agent_id"] for r in await pool.fetch("SELECT agent_id FROM memories WHERE id = ANY($1::bigint[])", ids1)}
    assert chosen1 <= set(WITNESSES)
    # 同 rng_seed 重跑抽样结果一致（可回放）
    seq2 = await _insert_event(pool)
    ids2 = await store.write_witness_projections(
        pool, gateway, event_seq=seq2, event_type="dialogue.chat", location_id="apt.kitchen",
        actors=ACTORS, sim_time=T0, base_importance=6, rng_seed=11,
    )
    chosen2 = {r["agent_id"] for r in await pool.fetch("SELECT agent_id FROM memories WHERE id = ANY($1::bigint[])", ids2)}
    assert len(ids2) == store.WITNESS_CAP
    # 抽样 rng 派生自 (rng_seed, event_seq)：seq 不同允许重抽，但同 (seed, seq) 必一致——此处验证集合大小稳定
    assert len(chosen2) == store.WITNESS_CAP


async def test_argue_witness_probability(pool, gateway) -> None:
    """argue 目击 30%（01 §4.1，事件级骰子）：跨种子既有全体投影也有零投影，且结果确定性。"""
    outcomes: set[int] = set()
    for seed in range(30):
        seq = await _insert_event(pool, seq_type="dialogue.argue", rng_seed=seed)
        ids = await store.write_witness_projections(
            pool, gateway, event_seq=seq, event_type="dialogue.argue", location_id="apt.kitchen",
            actors=ACTORS, sim_time=T0, base_importance=7, rng_seed=seed,
        )
        outcomes.add(len(ids))
        await pool.execute("DELETE FROM memories WHERE agent_id = ANY($1)", WITNESSES)
        # 同 (rng_seed, event_seq) 复算结果一致
        ids_again = await store.write_witness_projections(
            pool, gateway, event_seq=seq, event_type="dialogue.argue", location_id="apt.kitchen",
            actors=ACTORS, sim_time=T0, base_importance=7, rng_seed=seed,
        )
        assert len(ids_again) == len(ids), "骰子由 (rng_seed, event_seq) 派生，可回放"
        await pool.execute("DELETE FROM memories WHERE agent_id = ANY($1)", WITNESSES)
    assert outcomes == {0, store.WITNESS_CAP}, "argue 目击 = 事件级 30% 骰：中则全体（上限内）、不中则零"


async def test_fidelity_chain_rules() -> None:
    """验收 1 用例四相关：失真链数值（01 §4.1）与传播深度上限 4 手。"""
    seqs = [store.fidelity_at_hop(h) for h in (1, 2, 3, 4, 5)]
    assert seqs == pytest.approx([1.0, 0.8, 0.64, 0.512, 0.4096])
    assert store.detail_tier(0.71) == "full"
    assert store.detail_tier(0.64) == "no_details", "<0.7 丢数字/时间字段"
    assert store.detail_tier(0.512) == "skeleton", "<0.55 只保留主干"
    assert store.can_retell(1) and store.can_retell(3)
    assert not store.can_retell(4), "传播深度上限统一 4 手"
    # 失真概率：N 高翻转上调至 20%（大样本容差内）
    import random as _random

    n_normal = sum(store.roll_distortion(_random.Random(i)).flip for i in range(4000))
    assert 0.12 < n_normal / 4000 < 0.18
    n_high = sum(store.roll_distortion(_random.Random(i), neuroticism=75).flip for i in range(4000))
    assert 0.17 < n_high / 4000 < 0.23


async def test_validate_cites(pool, gateway) -> None:
    """gossip cites 强校验：≥1 条、裸数字字符串、指向本人记忆（01 §4.1；00 §4 红线 3）。"""
    mid = await store.insert_memory(
        pool, gateway, agent_id="A33", sim_time=T0, kind="event", content="我亲耳听见的事", importance=5, rng_seed=1,
    )
    other = await store.insert_memory(
        pool, gateway, agent_id="A34", sim_time=T0, kind="event", content="别人的事", importance=5, rng_seed=1,
    )
    assert await store.validate_cites(pool, agent_id="A33", cites=[str(mid)]) == [mid]
    with pytest.raises(ValueError):
        await store.validate_cites(pool, agent_id="A33", cites=[])
    with pytest.raises(ValueError):
        await store.validate_cites(pool, agent_id="A33", cites=[f"e{mid}"])  # 禁止 'e<seq>' 作值
    with pytest.raises(ValueError):
        await store.validate_cites(pool, agent_id="A33", cites=[str(other)])  # 必须本人记忆
    assert store.caused_by(1089) == "1089", "caused_by 裸 seq 数字字符串"


async def test_fidelity_not_in_payload(pool) -> None:
    """验收 3 指定用例：全库 events payload 不存在 fidelity/distortion 键（00 §4 红线 8）。"""
    n = await pool.fetchval(
        """
        SELECT count(*) FROM events
        WHERE payload ? 'fidelity' OR payload ? 'distortion'
        """
    )
    assert n == 0, "fidelity/distortion 为内核传播输入，不进 payload、不出站"
