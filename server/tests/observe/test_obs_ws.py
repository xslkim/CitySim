"""T-WEB-07 WS 通道测试（03 §5.2 全帧形态：hello/welcome/subscribe/event/state_diff/health/
resume/resync_required/ping-pong/90s 心跳断开；00 §4 红线 2 seq 续传不重不漏）。

真栈实测：进程内 uvicorn（随机空端口）+ websockets 客户端；节拍参数经 app.state 覆盖加速。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket
import tempfile
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
import uvicorn
import websockets

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.observe.app import create_app
from worldsim.observe.auth import TokenStore

DB_NAME = "worldsim_obs_ws_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
BASE = dt.datetime.now(LOCAL_TZ).replace(hour=11, minute=0, second=0, microsecond=0) + dt.timedelta(days=10)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def server() -> Any:
    dsn = _build_db(
        DB_NAME,
        DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql",
        DDL_DIR / "obs_views_v1.sql", DDL_DIR / "obs_derived_v1.sql",
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    token_db = str(Path(tempfile.mkdtemp()) / "tokens.db")
    app = create_app(pool=pool, token_db_path=token_db)
    app.state.ws_poll_interval = 0.05
    app.state.ws_health_interval = 0.2
    app.state.ws_heartbeat_timeout = 90.0
    token = TokenStore(token_db).issue("tester")
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    yield {"url": f"ws://127.0.0.1:{port}/ws", "token": token, "pool": pool, "app": app,
           "token_db": token_db}
    srv.should_exit = True
    await task
    await pool.close()
    _drop_db(DB_NAME)


async def _insert(pool: Any, tick: int, etype: str, vis: str, payload: dict,
                  *, actors: list[str] | None = None, minutes: int = 0) -> int:
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, payload, visibility)
        VALUES ($1, $2, $3, 'system', 'system', $4, $5::jsonb, $6) RETURNING seq
        """,
        tick, BASE + dt.timedelta(minutes=minutes), etype, actors or [],
        json.dumps(payload, ensure_ascii=False), vis,
    )


async def _recv_until(ws: Any, pred: Any, timeout: float = 5.0) -> list[dict]:
    """收集帧直到 pred 命中或超时；返回全部已收帧。"""
    frames = []
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, deadline - asyncio.get_event_loop().time()))
        except TimeoutError:
            break
        frame = json.loads(raw)
        frames.append(frame)
        if pred(frame):
            break
    return frames


@pytest.mark.asyncio
async def test_resume_no_dup_no_gap(server: Any) -> None:
    """断线 resume-from-seq：hello.last_seq 补推缺口段，不重不漏接 live（03 §5.2）。"""
    pool, token, url = server["pool"], server["token"], server["url"]
    seqs = []
    for i in range(3):
        seqs.append(await _insert(pool, i + 1, "dialogue.chat", "public",
                                  {"participants": ["A01", "A02"], "mode": "small", "topic_ids": [],
                                   "lines": [], "witnesses": [], "text_display": f"第{i + 1}段"},
                                  actors=["A01", "A02"], minutes=i))
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "client": "obs/test"}))
        welcome = json.loads(await ws.recv())
        assert set(welcome) == {"op", "watermark_tick", "server_time", "session_id"}
        assert welcome["op"] == "welcome" and isinstance(welcome["watermark_tick"], int)
        await ws.send(json.dumps({"op": "subscribe", "channels": [{"name": "events", "filter": {
            "types": [], "actors": [], "locations": [], "grades": []}}]}))
    # 断线期间新事件
    seqs.append(await _insert(pool, 4, "dialogue.chat", "public",
                              {"participants": ["A01", "A02"], "mode": "small", "topic_ids": [],
                               "lines": [], "witnesses": [], "text_display": "断线期新段"},
                              actors=["A01", "A02"], minutes=4))
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "client": "obs/test",
                                  "last_seq": seqs[1]}))
        frames = await _recv_until(ws, lambda f: f.get("op") == "welcome")
        replay = [f for f in frames if f.get("op") == "event"]
        got = [f["seq"] for f in replay]
        assert got == [seqs[2], seqs[3]]  # 缺口两段，seq 严格递增不重不漏
        assert got == sorted(set(got))


@pytest.mark.asyncio
async def test_resync_required_on_large_gap(server: Any) -> None:
    """缺口 > 环缓冲容量 → resync_required（构造 >容量 缺口；容量 03 §5.2=10,000，测试收缩为 5）。"""
    server["app"].state.ws_ring_capacity = 5
    pool, token, url = server["pool"], server["token"], server["url"]
    first = None
    for i in range(12):
        s = await _insert(pool, 100 + i, "agent.work", "public", {"sim_cost_min": 60},
                          actors=["A01"], minutes=10 + i)
        first = first or s
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "last_seq": first}))
        frame = json.loads(await ws.recv())
        assert frame == {"op": "resync_required"}


