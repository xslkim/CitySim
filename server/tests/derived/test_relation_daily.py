"""T-SYN-08 refresh_relation_daily 验收（05 §3.4 递推口径）。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.ingest.derived import refresh_relation_daily

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_derived_reldaily_test"
D0, D1, D2 = dt.date(2026, 10, 12), dt.date(2026, 10, 13), dt.date(2026, 10, 14)


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def _ins_snapshot(pool: Any, day: dt.date, relations: list[dict[str, Any]]) -> None:
    await pool.execute(
        "INSERT INTO world_state_snapshot (sim_day, state, digest) VALUES ($1,$2::jsonb,'sha256:x')",
        day, json.dumps({"sim": {"day": day.isoformat()}, "relations": relations,
                         "agents": [], "health": {"cost_daily_micro_cny": None}},
                        ensure_ascii=False))


async def _ins_change(pool: Any, day: dt.date, seq: int, a: str, b: str, da: int, dtv: int,
                      labels_added: list[str] | None = None) -> None:
    await pool.execute(
        """
        INSERT INTO relation_change_log (event_seq, a_id, b_id, delta_affinity, delta_tension,
                                         labels_added, sim_time, sim_day)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8)
        """, seq, a, b, da, dtv,
        json.dumps(labels_added) if labels_added else None,
        dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.timezone(dt.timedelta(hours=8))), day)


async def _matrix(pool: Any, day: dt.date) -> dict[tuple[str, str], tuple[int, int, list[str]]]:
    return {(r["a_id"], r["b_id"]): (int(r["affinity"]), int(r["tension"]), list(r["labels"]))
            for r in await pool.fetch(
                "SELECT a_id, b_id, affinity, tension, labels FROM relation_daily WHERE sim_day=$1",
                day)}


async def test_recurrence(pool) -> None:
    """验收 1：3 日数据集（基线矩阵 + 每日流水）逐日矩阵与手算一致（含同边多次求和、首日基线）。"""
    await _ins_snapshot(pool, D0, [
        {"a": "A01", "b": "A02", "affinity": 10, "tension": 5, "labels": ["同事"]},
        {"a": "A02", "b": "A01", "affinity": 8, "tension": 0, "labels": []}])
    n = await refresh_relation_daily.refresh(pool, D0)
    assert n == 2
    m0 = await _matrix(pool, D0)
    assert m0[("A01", "A02")] == (10, 5, ["同事"])  # 首日 = 基线

    await _ins_change(pool, D1, 101, "A01", "A02", 3, 2)
    await _ins_change(pool, D1, 102, "A01", "A02", 2, 1)   # 同边多次求和
    await _ins_change(pool, D1, 103, "A03", "A01", 1, 0)   # 新边（基线无 → 0 起算）
    await _ins_snapshot(pool, D1, [
        {"a": "A01", "b": "A02", "affinity": 15, "tension": 8, "labels": ["同事", "饭搭子"]},
        {"a": "A02", "b": "A01", "affinity": 8, "tension": 0, "labels": []},
        {"a": "A03", "b": "A01", "affinity": 1, "tension": 0, "labels": ["新识"]}])
    await refresh_relation_daily.refresh(pool, D1)
    m1 = await _matrix(pool, D1)
    assert m1[("A01", "A02")] == (10 + 5, 5 + 3, ["同事", "饭搭子"])  # 递推 + 快照 labels
    assert m1[("A02", "A01")] == (8, 0, [])
    assert m1[("A03", "A01")] == (1, 0, ["新识"])

    await _ins_change(pool, D2, 104, "A02", "A01", -4, 6)
    await _ins_snapshot(pool, D2, [
        {"a": "A01", "b": "A02", "affinity": 15, "tension": 8, "labels": ["同事"]},
        {"a": "A02", "b": "A01", "affinity": 4, "tension": 6, "labels": ["冷战"]}])
    await refresh_relation_daily.refresh(pool, D2)
    m2 = await _matrix(pool, D2)
    assert m2[("A01", "A02")] == (15, 8, ["同事"])   # 无流水 → 沿用前日值
    assert m2[("A02", "A01")] == (8 - 4, 0 + 6, ["冷战"])
    assert ("A03", "A01") in m2                      # 前日边延续


async def test_labels_from_snapshot_not_accumulated(pool) -> None:
    """验收 2：labels 不递推——定稿日 labels == 当日快照矩阵 labels；流水 labels_added 不参与。"""
    await _ins_snapshot(pool, D0, [
        {"a": "A01", "b": "A02", "affinity": 10, "tension": 5, "labels": ["同事"]}])
    await refresh_relation_daily.refresh(pool, D0)
    await _ins_change(pool, D1, 101, "A01", "A02", 3, 0, labels_added=["流水标签"])
    await _ins_snapshot(pool, D1, [
        {"a": "A01", "b": "A02", "affinity": 13, "tension": 5, "labels": ["快照标签"]}])
    await refresh_relation_daily.refresh(pool, D1)
    m1 = await _matrix(pool, D1)
    assert m1[("A01", "A02")][2] == ["快照标签"]  # 快照权威；流水 labels 仅留痕


async def test_delete_insert_idempotent(pool) -> None:
    """验收 3：同一 sim_day 连跑两次结果逐行一致（DELETE+INSERT 单事务幂等）。"""
    await _ins_snapshot(pool, D0, [
        {"a": "A01", "b": "A02", "affinity": 10, "tension": 5, "labels": []}])
    await _ins_change(pool, D0, 101, "A01", "A02", 3, 1)
    await refresh_relation_daily.refresh(pool, D0)
    first = await _matrix(pool, D0)
    await refresh_relation_daily.refresh(pool, D0)
    assert await _matrix(pool, D0) == first
    assert first[("A01", "A02")] == (13, 6, [])
