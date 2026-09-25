"""T-SYN-03 ws_client 验收（04 §1.3：攒批 200/2s、ACK 三位点、5s 超时 down、退避 1→60s±20%、
内核不阻塞、帧 schema 逐字段）。fake WS server（websockets.serve）。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
import socket
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
import websockets

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.sync.outbox import read_sync_state
from worldsim.sync.ws_client import SyncClient
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_sync_ws_client_test"
BASE = dt.datetime(2026, 10, 12, 9, 0, tzinfo=LOCAL_TZ)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeServer:
    """摄入 fake：hello→hello_ack(max_seq 可配)；batch/mem_batch/snapshot → 脚本化回复。"""

    def __init__(self, *, max_seq: int = 0) -> None:
        self.max_seq = max_seq
        self.frames: list[dict[str, Any]] = []
        self.reply_script: list[dict[str, Any] | None] = []  # None = 默认 ack
        self.hang = False

    def _default_ack(self, frame: dict[str, Any]) -> dict[str, Any]:
        if frame["frame"] == "batch":
            return {"frame": "ack", "upto": frame["to"], "mem_upto": None, "snap_upto": None}
        if frame["frame"] == "mem_batch":
            return {"frame": "ack", "upto": None, "mem_upto": frame["to"], "snap_upto": None}
        if frame["frame"] == "snapshot":
            return {"frame": "ack", "upto": None, "mem_upto": None,
                    "snap_upto": frame["sim_day"]}
        return {"frame": "nack", "from": 0, "reason": "unknown"}

    async def handler(self, ws: Any) -> None:
        async for raw in ws:
            frame = json.loads(raw)
            self.frames.append(frame)
            if frame["frame"] == "hello":
                await ws.send(json.dumps({"frame": "hello_ack", "max_seq": self.max_seq,
                                          "mem_max": 0, "snap_max": None}))
                continue
            if frame["frame"] == "ping":
                await ws.send(json.dumps({"frame": "pong"}))
                continue
            if self.hang:
                continue  # 不回复（发送超时路径）
            reply = self.reply_script.pop(0) if self.reply_script else None
            reply = reply if reply is not None else self._default_ack(frame)
            await ws.send(json.dumps(reply))


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


async def _ins_events(pool: Any, n: int, *, start_tick: int = 1) -> list[int]:
    seqs = []
    for i in range(n):
        seqs.append(await pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility)
            VALUES ($1, $2, 'agent.move', 'agent:A01', 'autonomous',
                    '{"from":"a","to":"b","sim_cost_min":1}', 'internal') RETURNING seq
            """, start_tick + i, BASE + dt.timedelta(minutes=start_tick + i)))
    return seqs


def _client(env: Any, clock: FakeClock | None = None, **kw: Any) -> SyncClient:
    return SyncClient(env["pool"], url=env["url"], token="t",
                      monotonic=(clock or FakeClock()).monotonic,
                      rng=random.Random(42), **kw)


async def test_batching_200_or_2s(env) -> None:
    """验收 1：250 事件 → 首帧 200 条；不足 200 时 2s 窗口到点即发。"""
    await _ins_events(env["pool"], 250)
    clock = FakeClock()
    client = _client(env, clock)
    await client.connect()
    n1 = await client.sync_once()
    assert n1["events"] == 200
    batch_frames = [f for f in env["fake"].frames if f["frame"] == "batch"]
    assert len(batch_frames) == 1 and len(batch_frames[0]["events"]) == 200
    assert batch_frames[0]["to"] - batch_frames[0]["from"] == 199
    # 余 50 条未满批：窗口未到点不发；到点即发
    n2 = await client.sync_once()
    assert n2["events"] == 0
    clock.advance(2.1)
    n3 = await client.sync_once()
    assert n3["events"] == 50
    batches = [f for f in env["fake"].frames if f["frame"] == "batch"]
    assert len(batches) == 2 and len(batches[1]["events"]) == 50