@pytest.mark.asyncio
async def test_subscribe_filter_types(server: Any) -> None:
    """events 频道 types 过滤：只推 chat，不推 argue。"""
    pool, token, url = server["pool"], server["token"], server["url"]
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token}))
        await ws.recv()
        await ws.send(json.dumps({"op": "subscribe", "channels": [{"name": "events", "filter": {
            "types": ["dialogue.chat"], "actors": [], "locations": [], "grades": []}}]}))
        await _insert(pool, 200, "dialogue.argue", "public",
                      {"participants": ["A01", "A03"], "reason_hint": "x", "lines": [], "witnesses": []},
                      actors=["A01", "A03"], minutes=20)
        await _insert(pool, 201, "dialogue.chat", "public",
                      {"participants": ["A01", "A02"], "mode": "small", "topic_ids": [], "lines": [],
                       "witnesses": [], "text_display": "过滤命中"},
                      actors=["A01", "A02"], minutes=21)
        frames = await _recv_until(ws, lambda f: f.get("op") == "event", timeout=3.0)
        events = [f for f in frames if f.get("op") == "event"]
        assert events and all(f["data"]["type"] == "dialogue.chat" for f in events)
        # 键名逐字 03 §5.2：event 帧 = {op, seq, data}
        assert all(set(f) == {"op", "seq", "data"} for f in events)


@pytest.mark.asyncio
async def test_state_diff_coalesce_500ms(server: Any) -> None:
    """state_diff 从事件流派生 + 500ms 合帧：同批多次变更同 agent 只留最新（03 §6.3）。"""
    server["app"].state.ws_poll_interval = 0.4  # 拉大轮询窗口，保证两事件落同一合帧批次（测试确定性）
    pool, token, url = server["pool"], server["token"], server["url"]
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token}))
        await ws.recv()
        await ws.send(json.dumps({"op": "subscribe", "channels": [{"name": "world_state"}]}))
        await _insert(pool, 300, "agent.move", "internal",
                      {"from": "a", "to": "corp.pantry", "sim_cost_min": 10}, actors=["A01"], minutes=30)
        await _insert(pool, 300, "state.needs_delta", "internal",
                      {"changes": [{"agent_id": "A01", "need": "mood", "delta": 5, "new_value": 70,
                                    "cause": "1"},
                                   {"agent_id": "A01", "need": "hunger", "delta": -4, "new_value": 40,
                                    "cause": "1"}]},
                      minutes=31)
        frames = await _recv_until(ws, lambda f: f.get("op") == "state_diff", timeout=3.0)
        diffs = [f for f in frames if f.get("op") == "state_diff"]
        assert len(diffs) == 1  # 合帧：一批一帧
        (cell,) = diffs[0]["data"]["agents"]
        assert cell["id"] == "A01" and cell["location_id"] == "corp.pantry"
        assert cell["mood"] == 70 and cell["needs"] == {"hunger": 40}


@pytest.mark.asyncio
async def test_heartbeat_timeout_disconnect(server: Any) -> None:
    """90s 无客户端帧服务端断开（03 §5.2；测试收缩为 0.4s）。"""
    server["app"].state.ws_heartbeat_timeout = 0.4
    token, url = server["token"], server["url"]
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token}))
        await ws.recv()
        # ping/pong 正常工作
        await ws.send(json.dumps({"op": "ping", "t": 1}))
        pong = json.loads(await ws.recv())
        assert pong["op"] == "pong"
        # 此后静默 → 看门狗断开
        try:
            await asyncio.wait_for(ws.recv(), timeout=5.0)
            raise AssertionError("应当被断开")
        except (websockets.exceptions.ConnectionClosed, TimeoutError) as e:
            assert isinstance(e, websockets.exceptions.ConnectionClosed), e


@pytest.mark.asyncio
async def test_bad_token_hello_closed(server: Any) -> None:
    """token 错误的 hello → 连接关闭；access_log 无事件内容（03 §8.1）。"""
    import sqlite3
    url = server["url"]
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": "bad_token"}))
        with pytest.raises(websockets.exceptions.ConnectionClosedError) as exc:
            await ws.recv()
        assert exc.value.rcvd.code == 4401
    conn = sqlite3.connect(server["token_db"])
    rows = conn.execute("SELECT endpoint FROM access_log").fetchall()
    conn.close()
    assert all(r[0] == "/ws" for r in rows)  # 端点级，不含事件内容
