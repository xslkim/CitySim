"""T-REL-02 关系结算矩阵与自然回归验收。

口径：02 文档 T-REL-02 验收 1~3（P0 行每行 1 例 / 同日同对第 3 次起减半 / 周回归 tension=30/80/100
三档非线性 + relation.changed 落库且 changes[].cause 可 ::bigint 转换）。
数值一律从 config/relations.yaml（01 §3.2/§3.4/§3.5 镜像）读值复算（00 §7 DoD 6）。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.adjudicator.state_events import StateAggregator
from worldsim.relations.relations import RelationEngine
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 10, 0, 0, tzinfo=LOCAL_TZ)  # 周一 10:00
REL_AGENT_IDS = ["A25", "A26", "A27"]
CFG = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "relations.yaml").read_text(encoding="utf-8"))

P0_ROWS = [  # 01 §3.2 P0 行（任务书"P0 行 M1 全实现"）
    "chat_enjoyable", "chat_flat", "argue", "argue_insulted", "gossip_bond",
    "invite_accepted", "invite_refused", "appointment_good", "stood_up",
]


@pytest.fixture(scope="module")
def engine_cfg() -> dict:
    return CFG


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in REL_AGENT_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, 'star', '{}'::jsonb, '{}'::jsonb, 100000, 'apt.lobby')
            ON CONFLICT (id) DO NOTHING
            """,
            aid, f"测试{aid}",
        )
    await p.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", REL_AGENT_IDS)
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", REL_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", REL_AGENT_IDS)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def rel(pool) -> tuple[RelationEngine, StateAggregator]:
    return RelationEngine(pool, CFG), StateAggregator(pool)


@pytest.mark.parametrize("kind", P0_ROWS)
async def test_matrix_p0_row(rel, pool, kind: str) -> None:
    """验收 1：P0 行每行 1 例——按 yaml 镜像值结算、scope 语义、clamp 值域。"""
    engine, agg = rel
    row = CFG["matrix"][kind]
    # 预置非零存量（tension=50），避免 clamp 掩盖矩阵原值（tension 下界 0）
    await pool.execute(
        "INSERT INTO relations (a_id, b_id, affinity, tension) VALUES ('A25', 'A26', 0, 50), ('A26', 'A25', 0, 50)"
    )
    changes = await engine.settle(agg, kind=kind, a_id="A25", b_id="A26", cause="301")
    scope = row.get("scope", "a_to_b")
    assert len(changes) == (2 if scope == "both" else 1), f"{kind} scope={scope}"
    c = changes[0]
    assert (c["a_id"], c["b_id"]) == ("A25", "A26")
    assert c["delta_affinity"] == row["delta_affinity"] and c["delta_tension"] == row["delta_tension"]
    cached = await pool_fetch_rel(agg, "A25", "A26")
    assert cached == (row["delta_affinity"], 50 + row["delta_tension"])


async def pool_fetch_rel(agg: StateAggregator, a: str, b: str) -> tuple[int, int]:
    row = await agg._pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id=$1 AND b_id=$2", a, b)
    return int(row["affinity"]), int(row["tension"])


async def test_chat_band_and_same_day_halving(rel, pool) -> None:
    """验收 1 减半规则用例：chat 自评档位（01 §7）+ 当日同对第 3 次起减半（01 §3.2 chat 行）。"""
    engine, agg = rel
    assert engine.chat_band(7, 9) == "enjoyable" and engine.chat_band(10, 7) == "enjoyable"
    assert engine.chat_band(1, 3) == "perfunctory"
    assert engine.chat_band(4, 6) == "flat"
    # 构造当日已落库 2 场 dialogue.chat（事件流为减半计数数据源）
    for i in range(2):
        await pool.execute(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES ($1, $2, 'dialogue.chat', 'agent:A25', 'autonomous', $3, 'public', '{}'::jsonb)
            """,
            100 + i, T0 + dt.timedelta(minutes=5 * i), ["A25", "A26"],
        )
    enjoyable = CFG["matrix"]["chat_enjoyable"]
    halving_from = CFG["chat"]["same_pair_daily_halving_from"]
    assert halving_from == 3
    # 当日第 3 场 → 减半（向零取整，工程默认 D16）
    changes = await engine.settle_chat(agg, a_id="A25", b_id="A26", band="enjoyable", cause="310", sim_now=T0)
    assert len(changes) == 2, "chat 对称结算双方各自的值"
    assert changes[0]["delta_affinity"] == int(enjoyable["delta_affinity"] * 0.5)
    assert changes[0]["delta_tension"] == int(enjoyable["delta_tension"] * 0.5)
    # 次日计数清零 → 不减半
    tomorrow = T0 + dt.timedelta(days=1)
    changes = await engine.settle_chat(agg, a_id="A25", b_id="A26", band="enjoyable", cause="311", sim_now=tomorrow)
    assert changes[0]["delta_affinity"] == enjoyable["delta_affinity"]
    # 敷衍档不进关系结算（冷却归 T-REL-04）
    assert await engine.settle_chat(agg, a_id="A25", b_id="A26", band="perfunctory", cause="312", sim_now=tomorrow) == []


async def test_tension_nonlinear_regression(rel) -> None:
    """验收 2 指定用例：tension=30/80/100 三档非线性回归量符合 01 §3.2（max(5%, tension×10%)）。"""
    engine, _ = rel
    reg = CFG["regression"]
    for tension, expected_rate in ((30, reg["tension_min_rate"]), (80, 0.08), (100, reg["tension_scale"])):
        _, d_ten = engine.regression_deltas(affinity=0, tension=tension)
        assert d_ten == -round(tension * expected_rate), f"tension={tension} 回归率 {expected_rate}"
    # affinity 向 0 回归 5%（双向符号）
    d_aff, _ = engine.regression_deltas(affinity=40, tension=0)
    assert d_aff == -round(40 * reg["affinity_rate"])
    d_aff, _ = engine.regression_deltas(affinity=-40, tension=0)
    assert d_aff == round(40 * reg["affinity_rate"])


async def test_weekly_regression_settles_event(rel, pool) -> None:
    """验收 3：回归跑批后 relation.changed ≥1 且 changes[].cause 可 ::bigint 转换；缓存行同步。"""
    engine, agg = rel
    await pool.execute(
        "INSERT INTO relations (a_id, b_id, affinity, tension) VALUES ('A26', 'A27', 40, 80), ('A27', 'A26', -10, 30)"
    )
    changes = await engine.weekly_regression(agg, cause="399")
    assert changes, "有变更才落"
    seqs = await agg.flush(tick=99, sim_now=T0, trigger="system", rng_seed=99)
    assert len(seqs) == 1
    count = await pool.fetchval("SELECT count(*) FROM events WHERE seq = ANY($1::bigint[]) AND type='relation.changed'", seqs)
    assert count >= 1
    castable = await pool.fetchval(
        """
        SELECT bool_and((c->>'cause') ~ '^[0-9]+$' AND (c->>'cause')::bigint >= 0)
        FROM events, LATERAL jsonb_array_elements(payload->'changes') c WHERE seq = ANY($1::bigint[])
        """,
        seqs,
    )
    assert castable is True, "changes[].cause 可 ::bigint 转换（00 §4 红线 3）"
    row = await pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A26' AND b_id='A27'")
    assert row["tension"] == 80 - round(80 * 0.08), "tension=80 档按 8%/周 回归"
    assert row["affinity"] == 40 - round(40 * 0.05)
