"""T-SYN-01 canonical/digest 消费方回归（唯一实现 = 02 T-ADJ-09 snapshot/canonical.py，R2 §A.7）。

验收 5（同值不同键序 → 同一 canonical/md5）与验收 6（digest_events 公式逐步手算复算，05 §2.2）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.snapshot.canonical import canonical_event_payload, digest_events
from worldsim.sync.whitelist import strip_payload
from worldsim.time_engine.clock import LOCAL_TZ

DB_NAME = "worldsim_sync_canonical_test"
DAY1 = dt.date(2026, 10, 12)
DAY2 = dt.date(2026, 10, 13)


def _hand_canonical(payload: dict, *, type_: str, visibility: str) -> str:
    """公式逐步手算（独立实现路径）：键白名单剥除 → 递归键序 → 无空白 UTF-8。"""
    stripped = strip_payload(payload, type_=type_, visibility=visibility)
    return json.dumps(stripped, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_key_order_irrelevant() -> None:
    """验收 5：同值不同键序/空白的 payload 产出同一 canonical 串与同一 md5。"""
    a = canonical_event_payload(
        {"to": "corp.pantry", "from": "corp.tech", "sim_cost_min": 3},
        type_="agent.move", visibility="internal")
    b = canonical_event_payload(
        {"sim_cost_min": 3, "from": "corp.tech", "to": "corp.pantry"},
        type_="agent.move", visibility="internal")
    assert a == b
    assert hashlib.md5(a.encode()).hexdigest() == hashlib.md5(b.encode()).hexdigest()
    assert a == '{"from":"corp.tech","sim_cost_min":3,"to":"corp.pantry"}'


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "schema_v1.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def _insert(pool, tick: int, sim_time: dt.datetime, payload: dict, *,
                  type_: str = "agent.move", visibility: str = "internal") -> int:
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility)
        VALUES ($1, $2, $3, 'system', 'system', $4::jsonb, $5) RETURNING seq
        """, tick, sim_time, type_, json.dumps(payload, ensure_ascii=False), visibility)


@pytest.mark.asyncio
async def test_digest_formula(pool) -> None:
    """验收 6：digest_events == 手算 COUNT/SUM(seq)/sha256(string_agg(md5 ORDER BY seq))。"""
    specs = [
        (1, dt.datetime(2026, 10, 12, 9, 0, tzinfo=LOCAL_TZ),
         {"from": "home.a", "to": "corp.tech", "sim_cost_min": 5, "text_raw": "永不入 digest"}),
        (2, dt.datetime(2026, 10, 12, 18, 0, tzinfo=LOCAL_TZ),
         {"from": "corp.tech", "to": "home.a", "sim_cost_min": 4}),
        (3, dt.datetime(2026, 10, 13, 9, 0, tzinfo=LOCAL_TZ),
         {"from": "home.a", "to": "corp.pantry", "sim_cost_min": 2}),
    ]
    seqs = [await _insert(pool, t, st, p) for t, st, p in specs]

    got = await digest_events(pool, DAY1)
    md5s = [hashlib.md5(_hand_canonical(p, type_="agent.move", visibility="internal")
                        .encode("utf-8")).hexdigest() for p in (specs[0][2], specs[1][2])]
    expected_digest = "sha256:" + hashlib.sha256("".join(md5s).encode("utf-8")).hexdigest()
    assert got["sim_day"] == DAY1.isoformat()
    assert got["count"] == 2
    assert got["sum_seq"] == seqs[0] + seqs[1]
    assert got["digest"] == expected_digest

    got2 = await digest_events(pool, DAY2)
    md5_2 = hashlib.md5(
        _hand_canonical(specs[2][2], type_="agent.move", visibility="internal").encode("utf-8")
    ).hexdigest()
    assert got2["count"] == 1
    assert got2["sum_seq"] == seqs[2]
    assert got2["digest"] == "sha256:" + hashlib.sha256(md5_2.encode("utf-8")).hexdigest()
