"""T-LOD-02 事件驱动即时升格 + secondary ≤16 硬上限 + LRU 挤出验收。

口径：02 文档 T-LOD-02 验收 1~3（被邀约当 tick 升格并当 tick 响应 / cooldown 回落 /
test_lru_evict_cap16 + agent.promoted payload.caused_by 裸 seq 形态 SQL 回归）。
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

from worldsim.scheduler.rotation import EventDrivenLOD, extract_targets
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 19, 0, 0, tzinfo=LOCAL_TZ)  # 周一 19:00 黄金档
IDS = [f"A{20 + i:02d}" for i in range(19)]  # A20~A38 共 19 名合成 agent
DB_NAME = "worldsim_promotion_test"

PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"
DDL_DIR = Path(__file__).resolve().parents[2] / "ddl"

_NEEDS = json.dumps({"energy": 70, "hunger": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70})


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )


@pytest_asyncio.fixture
async def pool():
    """私有 scratch 库（events append-only 不可清库；与共享库其他用例的同名 agent 事件隔离）。"""
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


async def _insert_chat(pool, *, tick: int, sim_now: dt.datetime, a: str, b: str, visibility: str = "public") -> int:
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
        VALUES ($1, $2, 'dialogue.chat', $3, 'autonomous', $4, $5, $6::jsonb)
        RETURNING seq
        """,
        tick, sim_now, f"agent:{a}", [a, b], visibility,
        json.dumps({"participants": [a, b], "mode": "small", "topic_ids": [], "lines": [], "witnesses": []},
                   ensure_ascii=False),
    )


async def test_promoted_on_interaction_same_tick(pool) -> None:
    """用例一：背景层 B 成为 dialogue.chat 对象 → 当 tick 升格 secondary 并当 tick 可响应。"""
    lod = EventDrivenLOD(pool)
    tick = 42
    seq = await _insert_chat(pool, tick=tick, sim_now=T0, a="A20", b="A21")
    await pool.execute("UPDATE agents SET cognition_tier='star' WHERE id='A20'")
    seqs = await lod.on_event(tick=tick, sim_now=T0, event={
        "seq": seq, "type": "dialogue.chat", "source": "agent:A20", "visibility": "public",
        "payload": {"participants": ["A20", "A21"], "witnesses": []},
    })
    assert len(seqs) == 1
    row = await pool.fetchrow("SELECT cognition_tier FROM agents WHERE id='A21'")
    assert row["cognition_tier"] == "secondary", "当 tick 升格"
    ev = await pool.fetchrow(
        "SELECT payload FROM events WHERE seq=$1", seqs[0],
    )
    payload = json.loads(ev["payload"]) if isinstance(ev["payload"], str) else ev["payload"]
    assert payload["from_tier"] == "background" and payload["to_tier"] == "secondary"
    assert payload["reason"] == "event_driven" and payload["caused_by"] == str(seq)
    int(payload["caused_by"])  # 裸 seq 可 ::bigint 转换（验收 3 形态）
    # 当 tick 响应：升格后 B 立即可被排程（next_due NULL → 到期），其响应事件同 tick 落库
    resp = await _insert_chat(pool, tick=tick, sim_now=T0, a="A21", b="A20")
    tick_of = await pool.fetchval("SELECT tick FROM events WHERE seq=$1", resp)
    assert tick_of == tick, "B 的响应事件同 tick 产出（04 §4.2 路径二联动）"


async def test_cooldown_demote_after_two_quiet_days(pool) -> None:
    """用例二：事件驱动升入 secondary 连续 2 模拟日零新交互 → agent.demoted(reason='cooldown')。"""
    lod = EventDrivenLOD(pool)
    seq = await _insert_chat(pool, tick=1, sim_now=T0, a="A20", b="A21")
    await lod.on_event(tick=1, sim_now=T0, event={
        "seq": seq, "type": "dialogue.chat", "source": "agent:A20", "visibility": "public",
        "payload": {"participants": ["A20", "A21"]},
    })
    assert await pool.fetchval("SELECT cognition_tier FROM agents WHERE id='A21'") == "secondary"
    # 2 模拟日内：不回落；满 2 模拟日零交互：回落
    later = T0 + dt.timedelta(days=1, hours=1)
    assert await lod.demote_inactive(tick=2, sim_now=later) == []
    after2 = T0 + dt.timedelta(days=2, seconds=1)
    seqs = await lod.demote_inactive(tick=3, sim_now=after2)
    assert len(seqs) == 1
    assert await pool.fetchval("SELECT cognition_tier FROM agents WHERE id='A21'") == "background"
    payload = json.loads(await pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert payload["reason"] == "cooldown" and payload["to_tier"] == "background"


async def test_lru_evict_cap16(pool) -> None:
    """验收 2 指定用例：17 人 secondary，最久无交互者当 tick 被挤出，落 agent.demoted(reason='lru_evict')。"""
    lod = EventDrivenLOD(pool)
    members = IDS[:17]  # A20~A36 共 17 人
    await pool.execute("UPDATE agents SET cognition_tier='secondary' WHERE id = ANY($1)", members)
    # LRU 键：A20 最久（2026-10-10），A21 次新（T0-1h），其余从未出现于事件流（NULL = 最久档，
    # 按 id 升序挤出）——为断言确定，先给 A22~A36 各写一条近期事件使 NULL 档只剩 A22
    await _insert_chat(pool, tick=1, sim_now=T0 - dt.timedelta(days=2), a="A20", b="A37")
    await _insert_chat(pool, tick=2, sim_now=T0 - dt.timedelta(hours=1), a="A21", b="A37")
    for aid in IDS[2:17]:
        await _insert_chat(pool, tick=3, sim_now=T0, a=aid, b="A37")
    seqs = await lod.enforce_cap(tick=4, sim_now=T0, caused_by="1")
    assert len(seqs) == 1, "恰挤出 1 人回 16"
    evicted = await pool.fetchval(
        "SELECT id FROM agents WHERE cognition_tier='background' AND id = ANY($1)", members,
    )
    assert evicted == "A20", "最久无交互者被挤出"
    payload = json.loads(await pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert payload["reason"] == "lru_evict" and payload["to_tier"] == "background"
    assert payload["caused_by"] == "1" and payload["caused_by"].isdigit()
    assert await pool.fetchval("SELECT count(*) FROM agents WHERE cognition_tier='secondary'") == 16


async def test_extract_targets_and_nominate() -> None:
    """目标提取口径（工程默认）+ 编剧点名直达 star（路径二③接口）。"""
    assert extract_targets({"seq": 1, "type": "social.invite", "source": "agent:A01",
                            "payload": {"from": "A01", "to": "A02"}}) == ["A02"]
    assert extract_targets({"seq": 1, "type": "dialogue.chat", "source": "agent:A01", "visibility": "public",
                            "payload": {"participants": ["A01", "A02"], "witnesses": ["A03"]}}) == ["A02", "A03"]
    assert extract_targets({"seq": 1, "type": "agent.move", "source": "agent:A01",
                            "payload": {"from": "x", "to": "apt.lobby"}}) == []
