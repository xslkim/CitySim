"""T-SYN-07 materialize_event_grade 验收（05 §3.8：基线/最新复核胜出/events 零 UPDATE）。"""

from __future__ import annotations

import hashlib

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.derived._helpers import ins_event, ins_task
from worldsim.ingest.derived import materialize_event_grade, worker
from worldsim.snapshot.canonical import canonical

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_derived_grade_test"


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def _row_hash(pool, seq: int) -> str:
    """目标 events 行全列 canonical md5（必过项②单元级前身：行 hash 前后比对）。"""
    row = await pool.fetchrow("SELECT * FROM events WHERE seq=$1", seq)
    import json

    def _def(o):
        import datetime as dt
        if isinstance(o, (dt.datetime, dt.date)):
            return o.isoformat()
        raise TypeError

    plain = json.loads(json.dumps(dict(row), default=_def, ensure_ascii=False))
    return hashlib.md5(canonical(plain).encode()).hexdigest()


async def test_baseline_from_ui_grade(pool) -> None:
    """验收 8：ui.grade='B' 事件摄入 → 视图行 (seq,'B',NULL,NULL)。"""
    await ins_event(pool, 10, "dialogue.chat", {"participants": ["A01", "A02"], "lines": [],
                                                "text_display": "t"},
                    visibility="public", ui={"grade": "B"})
    await ins_task(pool, "materialize_event_grade", "grade:e10")
    assert await worker.run_once(pool) == 1
    row = await pool.fetchrow("SELECT * FROM event_grade_view WHERE seq=10")
    assert row["grade"] == "B" and row["revised_by_seq"] is None and row["reason"] is None


async def test_revise_latest_wins(pool) -> None:
    """验收 9：同 target_seq 两条 revise（seq 小者 'A'、大者 'C'）→ grade='C'、revised_by_seq 大者。"""
    await ins_event(pool, 10, "dialogue.chat", {"participants": ["A01"], "lines": [],
                                                "text_display": "t"},
                    visibility="public", ui={"grade": "B"})
    await ins_task(pool, "materialize_event_grade", "grade:e10")
    await ins_event(pool, 20, "director.grade_revise",
                    {"target_seq": "10", "new_grade": "A", "reason": "上调"}, visibility="public",
                    trigger="director")
    await ins_event(pool, 21, "director.grade_revise",
                    {"target_seq": "10", "new_grade": "C", "reason": "终审下调"},
                    visibility="public", trigger="director")
    await ins_task(pool, "materialize_event_grade", "grade:e20")
    await ins_task(pool, "materialize_event_grade", "grade:e21")
    assert await worker.run_once(pool) == 3
    row = await pool.fetchrow("SELECT grade, revised_by_seq, reason FROM event_grade_view WHERE seq=10")
    assert row["grade"] == "C" and row["revised_by_seq"] == 21 and row["reason"] == "终审下调"
    # 乱序重放旧复核（seq 小者）不得覆盖（最新复核胜出，05 §3.8）
    await materialize_event_grade.materialize(pool, 20)
    row = await pool.fetchrow("SELECT grade, revised_by_seq FROM event_grade_view WHERE seq=10")
    assert row["grade"] == "C" and row["revised_by_seq"] == 21


async def test_events_row_zero_update(pool) -> None:
    """验收 10：grade_revise 物化前后目标 events 行 md5(canonical(行全列)) 不变（零 UPDATE）。"""
    await ins_event(pool, 10, "dialogue.chat", {"participants": ["A01"], "lines": [],
                                                "text_display": "t"},
                    visibility="public", ui={"grade": "B"})
    before = await _row_hash(pool, 10)
    await ins_event(pool, 20, "director.grade_revise",
                    {"target_seq": "10", "new_grade": "A", "reason": "复核"}, visibility="public",
                    trigger="director")
    await materialize_event_grade.materialize(pool, 20)
    after = await _row_hash(pool, 10)
    assert before == after
    assert await pool.fetchval("SELECT grade FROM event_grade_view WHERE seq=10") == "A"


async def test_baseline_sweep_no_override(pool) -> None:
    """每日兜底全量重扫：不覆盖复核结果（ON CONFLICT DO NOTHING）。"""
    await ins_event(pool, 10, "dialogue.chat", {"participants": ["A01"], "lines": [],
                                                "text_display": "t"},
                    visibility="public", ui={"grade": "B"})
    await ins_event(pool, 20, "director.grade_revise",
                    {"target_seq": "10", "new_grade": "A", "reason": "r"}, visibility="public",
                    trigger="director")
    await materialize_event_grade.materialize(pool, 10)
    await materialize_event_grade.materialize(pool, 20)
    n = await materialize_event_grade.baseline_sweep(pool)
    assert n == 0  # 两行均已存在
    assert await pool.fetchval("SELECT grade FROM event_grade_view WHERE seq=10") == "A"
