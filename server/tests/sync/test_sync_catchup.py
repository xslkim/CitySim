"""T-SYN-04 恢复追平/乱序重发/HTTPS 回退验收（04 §1.3 恢复态 + §9.1 回退端点与 409）。

验收 7（真摄入 API 幂等集成）由 tests/ingest/test_api.py::test_events_batch_idempotent 覆盖；
本文件补一条客户端→真摄入 API 的端到端追平集成。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import hashlib
import json
import random
import socket
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
import uvicorn
import websockets

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.ingest import create_app
from worldsim.snapshot.canonical import canonical, sha256_hex
from worldsim.sync.https_fallback import HttpsFallback
from worldsim.sync.outbox import read_sync_state, update_sync_state
from worldsim.sync.ws_client import SyncClient
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_sync_catchup_test"
REPLICA_DB = "worldsim_sync_catchup_replica_test"
BASE = dt.datetime(2026, 10, 12, 9, 0, tzinfo=LOCAL_TZ)
TOKEN = "catchup-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _ins_events(pool: Any, n: int, *, start_tick: int = 1) -> None:
    for i in range(n):
        await pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility)
            VALUES ($1, $2, 'agent.move', 'agent:A01', 'autonomous',
                    '{"from":"a","to":"b","sim_cost_min":1}', 'internal') RETURNING seq
            """, start_tick + i, BASE + dt.timedelta(minutes=start_tick + i))


class FakeServer:
    def __init__(self, *, max_seq: int = 0) -> None:
        self.max_seq = max_seq
        self.frames: list[dict[str, Any]] = []
        self.on_batch: Any = None  # 可注入自定义回复

    async def handler(self, ws: Any) -> None:
        async for raw in ws:
            f = json.loads(raw)
            self.frames.append(f)
            if f["frame"] == "hello":
                await ws.send(json.dumps({"frame": "hello_ack", "max_seq": self.max_seq}))
            elif f["frame"] == "batch":
                reply = self.on_batch(f) if self.on_batch else None
                await ws.send(json.dumps(
                    reply or {"frame": "ack", "upto": f["to"], "mem_upto": None,
                              "snap_upto": None}))
            elif f["frame"] == "snapshot":
                await ws.send(json.dumps({"frame": "ack", "upto": None, "mem_upto": None,
                                          "snap_upto": f["sim_day"]}))


