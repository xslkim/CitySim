"""T-ADJ-07 grade 自动打分（ui.grade 初值）验收。

口径：02 文档 T-ADJ-07 验收 1~2（4 条规则逐条命中/不命中、定级边界、无 witnesses 键类型按空集处理 +
append-only 触发器拒绝 UPDATE 的 M0 双保险回归）+ 有效 grade 读取接口（初值 ⊕ 最新复核）。
"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio

from worldsim.adjudicator.grade import Grader, effective_grade, level_of
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 20, 0, 0, tzinfo=LOCAL_TZ)
IDS = ["A20", "A21", "A22", "A23"]


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'M', 28, 'star', '{}'::jsonb, '{}'::jsonb, 0, 'apt.lobby')
            ON CONFLICT (id) DO NOTHING
            """,
            aid, f"测试{aid}",
        )
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM memories WHERE agent_id = ANY($1)", IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", IDS)
        finally:
            await conn.close()


async def test_r1_involved_union(pool) -> None:
    """R1：actors+witnesses+cites 波及者并集 ≥2 命中；无 witnesses 键类型按空集处理（D5）。"""
    g = Grader(pool)
    assert await g.grade(type_="agent.move", actors=["A20"], payload={}) == "C"  # R1 不命中
    assert await g.grade(type_="dialogue.chat", actors=["A20", "A21"], payload={}) == "C"  # 仅 R1 命中 → C
    assert await g.grade(type_="dialogue.chat", actors=["A20", "A21"], payload={"caused_by": "7"}) == "B"  # R1+R4 → B
    # witnesses 并集（dialogue 域注册键）
    assert await g.grade(type_="dialogue.argue", actors=["A20"],
                         payload={"witnesses": ["A21", "A22"]}) == "C"  # R1 命中但仅 1 条
    grade = await g.grade(type_="dialogue.argue", actors=["A20"],
                          payload={"witnesses": ["A21"]}, rel_hit=True)
    assert grade == "B"  # R1+R2


async def test_r1_gossip_cites_involved(pool) -> None:
    """R1 gossip cites 波及者：被引用记忆的 source_event_seq 事件的 actors 计入并集。"""
    src = await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
        VALUES (1, $1, 'dialogue.argue', 'agent:A22', 'autonomous', '{A22,A23}', 'public', '{}')
        RETURNING seq
        """,
        T0,
    )
    mid = await pool.fetchval(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance, source_event_seq)
        VALUES ('A20', $1, 'event', '我看到的', '我看到的', 5, $2) RETURNING id
        """,
        T0, src,
    )
    g = Grader(pool)
    grade = await g.grade(type_="dialogue.gossip", actors=["A20", "A21"],
                          payload={"cites": [str(mid)]})
    assert grade == "C"  # 仅 R1 命中（cites 波及 4 人并集）→ C
    assert await g.involved_actors(actors=["A20", "A21"], payload={"cites": [str(mid)]}) == {"A20", "A21", "A22", "A23"}


async def test_r2_r3_r4_and_level_boundaries(pool) -> None:
    """R2/R3/R4 逐条命中/不命中 + 定级边界（≥3 A / 2 B / 0~1 C，04 §6.6）。"""
    g = Grader(pool)
    base = {"type_": "dialogue.confess", "actors": ["A20", "A21"], "payload": {}}
    assert await g.grade(**base, rel_hit=True, mood_hit=True, followups=True) == "A"   # 4 命中
    assert await g.grade(**base, rel_hit=True, mood_hit=True, followups=False) == "A"  # 3 命中
    assert await g.grade(**base, rel_hit=True) == "B"                                   # R1+R2
    assert await g.grade(**base, mood_hit=True) == "B"                                  # R1+R3
    assert await g.grade(**base, followups=True) == "B"                                 # R1+R4
    assert await g.grade(**base) == "C"                                                 # 仅 R1
    assert level_of(4) == "A" and level_of(3) == "A" and level_of(2) == "B"
    assert level_of(1) == "C" and level_of(0) == "C"


async def test_append_only_trigger_rejects_update(pool) -> None:
    """验收 2（SQL）：UPDATE events 必须被 append-only 触发器拒绝（M0 双保险回归）。"""
    seq = await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload, ui)
        VALUES (1, $1, 'agent.think', 'agent:A20', 'autonomous', '{A20}', 'internal', '{}', '{"grade":"C"}')
        RETURNING seq
        """,
        T0,
    )
    with pytest.raises(asyncpg.exceptions.RaiseError):
        await pool.execute(
            "UPDATE events SET ui=jsonb_set(ui,'{grade}','\"A\"') WHERE seq=$1", seq,
        )


async def test_effective_grade_initial_and_revise(pool) -> None:
    """有效 grade = 初值 ⊕ 最新 director.grade_revise（接口预留，04 §6.6/06 §2）。"""
    seq = await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload, ui)
        VALUES (1, $1, 'dialogue.confess', 'agent:A20', 'autonomous', '{A20,A21}', 'public',
                '{"participants":["A20","A21"],"result":"accepted","lines":[]}', '{"grade":"B"}')
        RETURNING seq
        """,
        T0,
    )
    assert await effective_grade(pool, seq) == "B"  # 无复核 → 初值
    await pool.execute(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
        VALUES (2, $1, 'director.grade_revise', 'director', 'director', '{}', 'public',
                $2::jsonb)
        """,
        T0, json.dumps({"target_seq": str(seq), "new_grade": "A", "reason": "K3 终审上调"}),
    )
    assert await effective_grade(pool, seq) == "A"  # 最新复核覆盖（事件行零 UPDATE）
    row = await pool.fetchval("SELECT ui->>'grade' FROM events WHERE seq=$1", seq)
    assert row == "B", "原事件行 ui.grade 初值不变（append-only）"
