"""T-SYN-02 三流读取器验收（04 §1.3 / 05 §3.1/§3.2）。"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.sync.outbox import (
    EVENT_COLUMNS,
    MEMORY_COLUMNS,
    fetch_events_after,
    fetch_memory_projections_after,
    read_sync_state,
    update_sync_state,
)
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_sync_outbox_test"
BASE = dt.datetime(2026, 10, 12, 9, 0, tzinfo=LOCAL_TZ)


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def _ins_event(pool, tick: int, payload: dict, *, visibility: str = "public",
                     type_: str = "agent.move") -> int:
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility)
        VALUES ($1, $2, $3, 'agent:A01', 'autonomous', $4::jsonb, $5) RETURNING seq
        """, tick, BASE + dt.timedelta(minutes=tick), type_,
        json.dumps(payload, ensure_ascii=False), visibility)


async def test_events_row_columns(pool) -> None:
    """验收 1：读取器返回 dict 键集 == 05 §3.1 白名单 15 列。"""
    await _ins_event(pool, 1, {"from": "home.a", "to": "corp.tech", "sim_cost_min": 5,
                               "text_raw": "不该出站", "debug": 1}, visibility="public")
    rows = await fetch_events_after(pool, 0, limit=10)
    assert len(rows) == 1
    assert set(rows[0]) == set(EVENT_COLUMNS)
    assert rows[0]["payload"] == {"from": "home.a", "to": "corp.tech", "sim_cost_min": 5}


async def test_memory_projection_shape(pool) -> None:
    """验收 2：投影行恰八列；content_display IS NULL 不出行；目击投影原样直传；无 content/embedding/archived。"""
    await pool.execute(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance,
                              source_event_seq, is_witness)
        VALUES ('A01', $1, 'event', '内部原文', '展示文本', 5, 42, TRUE),
               ('A02', $1, 'reflection', '未过审原文', NULL, 7, NULL, FALSE)
        """, BASE)
    rows = await fetch_memory_projections_after(pool, 0, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == set(MEMORY_COLUMNS)
    assert not {"content", "embedding", "archived"} & set(row)
    assert row["is_witness"] is True and row["source_event_seq"] == 42
    assert row["content_display"] == "展示文本"


async def test_resume_cursors(pool) -> None:
    """验收 3：seq 1..10 置 last_acked_seq=7 → 读出恰 8..10 升序。"""
    seqs = [await _ins_event(pool, i + 1, {"from": "a", "to": "b", "sim_cost_min": 1},
                             visibility="internal") for i in range(10)]
    assert seqs == sorted(seqs)
    rows = await fetch_events_after(pool, seqs[6], limit=100)
    assert [r["seq"] for r in rows] == seqs[7:]


async def test_blocked_row_never_sent(pool) -> None:
    """07 D3：block→internal（internal + payload 携 text_raw）整行不出站；主库可含原文，输出恒无。"""
    blocked_seq = await _ins_event(
        pool, 100, {"participants": ["A01", "A02"], "text_raw": "被 block 的原文串"},
        visibility="internal", type_="dialogue.chat")
    normal_internal = await _ins_event(
        pool, 101, {"changes": [{"agent_id": "A01", "need": "hunger", "delta": -5,
                                 "new_value": 60, "cause": "eat"}]},
        visibility="internal", type_="state.needs_delta")
    assert await pool.fetchval("SELECT payload ? 'text_raw' FROM events WHERE seq=$1",
                               blocked_seq) is True  # 主库可为 true
    rows = await fetch_events_after(pool, 0, limit=100)
    seqs = [r["seq"] for r in rows]
    assert blocked_seq not in seqs          # block 行整行不出站
    assert normal_internal in seqs          # 正常 internal 结构化出站
    assert all("text_raw" not in r["payload"] for r in rows)  # 读取器输出恒 false


async def test_sync_state_roundtrip(pool) -> None:
    """sync_state 三位点读写（04 §1.3 ACK 后 UPDATE 语义）。"""
    s = await read_sync_state(pool)
    assert s == {"last_acked_seq": 0, "last_acked_memory_id": 0, "last_acked_snapshot_day": None}
    await update_sync_state(pool, upto=100, mem_upto=50, snap_upto=dt.date(2026, 10, 12))
    s = await read_sync_state(pool)
    assert s["last_acked_seq"] == 100 and s["last_acked_memory_id"] == 50
    assert s["last_acked_snapshot_day"] == dt.date(2026, 10, 12)
    await update_sync_state(pool, upto=200)  # None = 不动该列
    s = await read_sync_state(pool)
    assert s["last_acked_seq"] == 200 and s["last_acked_memory_id"] == 50
