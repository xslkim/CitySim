"""T-WEB-20 端到端联调测试（03 §9.1 断线演练 / 双通道抓包验收；真栈 uvicorn + websockets）。

- 断线演练：kill obs-api 后恢复 → 按 last_seq resume 不重不漏（seq 连续性校验）。
- 双通道：REST/WS 全帧 grep 无 text_raw/原文（00 §4 红线 7；05 §2.1 block→internal 不出展示文本）。
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

DB_NAME = "worldsim_e2e_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
BASE = dt.datetime.now(LOCAL_TZ).replace(hour=14, minute=0, second=0, microsecond=0) + dt.timedelta(days=12)
SECRET = "RAW_SECRET_绝不出站"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    def __init__(self, pool: Any, token_db: str, token: str) -> None:
        self.pool = pool
        self.token_db = token_db
        self.token = token
        self.srv: uvicorn.Server | None = None
        self.task: asyncio.Task | None = None
        self.port = 0

    async def start(self) -> None:
        app = create_app(pool=self.pool, token_db_path=self.token_db)
        app.state.ws_poll_interval = 0.05
        self.port = _free_port()
        self.srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.task = asyncio.create_task(self.srv.serve())
        for _ in range(100):
            if self.srv.started:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("obs-api 未就绪")

    async def stop(self) -> None:
        assert self.srv and self.task
        self.srv.should_exit = True
        await self.task


@pytest_asyncio.fixture
async def stack() -> Any:
    dsn = _build_db(
        DB_NAME,
        DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql",
        DDL_DIR / "obs_views_v1.sql", DDL_DIR / "obs_derived_v1.sql",
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    # 快照（/api/snapshot 数据源）
    state = {
        "sim": {"day": BASE.date().isoformat(), "sim_time": BASE.isoformat(), "compression_ratio": 3.0},
        "agents": [{"id": r["id"], "name": r["name"], "cognition_tier": "star",
                    "position": r["position"], "activity": None,
                    "needs": {"hunger": 50}, "mood": 60, "goals": [],
                    "persona_display": {"big_five": None, "backstory": None,
                                        "appearance": {"signature_color": {"hex": "#C3CDDA"}},
                                        "signature_quirk": None, "contrast_public": None,
                                        "speech_style_public": None},
                    "routine": {}} for r in await pool.fetch("SELECT id, name, position FROM agents ORDER BY id")],
        "relations": [], "economy": {"stocks": [{"symbol": "星澜科技", "price": 1012.4}]},
        "health": {"cost_daily_micro_cny": 0}, "announcements": [],
    }
    await pool.execute(
        "INSERT INTO obs.world_state_snapshot (sim_day, state, digest) VALUES ($1, $2::jsonb, 'sha256:t')",
        BASE.date(), json.dumps(state, ensure_ascii=False),
    )
    # 事件：public 含 text_raw + block→internal 事件（无 text_display，原文只留 text_raw）
    await pool.execute(
        "INSERT INTO events (tick, sim_time, type, source, trigger, actors, location_id, payload, visibility, ui)"
        " VALUES"
        f" (1, '{BASE.isoformat()}', 'dialogue.chat', 'agent:A01', 'autonomous', '{{A01,A02}}', 'corp.pantry',"
        f"  '{{\"participants\":[\"A01\",\"A02\"],\"lines\":[],\"witnesses\":[],\"text_display\":\"茶水间闲聊\","
        f"   \"text_raw\":\"{SECRET}\"}}'::jsonb, 'public', '{{\"grade\":\"B\"}}'::jsonb),"
        f" (2, '{(BASE + dt.timedelta(minutes=5)).isoformat()}', 'dialogue.gossip', 'agent:A02', 'autonomous',"
        f"  '{{A02,A03}}', 'corp.pantry', '{{\"teller\":\"A02\",\"listener\":\"A03\",\"about\":\"A01\","
        f"   \"cites\":[],\"lines\":[],\"text_raw\":\"{SECRET}\"}}'::jsonb, 'internal', '{{\"grade\":\"C\"}}'::jsonb)",
    )
    token_db = str(Path(tempfile.mkdtemp()) / "tokens.db")
    token = TokenStore(token_db).issue("e2e")
    st = Stack(pool, token_db, token)
    await st.start()
    try:
        yield st
    finally:
        await st.stop()
        await pool.close()
        _drop_db(DB_NAME)


async def _hello_subscribe(url: str, token: str, last_seq: int | None = None) -> Any:
    ws = await websockets.connect(url)
    hello = {"op": "hello", "token": token, "client": "e2e/1.0.0"}
    if last_seq is not None:
        hello["last_seq"] = last_seq
    await ws.send(json.dumps(hello))
    frames = []
    while True:
        f = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        frames.append(f)
        if f.get("op") == "welcome":
            break
        if f.get("op") == "resync_required":
            break
    await ws.send(json.dumps({"op": "subscribe", "channels": [
        {"name": "events", "filter": {"types": [], "actors": [], "locations": [], "grades": []}}]}))
    return ws, frames


@pytest.mark.asyncio
async def test_resume_after_obs_api_restart(stack: Stack) -> None:
    """断线演练（03 §9.1 末行口径的服务重启版）：停 obs-api 恢复后按 last_seq resume 不重不漏。"""
    url = f"ws://127.0.0.1:{stack.port}/ws"
    ws, frames = await _hello_subscribe(url, stack.token)
    got_live = [f["seq"] for f in await _collect(ws, 1.0) if f.get("op") == "event"]
    await ws.close()
    await stack.stop()  # 停 obs-api（断线）
    # 断线期间内核照跑：新事件落库
    await stack.pool.execute(
        "INSERT INTO events (tick, sim_time, type, source, trigger, actors, payload, visibility)"
        f" VALUES (3, '{(BASE + dt.timedelta(minutes=10)).isoformat()}', 'world.announce', 'world', 'world',"
        "  '{}', '{\"title\":\"停水通知\",\"body\":\"今晚 22:00\",\"scope\":\"all\",\"text_display\":\"各位注意\"}'::jsonb, 'public')"
    )
    new_seq = await stack.pool.fetchval("SELECT max(seq) FROM events")
    await stack.start()  # 恢复
    url = f"ws://127.0.0.1:{stack.port}/ws"
    ws2, frames2 = await _hello_subscribe(url, stack.token, last_seq=2)
    replayed = [f["seq"] for f in frames2 if f.get("op") == "event"]
    assert replayed == [new_seq]  # 缺口恰一条，不重不漏
    more = [f["seq"] for f in await _collect(ws2, 0.5) if f.get("op") == "event"]
    assert not set(replayed) & set(more)  # resume 与 live 无重复
    await ws2.close()


async def _collect(ws: Any, seconds: float) -> list[dict]:
    frames = []
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            frames.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.05, deadline - asyncio.get_event_loop().time()))))
        except TimeoutError:
            break
    return frames


@pytest.mark.asyncio
async def test_text_raw_never_outbound(stack: Stack) -> None:
    """双通道抓包：REST 全端点 + WS 全帧不含 text_raw/原文；internal 事件无展示文本键（05 §2.1）。"""
    import httpx
    base = f"http://127.0.0.1:{stack.port}"
    auth = {"Authorization": f"Bearer {stack.token}"}
    bodies: list[str] = []
    async with httpx.AsyncClient(base_url=base, headers=auth) as c:
        for path in ("/api/snapshot", "/api/events", "/api/agents", "/api/agents/A01",
                     "/api/agents/A01/state", "/api/agents/A01/schedule", "/api/agents/A01/reflections",
                     "/api/ripple/today", "/api/relations/snapshots", "/api/health", "/api/usage"):
            r = await c.get(path)
            assert r.status_code == 200, (path, r.text[:200])
            bodies.append(r.text)
        # internal 事件（seq=2）在事件流内但无展示文本键
        ev2 = json.loads((await c.get("/api/events?type=dialogue.gossip")).text)["data"]["items"][0]
        assert "text_display" not in ev2["payload"] and "text_raw" not in ev2["payload"]
    url = f"ws://127.0.0.1:{stack.port}/ws"
    ws, frames = await _hello_subscribe(url, stack.token, last_seq=0)
    frames += await _collect(ws, 1.0)
    await ws.close()
    bodies.append(json.dumps(frames, ensure_ascii=False))
    for body in bodies:
        assert "text_raw" not in body, body[:300]
        assert SECRET not in body
        # 键形态断言（文案中合法的 "prompt 多样性" 建议语不算泄漏，03 §3.6 表行）
        assert '"embedding"' not in body and '"prompt"' not in body


@pytest.mark.asyncio
async def test_snapshot_merge_via_api(stack: Stack) -> None:
    """`/api/snapshot?tick=` 历史合并端到端可达（03 §4.2 服务端合并路径）。"""
    import httpx
    base = f"http://127.0.0.1:{stack.port}"
    async with httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {stack.token}"}) as c:
        r1 = await c.get("/api/snapshot?tick=1")
        r2 = await c.get("/api/snapshot?tick=1")
        assert r1.status_code == 200
        assert r1.text == r2.text  # 同 tick 两次响应逐字节一致（验收 1）
        assert r1.json()["data"]["tick"] == 1
