"""T-SYN-05 摄入 API 验收（04 §9.1 全约定；05 §4.1 摄入事务；真栈 uvicorn + httpx/websockets）。"""

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
import websockets

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.ingest import create_app

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_ingest_api_test"
TOKEN = "test-ingest-token"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
BASE = dt.datetime(2026, 10, 12, 9, 0, tzinfo=LOCAL_TZ)
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _event(seq: int, *, type_: str = "agent.move", visibility: str = "internal",
           payload: dict | None = None, ui: dict | None = None,
           schema_version: int = 1, minutes: int | None = None) -> dict[str, Any]:
    st = BASE + dt.timedelta(minutes=minutes if minutes is not None else seq)
    return {
        "seq": seq, "tick": seq, "sim_time": st.isoformat(), "wall_time": st.isoformat(),
        "type": type_, "source": "agent:A01", "trigger": "autonomous",
        "arc_id": None, "location_id": "corp.tech", "actors": ["A01"], "rng_seed": None,
        "payload": payload if payload is not None else {"from": "a", "to": "b", "sim_cost_min": 1},
        "visibility": visibility, "ui": ui, "schema_version": schema_version,
    }


@pytest_asyncio.fixture
async def server() -> Any:
    dsn = _build_db(DB_NAME, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    app = create_app(pool=pool, token_hash=TOKEN_HASH)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    yield {"http": f"http://127.0.0.1:{port}", "ws": f"ws://127.0.0.1:{port}/v1/ingest/ws",
           "pool": pool, "app": app}
    srv.should_exit = True
    await task
    await pool.close()
    _drop_db(DB_NAME)


async def test_auth_required(server) -> None:
    """验收 1：无/错 token 全部端点 401；配置与库中无 token 原文（只哈希）。"""
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        for method, path, body in [
            ("POST", "/v1/events:batch", {"from_seq": 1, "events": []}),
            ("POST", "/v1/memories:batch", {"from_id": 1, "memories": []}),
            ("POST", "/v1/snapshot", {"sim_day": "2026-10-12", "digest": "x", "state": {}}),
            ("GET", "/v1/health", None),
        ]:
            assert (await c.request(method, path, json=body)).status_code == 401
            assert (await c.request(method, path, json=body,
                                    headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert server["app"].state.token_hash == TOKEN_HASH != TOKEN
    assert TOKEN not in json.dumps(dict(server["app"].state.__dict__.get("_state", {})),
                                 default=str)


async def test_events_batch_idempotent(server) -> None:
    """验收 2：同批 POST 两次，行数与 SUM(seq) 不变。"""
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        body = {"from_seq": 1, "events": [_event(i) for i in range(1, 11)]}
        r1 = await c.post("/v1/events:batch", json=body, headers=AUTH)
        assert r1.status_code == 200 and r1.json()["acked_upto"] == 10
        r2 = await c.post("/v1/events:batch", json=body, headers=AUTH)
        assert r2.status_code == 200
    pool = server["pool"]
    assert await pool.fetchval("SELECT count(*) FROM events") == 10
    assert await pool.fetchval("SELECT sum(seq) FROM events") == 55


async def test_out_of_order_409(server) -> None:
    """验收 3：from_seq != max+1 且超窗 → 409 且 body 含 expected_seq。"""
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        await c.post("/v1/events:batch", json={"from_seq": 1, "events": [_event(1)]}, headers=AUTH)
        r = await c.post("/v1/events:batch",
                         json={"from_seq": 50000, "events": [_event(50000)]}, headers=AUTH)
        assert r.status_code == 409 and r.json()["expected_seq"] == 2


async def test_catchup_window_reorder(server) -> None:
    """验收 4：窗口内乱序两批（to=1000 先于 from=1 到达）均接受且最终有序落库。"""
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        r1 = await c.post("/v1/events:batch",
                          json={"from_seq": 501, "events": [_event(i) for i in range(501, 1001)]},
                          headers=AUTH)
        assert r1.status_code == 200 and r1.json()["acked_upto"] == 0  # 缓冲未落库
        r2 = await c.post("/v1/events:batch",
                          json={"from_seq": 1, "events": [_event(i) for i in range(1, 501)]},
                          headers=AUTH)
        assert r2.status_code == 200 and r2.json()["acked_upto"] == 1000  # 冲刷连续段
    pool = server["pool"]
    assert await pool.fetchval("SELECT count(*) FROM events") == 1000
    assert await pool.fetchval("SELECT min(seq) FROM events") == 1
    assert await pool.fetchval("SELECT max(seq) FROM events") == 1000


async def test_schema_version_reject(server, caplog) -> None:
    """验收 5：schema_version=2 整批拒绝、events 无新增、日志有 ERROR。"""
    with caplog.at_level("ERROR", logger="worldsim.ingest.routes"):
        async with httpx.AsyncClient(base_url=server["http"]) as c:
            r = await c.post("/v1/events:batch",
                             json={"from_seq": 1, "events": [_event(1, schema_version=2)]},
                             headers=AUTH)
            assert r.status_code == 400 and r.json()["reason"] == "schema"
    assert await server["pool"].fetchval("SELECT count(*) FROM events") == 0
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_derived_task_atomic(server) -> None:
    """验收 6：relation.changed 摄入同事务得 events 行 + derived_task 行（relchg:e<seq>）。"""
    ev = _event(1, type_="relation.changed", payload={"changes": [
        {"a_id": "A01", "b_id": "A02", "delta_affinity": 3, "delta_tension": 0}]})
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        r = await c.post("/v1/events:batch", json={"from_seq": 1, "events": [ev]}, headers=AUTH)
        assert r.status_code == 200
    pool = server["pool"]
    assert await pool.fetchval("SELECT count(*) FROM events WHERE seq=1") == 1
    row = await pool.fetchrow("SELECT task_type, dedupe_key, sim_day FROM derived_task")
    assert row["task_type"] == "project_relation_change" and row["dedupe_key"] == "relchg:e1"
    assert row["sim_day"] == BASE.date()
    # D6：ui.grade 初值事件同排 materialize_event_grade
    ev2 = _event(2, type_="dialogue.chat", visibility="public",
                 payload={"participants": ["A01", "A02"], "lines": [], "text_display": "t"},
                 ui={"grade": "B"})
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        await c.post("/v1/events:batch", json={"from_seq": 2, "events": [ev2]}, headers=AUTH)
    assert await pool.fetchval(
        "SELECT count(*) FROM derived_task WHERE dedupe_key='grade:e2'") == 1


async def test_snapshot_upsert_and_digest(server) -> None:
    """验收 7：同 sim_day 两次 POST 后者覆盖；digest 不符 4xx 拒收。"""
    from worldsim.snapshot.canonical import canonical, sha256_hex
    state1 = {"sim": {"day": "2026-10-12"}, "agents": [], "health": {"cost_daily_micro_cny": None}}
    state2 = {"sim": {"day": "2026-10-12"}, "agents": [{"id": "A01"}],
              "health": {"cost_daily_micro_cny": 100}}
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        for st in (state1, state2):
            d = "sha256:" + sha256_hex(canonical(st))
            r = await c.post("/v1/snapshot",
                             json={"sim_day": "2026-10-12", "digest": d, "state": st},
                             headers=AUTH)
            assert r.status_code == 200
        r = await c.post("/v1/snapshot",
                         json={"sim_day": "2026-10-12", "digest": "sha256:bad", "state": state1},
                         headers=AUTH)
        assert 400 <= r.status_code < 500
    row = await server["pool"].fetchrow(
        "SELECT state, digest FROM world_state_snapshot WHERE sim_day='2026-10-12'")
    assert json.loads(row["state"])["agents"] == [{"id": "A01"}]  # 后者覆盖
    # d−1 定稿任务（05 §4.2）
    tasks = {r["dedupe_key"] for r in await server["pool"].fetch("SELECT dedupe_key FROM derived_task")}
    assert "relation_daily:2026-10-11" in tasks and "health_daily:2026-10-11" in tasks


async def test_ws_ack_after_commit(server) -> None:
    """验收 8：故障注入使事务提交失败 → 客户端收不到 ack（ack 仅在 commit 后发出）。"""
    buffer = server["app"].state.batch_buffer
    orig = buffer.submit

    async def boom(*a: Any, **kw: Any) -> int:
        raise RuntimeError("injected commit failure")

    buffer.submit = boom  # type: ignore[method-assign]
    try:
        async with websockets.connect(server["ws"], additional_headers=AUTH) as ws:
            await ws.send(json.dumps({"frame": "hello", "token": TOKEN, "from_seq": 0,
                                      "kernel_ver": "test"}))
            hello_ack = json.loads(await ws.recv())
            assert hello_ack["frame"] == "hello_ack" and hello_ack["max_seq"] == 0
            await ws.send(json.dumps({"frame": "batch", "from": 1, "to": 1,
                                      "events": [_event(1)], "schema_version": 1}))
            with pytest.raises((asyncio.TimeoutError, websockets.exceptions.ConnectionClosed)):
                await asyncio.wait_for(ws.recv(), timeout=3)  # 无 ack
        assert await server["pool"].fetchval("SELECT count(*) FROM events") == 0
    finally:
        buffer.submit = orig  # type: ignore[method-assign]
        # 恢复后正常收发
        async with websockets.connect(server["ws"], additional_headers=AUTH) as ws:
            await ws.send(json.dumps({"frame": "hello", "token": TOKEN, "from_seq": 0,
                                      "kernel_ver": "test"}))
            await ws.recv()
            await ws.send(json.dumps({"frame": "batch", "from": 1, "to": 1,
                                      "events": [_event(1)], "schema_version": 1}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert ack["frame"] == "ack" and ack["upto"] == 1


async def test_health_endpoint(server) -> None:
    """验收 9（curl 冒烟等价）：/v1/health 返回 max(seq)/延迟/最近 digest 三字段。"""
    async with httpx.AsyncClient(base_url=server["http"]) as c:
        await c.post("/v1/events:batch", json={"from_seq": 1, "events": [_event(1)]}, headers=AUTH)
        r = await c.get("/v1/health", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["max_seq"] == 1
        assert "ingest_lag_seconds" in body
        assert body["last_digest"] is None  # digest_log 归 T-SYN-09，未建表时缺席


async def test_timeline_divergence_sim_time_regression(server, tmp_path) -> None:
    """R3 #2①：重启后低位 seq 重排且 sim_time 系统性回退 → 副本重建、ack 带 reset、alerts 留痕。"""
    from worldsim.audit import alerts
    alerts.set_path(tmp_path / "alerts.log")
    try:
        async with httpx.AsyncClient(base_url=server["http"]) as c:
            old = [_event(i, payload={"old": i}) for i in range(1, 11)]
            r1 = await c.post("/v1/events:batch", json={"from_seq": 1, "events": old},
                              headers=AUTH)
            assert r1.status_code == 200 and r1.json()["acked_upto"] == 10
            # 新时间线：seq 从 1 重排、sim_time 回退 7 天（split-brain 重启重排场景）
            new = [_event(i, payload={"new": i}, minutes=-10080 + i) for i in range(1, 6)]
            r2 = await c.post("/v1/events:batch", json={"from_seq": 1, "events": new},
                              headers=AUTH)
            assert r2.status_code == 200 and r2.json()["reset"] is True
            assert r2.json()["acked_upto"] == 5
        pool = server["pool"]
        assert await pool.fetchval("SELECT count(*) FROM events") == 5  # 旧时间线已清空
        assert await pool.fetchval("SELECT payload->>'new' FROM events WHERE seq=1") == "1"
        text = (tmp_path / "alerts.log").read_text(encoding="utf-8")
        assert "replica.timeline_diverged" in text
    finally:
        alerts.set_path(None)


async def test_timeline_divergence_content_conflict(server, tmp_path) -> None:
    """R3 #2②：seq 撞车且内容不一致（sim_time 未回退）→ 同样走重建通道，不静默吞。"""
    from worldsim.audit import alerts
    alerts.set_path(tmp_path / "alerts.log")
    try:
        async with httpx.AsyncClient(base_url=server["http"]) as c:
            old = [_event(i, payload={"old": i}) for i in range(1, 11)]
            await c.post("/v1/events:batch", json={"from_seq": 1, "events": old}, headers=AUTH)
            new = [_event(i, payload={"new": i}, minutes=2000 + i) for i in range(1, 6)]
            r = await c.post("/v1/events:batch", json={"from_seq": 1, "events": new},
                             headers=AUTH)
            assert r.status_code == 200 and r.json()["reset"] is True
        pool = server["pool"]
        assert await pool.fetchval("SELECT count(*) FROM events") == 5
        assert await pool.fetchval("SELECT payload->>'new' FROM events WHERE seq=5") == "5"
    finally:
        alerts.set_path(None)
