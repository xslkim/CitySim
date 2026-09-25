"""T-SYN-07 project_relation_change 验收（05 §3.3 展开口径 + §4.2 幂等 + §4.1 SKIP LOCKED 并发）。"""

from __future__ import annotations

import asyncio
import datetime as dt

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.derived._helpers import BASE, ins_event, ins_task
from worldsim.ingest.derived import project_relation_change, worker

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_derived_relchg_test"

CHANGES = [
    {"a_id": "A01", "b_id": "A02", "delta_affinity": 3, "delta_tension": 0},
    {"a_id": "A02", "b_id": "A01", "delta_affinity": 3, "delta_tension": 1,
     "labels_added": ["同事"], "labels_removed": ["陌生"]},
    {"a_id": "A01", "b_id": "A03", "delta_affinity": -2, "delta_tension": 5, "cause": "argue"},
]


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def test_expand_changes(pool) -> None:
    """验收 1：1 条 relation.changed 含 3 条 changes（其一含 labels）→ 3 行落表，labels 与源一致。"""
    await ins_event(pool, 100, "relation.changed", {"changes": CHANGES})
    n = await project_relation_change.project(pool, 100)
    assert n == 3
    rows = await pool.fetch(
        "SELECT a_id, b_id, delta_affinity, delta_tension, labels_added, labels_removed, sim_day"
        " FROM relation_change_log WHERE event_seq=100 ORDER BY a_id, b_id")
    assert len(rows) == 3
    labeled = next(r for r in rows if r["a_id"] == "A02")
    import json
    assert json.loads(labeled["labels_added"]) == ["同事"]
    assert json.loads(labeled["labels_removed"]) == ["陌生"]
    assert all(r["sim_day"] == BASE.date() for r in rows)


async def test_idempotent(pool) -> None:
    """验收 2：同事件重投影两次行数不变（UNIQUE(event_seq,a_id,b_id) 兜底）。"""
    await ins_event(pool, 100, "relation.changed", {"changes": CHANGES})
    await project_relation_change.project(pool, 100)
    await project_relation_change.project(pool, 100)
    assert await pool.fetchval("SELECT count(*) FROM relation_change_log") == 3


async def test_worker_consumes_and_marks_done(pool) -> None:
    """worker.run_once 消费 relchg 任务：落行 + 任务置 done；重复 dedupe_key 零冲突。"""
    await ins_event(pool, 100, "relation.changed", {"changes": CHANGES})
    await ins_task(pool, "project_relation_change", "relchg:e100")
    await ins_task(pool, "project_relation_change", "relchg:e100")  # dedupe 幂等
    done = await worker.run_once(pool)
    assert done == 1
    assert await pool.fetchval(
        "SELECT status FROM derived_task WHERE dedupe_key='relchg:e100'") == "done"
    assert await pool.fetchval("SELECT count(*) FROM relation_change_log") == 3


async def test_worker_concurrent_no_double_processing(pool) -> None:
    """验收 11：2 个 worker 并发消费同一任务表，无重复消费（行数正确、dedupe 冲突零）。"""
    for i in range(1, 21):
        await ins_event(pool, i, "relation.changed", {"changes": [
            {"a_id": "A01", "b_id": "A02", "delta_affinity": 1, "delta_tension": 0}]},
            minutes=i)
        await ins_task(pool, "project_relation_change", f"relchg:e{i}")

    async def loop() -> int:
        total = 0
        while True:
            n = await worker.run_once(pool)
            if n == 0:
                return total
            total += n

    a, b = await asyncio.gather(loop(), loop())
    assert a + b == 20
    assert await pool.fetchval("SELECT count(*) FROM relation_change_log") == 20
    assert await pool.fetchval(
        "SELECT count(*) FROM derived_task WHERE status='done'") == 20
