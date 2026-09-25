"""T-SYN-09 云端 digest 比对/重灌闭环验收（04 §9.2 / 05 §4.3 / 07 D4-D5；授权面清单制 P2-4）。"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import socket
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
import uvicorn

from tests.conftest import DDL_DIR, _build_db, _drop_db, _psql
from worldsim.ingest import create_app
from worldsim.ingest.digest import replica_day_digest
from worldsim.snapshot.canonical import canonical, sha256_hex

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_ingest_digest_test"
TOKEN = "digest-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
D = dt.date(2026, 10, 12)         # 比对日
LATE = dt.date(2026, 10, 14)      # T（= D+2，恰在窗口边）


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def server() -> Any:
    _psql("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='worldsim_ingest') THEN "
          "CREATE ROLE worldsim_ingest LOGIN; END IF; END $$")
    dsn = _build_db(DB_NAME, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql",
                    DDL_DIR / "replica_digest_log.sql")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    app = create_app(pool=pool, token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    yield {"http": f"http://127.0.0.1:{port}", "pool": pool}
    srv.should_exit = True
    await task
    await pool.close()
    _drop_db(DB_NAME)


async def _ins_event(pool: Any, seq: int, day: dt.date, hh: int = 9) -> None:
    st = dt.datetime.combine(day, dt.time(hh, 0), tzinfo=LOCAL_TZ)
    await pool.execute(
        """
        INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, actors,
                            payload, visibility)
        VALUES ($1,$1,$2,$2,'agent.move','agent:A01','autonomous','{A01}',
                '{"from":"a","to":"b","sim_cost_min":1}','internal')
        """, seq, st)


async def _ins_snapshot(pool: Any, day: dt.date, state: dict) -> str:
    digest = "sha256:" + sha256_hex(canonical(state))
    await pool.execute(
        "INSERT INTO world_state_snapshot (sim_day, state, digest) VALUES ($1,$2::jsonb,$3)",
        day, json.dumps(state, ensure_ascii=False), digest)
    return digest


async def _push(server: Any, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        r = await c.post("/v1/digest", json=body, headers=AUTH)
        assert r.status_code == 200
        return r.json()


async def test_compare_match(server) -> None:
    """验收 4：两端一致数据 → 比对 ok 落 digest_log。"""
    pool = server["pool"]
    for i in range(1, 4):
        await _ins_event(pool, i, D, hh=8 + i)
    await _ins_event(pool, 100, LATE)  # 定 T
    state = {"sim": {"day": D.isoformat()}, "relations": [], "agents": [],
             "health": {"cost_daily_micro_cny": 10}}
    await _ins_snapshot(pool, D, state)
    body = await replica_day_digest(pool, D)  # 与本机同数据 → 一致
    resp = await _push(server, body)
    assert resp == {"status": "ok", "mismatch_days": []}
    assert await pool.fetchval(
        "SELECT status FROM digest_log WHERE sim_day=$1 ORDER BY id DESC LIMIT 1", D) == "ok"


async def test_mismatch_triggers_reproject(server) -> None:
    """验收 5：删一行后推送 → mismatch → 当日 DELETE → 响应含该日 → 重发复检 ok + refresh 任务。"""
    pool = server["pool"]
    for i in range(1, 4):
        await _ins_event(pool, i, D, hh=8 + i)
    await _ins_event(pool, 100, LATE)
    good = await replica_day_digest(pool, D)
    # 云端删一行 → 推送（本机口径仍是 3 行）→ mismatch
    await pool.execute("DELETE FROM events WHERE seq=3")
    resp = await _push(server, good)
    assert resp["status"] == "mismatch" and resp["mismatch_days"] == [D.isoformat()]
    assert await pool.fetchval(
        "SELECT count(*) FROM events WHERE (sim_time AT TIME ZONE 'Asia/Shanghai')::date=$1",
        D) == 0  # 当日已删，待重灌
    tasks = {r["dedupe_key"] for r in await pool.fetch("SELECT dedupe_key FROM derived_task")}
    assert f"reproject:relation_daily:{D.isoformat()}" in tasks
    assert f"reproject:health_daily:{D.isoformat()}" in tasks
    # 本机重发（mode=reproject 豁免乱序，07 D4）→ 复检 ok
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        events = []
        for i in range(1, 4):
            st = dt.datetime.combine(D, dt.time(8 + i, 0), tzinfo=LOCAL_TZ)
            events.append({"seq": i, "tick": i, "sim_time": st.isoformat(),
                           "wall_time": st.isoformat(), "type": "agent.move",
                           "source": "agent:A01", "trigger": "autonomous", "actors": ["A01"],
                           "payload": {"from": "a", "to": "b", "sim_cost_min": 1},
                           "visibility": "internal", "schema_version": 1})
        r = await c.post("/v1/events:batch",
                         json={"from_seq": 1, "events": events, "mode": "reproject"},
                         headers=AUTH)
        assert r.status_code == 200
    resp2 = await _push(server, good)
    assert resp2 == {"status": "ok", "mismatch_days": []}
    statuses = [r["status"] for r in await pool.fetch(
        "SELECT status FROM digest_log WHERE sim_day=$1 ORDER BY id", D)]
    assert statuses == ["mismatch", "reprojected", "ok"]


async def test_snapshot_digest_recheck(server) -> None:
    """验收 6：篡改库存 state 一键 → 比对复核失败 → mismatch 重灌含快照删除（05 §3.6）。"""
    pool = server["pool"]
    await _ins_event(pool, 1, D)
    await _ins_event(pool, 100, LATE)
    state = {"sim": {"day": D.isoformat()}, "relations": [], "agents": [{"id": "A01"}],
             "health": {"cost_daily_micro_cny": None}}
    orig_digest = await _ins_snapshot(pool, D, state)
    await pool.execute(
        "UPDATE world_state_snapshot SET state = jsonb_set(state, '{health,cost_daily_micro_cny}',"
        " '999') WHERE sim_day=$1", D)  # 篡改库存
    body = await replica_day_digest(pool, D)  # 现场重算 → 已反映篡改
    body["snapshot_digest"] = orig_digest      # 本机持有的是原始 digest
    resp = await _push(server, body)
    assert resp["status"] == "mismatch"
    assert await pool.fetchval(
        "SELECT count(*) FROM world_state_snapshot WHERE sim_day=$1", D) == 0


async def test_digest_log_grant_surface_unchanged(server) -> None:
    """验收 7（P2-4）：增 digest_log 后 obs_ro 可访问对象仍恰为清单 8 项；digest_log 拒访。"""
    _psql("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='obs_ro') THEN "
          "CREATE ROLE obs_ro LOGIN; END IF; END $$")
    pool = server["pool"]
    rows = await pool.fetch(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
        " WHERE table_schema='public' AND grantee='obs_ro'")
    assert {(r["table_name"], r["privilege_type"]) for r in rows} == {
        (t, "SELECT") for t in ("events", "memory_projection", "relation_change_log",
                                "relation_daily", "health_daily", "world_state_snapshot",
                                "ripple_edge", "event_grade_view")}
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL ROLE obs_ro")
        with pytest.raises(Exception):
            await conn.fetchval("SELECT count(*) FROM digest_log")
