"""R1 迭代包 #1：当日滚动 latest 通道验收（ingest 落库 + obs-api 优先读取）。

- POST /v1/state:latest：随帧 digest（canonical 唯一实现）复核 + 单行恒等 upsert；
  digest 不符拒收（400 reason=digest），不推进 sync_state.snap_upto、不排派生任务。
- /api/snapshot：obs.world_state_latest 有行 → 优先（kind=rolling + snapshot_time），
  无行 → 回退日界 world_state_snapshot（kind=day_end）。
"""

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

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.ingest import create_app as create_ingest_app
from worldsim.observe.app import create_app as create_observe_app
from worldsim.observe.auth import TokenStore
from worldsim.snapshot.canonical import canonical, sha256_hex

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_state_latest_test"
TOKEN = "latest-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
T0 = dt.datetime(2026, 10, 21, 12, 30, tzinfo=LOCAL_TZ)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _state(hhmm: str) -> dict[str, Any]:
    return {"sim": {"day": T0.date().isoformat(),
                    "sim_time": f"{T0.date().isoformat()}T{hhmm}:00+08:00",
                    "compression_ratio": 3.0},
            "agents": [], "relations": [], "economy": {"stocks": []},
            "health": {"cost_daily_micro_cny": 0}, "announcements": []}


@pytest_asyncio.fixture
async def stack() -> Any:
    dsn = _build_db(DB_NAME, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql",
                    DDL_DIR / "replica_world_state_latest.sql",
                    DDL_DIR / "replica_obs_views.sql")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)

    async def _serve(app: Any) -> Any:
        port = _free_port()
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        task = asyncio.create_task(srv.serve())
        for _ in range(100):
            if srv.started:
                break
            await asyncio.sleep(0.05)
        return srv, task, port

    ingest_app = create_ingest_app(pool=pool,
                                   token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
    i_srv, i_task, i_port = await _serve(ingest_app)
    import tempfile
    token_db = tempfile.mkstemp()[1]
    o_token = TokenStore(token_db).issue("latest")
    obs_app = create_observe_app(pool=pool, token_db_path=token_db)
    o_srv, o_task, o_port = await _serve(obs_app)
    try:
        yield {"pool": pool, "ingest": f"http://127.0.0.1:{i_port}",
               "obs": f"http://127.0.0.1:{o_port}", "obs_token": o_token}
    finally:
        for srv, task in ((i_srv, i_task), (o_srv, o_task)):
            srv.should_exit = True
            await task
        await pool.close()
        _drop_db(DB_NAME)


async def _push_latest(http: str, tick: int, state: dict[str, Any], digest: str | None = None
                       ) -> httpx.Response:
    body = {"tick": tick, "sim_time": state["sim"]["sim_time"],
            "digest": digest or "sha256:" + sha256_hex(canonical(state)), "state": state}
    async with httpx.AsyncClient(base_url=http) as c:
        return await c.post("/v1/state:latest", json=body, headers=AUTH)


async def test_state_latest_upsert_and_digest_recheck(stack) -> None:
    """验收：digest 一致 → 单行 upsert（第二次覆盖）；digest 不符 → 400 拒收且行不被污染。"""
    pool, http = stack["pool"], stack["ingest"]
    r1 = await _push_latest(http, 100, _state("12:30"))
    assert r1.status_code == 200 and r1.json() == {"latest_acked": True}
    row = await pool.fetchrow("SELECT tick, digest FROM world_state_latest WHERE id = 1")
    assert row["tick"] == 100

    good = _state("12:35")
    r2 = await _push_latest(http, 101, good)
    assert r2.status_code == 200
    assert (await pool.fetchval("SELECT tick FROM world_state_latest WHERE id = 1")) == 101

    r3 = await _push_latest(http, 102, _state("12:40"), digest="sha256:bad")
    assert r3.status_code == 400 and r3.json()["reason"] == "digest"
    row2 = await pool.fetchrow("SELECT tick, digest FROM world_state_latest WHERE id = 1")
    assert row2["tick"] == 101  # 拒收不污染
    assert row2["digest"] == "sha256:" + sha256_hex(canonical(good))


async def test_snapshot_llm_status_r1_2(stack) -> None:
    """R1 #2 验收：近窗 system.llm.failover 事件 → llm_status（star chain_end = degraded 观众信号）。"""
    pool, obs, token = stack["pool"], stack["obs"], stack["obs_token"]
    await pool.execute(
        "INSERT INTO world_state_snapshot (sim_day, state, digest) VALUES ($1,$2::jsonb,'sha256:t')",
        T0.date(), json.dumps(_state("12:30"), ensure_ascii=False))
    ref = T0 + dt.timedelta(hours=5)
    async def _failover(seq: int, to_provider: str, task_type: str = "star_decision") -> None:
        await pool.execute(
            """
            INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, actors,
                                payload, visibility)
            VALUES ($1,$1,$2,$2,'system.llm.failover','system','system','{}',$3::jsonb,'internal')
            """,
            seq, ref, json.dumps({"task_type": task_type, "from_provider": "zhipu",
                                  "to_provider": to_provider, "reason": "consecutive_429_5xx"},
                                 ensure_ascii=False))
    await _failover(1, "chain_end")
    await _failover(2, "zhipu", task_type="dialogue")  # 他类型恢复事件不解除 star 判定
    await pool.execute(
        "INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, actors,"
        " payload, visibility) VALUES (3,3,$1,$1,'agent.move','agent:A01','autonomous','{A01}',"
        " '{\"from\":\"a\",\"to\":\"b\"}'::jsonb,'internal')", ref)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=obs) as c:
        r = await c.get("/api/snapshot", headers=headers)
    status = r.json()["data"]["llm_status"]
    assert status["degraded"] is True
    assert status["failover_count"] == 2
    assert status["last_reason"] == "consecutive_429_5xx"


async def test_snapshot_api_prefers_rolling_then_falls_back(stack) -> None:
    """验收：latest 有行 → kind=rolling + snapshot_time；删行 → 回退日界 kind=day_end。"""
    pool, obs, token = stack["pool"], stack["obs"], stack["obs_token"]
    day_state = _state("06:50")
    await pool.execute(
        "INSERT INTO world_state_snapshot (sim_day, state, digest) VALUES ($1,$2::jsonb,'sha256:t')",
        T0.date() - dt.timedelta(days=1), json.dumps(day_state, ensure_ascii=False))

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=obs) as c:
        r0 = await c.get("/api/snapshot", headers=headers)
        assert r0.json()["data"]["snapshot_kind"] == "day_end"

        await _push_latest(stack["ingest"], 200, _state("12:45"))
        r1 = await c.get("/api/snapshot", headers=headers)
        d1 = r1.json()["data"]
        assert d1["snapshot_kind"] == "rolling"
        # timestamptz 回读为 UTC ISO（J 项时区口径待统一，本轮不动）——两种写法都接受
        assert d1["snapshot_time"].startswith(T0.date().isoformat())
        assert ("12:45" in d1["snapshot_time"]) or ("04:45" in d1["snapshot_time"])
        assert d1["tick"] == 200  # 滚动行自带 tick（不再从日界事件反推）

        await pool.execute("DELETE FROM world_state_latest")
        r2 = await c.get("/api/snapshot", headers=headers)
        assert r2.json()["data"]["snapshot_kind"] == "day_end"