async def test_ack_updates_sync_state(env) -> None:
    """验收 2：fake ack{upto,mem_upto,snap_upto} 后 sync_state 三列同值。"""
    await _ins_events(env["pool"], 3)
    client = _client(env)
    await client.connect()
    await client._apply_ack({"frame": "ack", "upto": 3, "mem_upto": 77,
                             "snap_upto": "2026-10-12"})
    s = await read_sync_state(env["pool"])
    assert s["last_acked_seq"] == 3 and s["last_acked_memory_id"] == 77
    assert s["last_acked_snapshot_day"] == dt.date(2026, 10, 12)


async def test_send_timeout_marks_down(env) -> None:
    """验收 3：fake server 挂起 → 发送超时标记 down 并进入退避（注入超时 0.3s 加速）。"""
    await _ins_events(env["pool"], 5)
    env["fake"].hang = True
    clock = FakeClock()
    client = _client(env, clock, send_timeout_s=0.3)
    await client.connect()
    await client.sync_once()          # 首批入攒批缓冲（窗口起算）
    clock.advance(3.0)                # 窗口到点 → 触发发送
    with pytest.raises(asyncio.TimeoutError):
        await client.sync_once()
    # run_forever 异常路径：标记 down + 退避序列推进
    client.down = True
    d1 = client.next_backoff()
    assert 0.8 <= d1 <= 1.2 and client.backoff_s == 2.0


async def test_reconnect_backoff(env) -> None:
    """验收 4：重连间隔序列 ∈ 1s→60s 单调封顶且带 ±20% 抖动。"""
    client = _client(env)
    delays = [client.next_backoff() for _ in range(10)]
    bases = [min(1.0 * 2**i, 60.0) for i in range(10)]
    for d, b in zip(delays, bases):
        assert 0.8 * b <= d <= 1.2 * b
    assert delays[-1] <= 72.0 and client.backoff_s == 60.0


async def test_kernel_unblocked_when_down(env) -> None:
    """验收 5：通道 down 时事件写主库无等待（写路径不 await sync 发送）。"""
    port = _free_port()  # 未监听端口 → 连接即拒
    client = SyncClient(env["pool"], url=f"ws://127.0.0.1:{port}/v1/ingest/ws", token="t",
                        sleep=lambda s: asyncio.sleep(0))
    task = asyncio.create_task(client.run_forever(poll_s=0.01))
    try:
        for _ in range(50):
            if client.down:
                break
            await asyncio.sleep(0.05)
        assert client.down
        started = asyncio.get_event_loop().time()
        seqs = await _ins_events(env["pool"], 50, start_tick=1000)
        elapsed = asyncio.get_event_loop().time() - started
        assert len(seqs) == 50 and elapsed < 5  # 写路径不阻塞
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_frame_schema(env) -> None:
    """验收 6：出站帧逐字段对照 04 §1.3 帧样例（hello/batch/ack 处理路径含 snap_upto 键）。"""
    await _ins_events(env["pool"], 2)
    dump = []
    clock = FakeClock()
    client = _client(env, clock)
    client.frame_dump = None
    orig_send = client._send

    async def spy(frame: dict[str, Any]) -> None:
        await orig_send(frame)
        dump.append(dict(frame))  # setdefault(schema_version) 之后拷贝

    client._send = spy  # type: ignore[method-assign]
    await client.connect()
    await client.sync_once()          # 首批入攒批缓冲
    clock.advance(3.0)                # 攒批窗口到点 → 发出 batch
    await client.sync_once()
    hello, batch = dump[0], dump[1]
    assert hello["frame"] == "hello"
    assert {"token", "from_seq", "kernel_ver"} <= set(hello)
    assert batch["frame"] == "batch" and {"from", "to", "events"} <= set(batch)
    assert batch["schema_version"] == 1
    ev = batch["events"][0]
    assert {"seq", "tick", "sim_time", "wall_time", "type", "source", "trigger", "arc_id",
            "location_id", "actors", "rng_seed", "payload", "visibility", "ui",
            "schema_version"} == set(ev)  # 05 §3.1 白名单 15 列
    # ack 处理路径 snap_upto 键存在
    await client._apply_ack({"frame": "ack", "upto": None, "mem_upto": None,
                             "snap_upto": "2026-10-12"})
    s = await read_sync_state(env["pool"])
    assert s["last_acked_snapshot_day"] == dt.date(2026, 10, 12)
