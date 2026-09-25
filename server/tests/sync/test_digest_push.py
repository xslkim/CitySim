"""T-SYN-09 本机 digest 推送验收（04 §9.1 body 契约 / 05 §2.2 口径与 T−2 窗口）。"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.snapshot.canonical import canonical, sha256_hex
from worldsim.sync.digest import build_push_body, digest_memories, in_compare_window
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_sync_digest_push_test"
DAY = dt.date(2026, 10, 12)


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def _ins_event(pool, tick: int, sim_time: dt.datetime) -> int:
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility)
        VALUES ($1, $2, 'agent.move', 'agent:A01', 'autonomous',
                '{"from":"a","to":"b","sim_cost_min":1}', 'internal') RETURNING seq
        """, tick, sim_time)


async def test_push_body_shape(pool, tmp_path) -> None:
    """验收 1：推送 body 逐键对齐 04 §9.1 契约。"""
    t0 = dt.datetime(2026, 10, 12, 9, 0, tzinfo=LOCAL_TZ)
    await _ins_event(pool, 1, t0)
    await _ins_event(pool, 2, t0 + dt.timedelta(hours=1))
    await pool.execute(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance)
        VALUES ('A01', $1, 'event', '原文', '展示', 5)
        """, t0)
    state = {"sim": {"day": DAY.isoformat()}, "agents": [], "relations": [],
             "health": {"cost_daily_micro_cny": None}}
    (tmp_path / f"snapshot_{DAY.isoformat()}.digest").write_text(
        "sha256:" + sha256_hex(canonical(state)) + "\n", encoding="utf-8")
    body = await build_push_body(pool, DAY, out_dir=tmp_path)
    assert set(body) == {"sim_day", "events", "memories", "snapshot_digest"}
    assert set(body["events"]) == {"count", "sum_seq", "digest"}
    assert set(body["memories"]) == {"count", "sum_id"}
    assert body["events"]["count"] == 2
    assert body["events"]["digest"].startswith("sha256:")
    assert body["memories"] == {"count": 1, "sum_id": 1}
    assert body["snapshot_digest"] == "sha256:" + sha256_hex(canonical(state))


async def test_memory_digest_dispatched_only(pool) -> None:
    """验收 2：content_display IS NULL 行两侧均不计入 COUNT/SUM（05 §2.2 已出行口径）。"""
    t0 = dt.datetime(2026, 10, 12, 9, 0, tzinfo=LOCAL_TZ)
    await pool.execute(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance)
        VALUES ('A01', $1, 'event', '原文1', '已过审', 5),
               ('A02', $1, 'reflection', '原文2', NULL, 7)   -- 未过审：不计
        """, t0)
    m = await digest_memories(pool, DAY)
    assert m == {"count": 1, "sum_id": 1}
    other = await digest_memories(pool, DAY + dt.timedelta(days=1))
    assert other == {"count": 0, "sum_id": 0}


def test_window_t_minus_2() -> None:
    """验收 3：当前模拟日=T，T−1/T 不参与比对（不一致也不报警）。"""
    today = dt.date(2026, 10, 15)
    assert in_compare_window(dt.date(2026, 10, 13), today) is True   # T−2
    assert in_compare_window(dt.date(2026, 10, 14), today) is False  # T−1
    assert in_compare_window(today, today) is False                  # T
    assert in_compare_window(dt.date(2026, 10, 1), today) is True