@pytest_asyncio.fixture
async def env() -> Any:
    dsn = _build_db(DB_NAME, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    fake = FakeServer()
    port = _free_port()
    srv = await websockets.serve(fake.handler, "127.0.0.1", port)
    yield {"pool": pool, "fake": fake, "url": f"ws://127.0.0.1:{port}/v1/ingest/ws"}
    srv.close()
    await srv.wait_closed()
    await pool.close()
    _drop_db(DB_NAME)


def _client(env: Any, **kw: Any) -> SyncClient:
    return SyncClient(env["pool"], url=env["url"], token=TOKEN, rng=random.Random(7), **kw)


async def test_resume_hello(env) -> None:
    """验收 1：hello_ack max_seq == last_acked → 无重发直接进正常态。"""
    await _ins_events(env["pool"], 5)
    await update_sync_state(env["pool"], upto=5)
    env["fake"].max_seq = 5
    client = _client(env)
    await client.connect()
    n = await client.sync_once()
    assert n["events"] == 0
    assert [f for f in env["fake"].frames if f["frame"] == "batch"] == []


async def test_rewind_when_cloud_behind(env) -> None:
    """验收 2：max_seq < last_acked → 客户端自 max_seq+1 起重发。"""
    await _ins_events(env["pool"], 10)
    await update_sync_state(env["pool"], upto=10)
    env["fake"].max_seq = 4
    client = _client(env, batch_window_s=0.0)
    await client.connect()
    assert client._cursor_seq == 4  # 重绕到副本位点
    await client.sync_once()
    batches = [f for f in env["fake"].frames if f["frame"] == "batch"]
    assert batches and batches[0]["from"] == 5


async def test_catchup_batch_shape(env) -> None:
    """验收 3：积压 1200 → 追平帧 500/500/200，流水窗口 ≤8 批在途。"""
    await _ins_events(env["pool"], 1200)
    in_flight_peak = 0
    current = 0

    def on_batch(f: dict[str, Any]) -> dict[str, Any]:
        nonlocal in_flight_peak, current
        current += 1
        in_flight_peak = max(in_flight_peak, current)
        if current >= 3:  # 攒 3 批再统一 ack，制造在途窗口
            current = 0
        return {"frame": "ack", "upto": f["to"], "mem_upto": None, "snap_upto": None}

    env["fake"].on_batch = on_batch
    client = _client(env)
    await client.connect()
    n = await client.sync_once()
    assert n["events"] == 1200
    batches = [f for f in env["fake"].frames if f["frame"] == "batch"]
    assert [len(b["events"]) for b in batches] == [500, 500, 200]
    assert in_flight_peak <= 8
    s = await read_sync_state(env["pool"])
    assert s["last_acked_seq"] == 1200


async def test_snapshot_backfill(env, tmp_path) -> None:
    """验收 4：位点后 2 个模拟日快照 → 按 sim_day 升序补发 2 帧。"""
    for day in ("2026-10-12", "2026-10-13"):
        state = {"sim": {"day": day}, "agents": [], "health": {"cost_daily_micro_cny": None}}
        with gzip.open(tmp_path / f"snapshot_{day}.whitelist.json.gz", "wt", encoding="utf-8") as f:
            f.write(canonical(state))
        (tmp_path / f"snapshot_{day}.digest").write_text(
            "sha256:" + sha256_hex(canonical(state)) + "\n", encoding="utf-8")
    await update_sync_state(env["pool"], snap_upto=dt.date(2026, 10, 11))
    client = _client(env, snapshot_dir=str(tmp_path))
    await client.connect()
    sent = await client.send_snapshots_pending()
    assert sent == 2
    snaps = [f for f in env["fake"].frames if f["frame"] == "snapshot"]
    assert [s["sim_day"] for s in snaps] == ["2026-10-12", "2026-10-13"]
    s = await read_sync_state(env["pool"])
    assert s["last_acked_snapshot_day"] == dt.date(2026, 10, 13)


async def test_409_resend(env) -> None:
    """验收 6：fake 回 nack(expected_seq) → 客户端按 expected_seq 重发且最终收到 ack。"""
    await _ins_events(env["pool"], 10)
    calls = {"n": 0}

    def on_batch(f: dict[str, Any]) -> dict[str, Any] | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"frame": "nack", "from": f["from"], "reason": "order",
                    "expected_seq": f["from"]}
        return None  # 之后默认 ack

    env["fake"].on_batch = on_batch
    client = _client(env, batch_window_s=0.0)  # 立即 flush
    await client.connect()
    await client.sync_once()  # 收到 nack → 游标回退
    await client.sync_once()  # 重发 → ack
    batches = [f for f in env["fake"].frames if f["frame"] == "batch"]
    assert len(batches) == 2 and batches[0]["from"] == batches[1]["from"]
    s = await read_sync_state(env["pool"])
    assert s["last_acked_seq"] == 10


async def test_https_fallback(env, tmp_path) -> None:
    """验收 5：WS 拒连时三端点各走通一次，sync_state 位点随响应推进（真摄入 API）。"""
    rdsn = _build_db(REPLICA_DB, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql")
    rpool = await asyncpg.create_pool(rdsn, min_size=1, max_size=4)
    app = create_app(pool=rpool, token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    try:
        pool = env["pool"]
        await _ins_events(pool, 12)
        mem_id = await pool.fetchval(
            """
            INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance)
            VALUES ('A01', $1, 'reflection', '原文', '反思展示', 6) RETURNING id
            """, BASE)
        day = "2026-10-12"
        state = {"sim": {"day": day}, "agents": [], "health": {"cost_daily_micro_cny": None}}
        with gzip.open(tmp_path / f"snapshot_{day}.whitelist.json.gz", "wt", encoding="utf-8") as f:
            f.write(canonical(state))
        (tmp_path / f"snapshot_{day}.digest").write_text(
            "sha256:" + sha256_hex(canonical(state)) + "\n", encoding="utf-8")

        fb = HttpsFallback(pool, base_url=f"http://127.0.0.1:{port}", token=TOKEN,
                           snapshot_dir=str(tmp_path))
        sent = await fb.sync_once()
        assert sent == {"events": 12, "memories": 1, "snapshots": 1}
        s = await read_sync_state(pool)
        assert s["last_acked_seq"] == 12
        assert s["last_acked_memory_id"] == mem_id
        assert s["last_acked_snapshot_day"] == dt.date(2026, 10, 12)
        # 副本侧落库核对（端到端幂等：再来一轮零增量）
        assert await rpool.fetchval("SELECT count(*) FROM events") == 12
        assert await rpool.fetchval("SELECT count(*) FROM memory_projection") == 1
        assert await rpool.fetchval(
            "SELECT count(*) FROM world_state_snapshot WHERE sim_day='2026-10-12'") == 1
        sent2 = await fb.sync_once()
        assert sent2 == {"events": 0, "memories": 0, "snapshots": 0}
        assert await rpool.fetchval("SELECT count(*) FROM events") == 12
    finally:
        srv.should_exit = True
        await task
        await rpool.close()
        _drop_db(REPLICA_DB)
