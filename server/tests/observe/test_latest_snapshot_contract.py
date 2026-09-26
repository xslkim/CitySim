"""T-ITER2-03 latest_snapshot 消费方契约护栏（round2 #3）：rolling/day_end 两分支键集一致 + schedule 端点口径。

回归背景：T-ITER1-01 切换快照数据源后 rolling 分支缺 `sim_day` 键，`/api/agents/{id}/schedule`
缺省 day 调用 KeyError 500（obs-api.log 累计 11 次）。本文件把"两分支返回键集一致"固化为契约测试，
杜绝"换数据源漏审计"第三类复发。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.observe.app import create_app
from worldsim.observe.auth import TokenStore
from worldsim.observe.rest_snapshot import latest_snapshot

DB_NAME = "worldsim_latest_snapshot_contract_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))

# 两分支共有的核心键（消费方依赖集；rolling 特有 tick/sim_time 帧头字段）
CORE_KEYS = {"kind", "sim_day", "state"}

_LATEST_DDL = """
CREATE TABLE public.world_state_latest (
  id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  tick BIGINT NOT NULL,
  sim_time TIMESTAMPTZ NOT NULL,
  state JSONB NOT NULL,
  digest TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


@pytest_asyncio.fixture
async def env() -> Any:
    dsn = _build_db(DB_NAME, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql",
                    DDL_DIR / "health_daily_v1.sql", DDL_DIR / "obs_views_v1.sql",
                    DDL_DIR / "obs_derived_v1.sql")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    day = dt.date(2026, 10, 12)
    async with pool.acquire() as conn:
        agents = [dict(r) for r in await conn.fetch(
            "SELECT id, name, gender, age, room_no, department, job_title, cognition_tier,"
            " position, needs FROM agents ORDER BY id")]
    for a in agents:
        a["persona_display"] = {}
        a["routine"] = {"regular": {"work": "工作日 09:00~18:00", "sleep": "00:30~06:30"}}
        a["mood"] = 70
        a["activity"] = "agent.work"
        a["goals"] = []
        if isinstance(a["needs"], str):
            a["needs"] = json.loads(a["needs"])
    state = {"sim": {"sim_time": "2026-10-12T13:25:00+08:00", "compression_ratio": 3.0},
             "agents": agents, "relations": [], "economy": {"stocks": []}}
    async with pool.acquire() as conn:
        # 日界定稿行（day_end 分支）
        await conn.execute(
            "INSERT INTO obs.world_state_snapshot (sim_day, state, digest) VALUES ($1,$2::jsonb,'d1')",
            day, json.dumps(state, ensure_ascii=False))
        # 当日滚动行（rolling 分支；模拟副本库增量表，主库直连形态需自建同名表/视图）
        await conn.execute(_LATEST_DDL)
        await conn.execute(
            "CREATE OR REPLACE VIEW obs.world_state_latest AS SELECT * FROM public.world_state_latest")
        await conn.execute(
            "INSERT INTO public.world_state_latest (id, tick, sim_time, state, digest)"
            " VALUES (1, 77, $1, $2::jsonb, 'd2')",
            dt.datetime(2026, 10, 12, 13, 25, tzinfo=LOCAL_TZ), json.dumps(state, ensure_ascii=False))
    import tempfile
    token_db = str(Path(tempfile.mkdtemp()) / "tokens.db")
    app = create_app(pool=pool, token_db_path=token_db)
    token = TokenStore(token_db).issue("tester")
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield {"pool": pool, "client": c}
    await pool.close()
    _drop_db(DB_NAME)


@pytest.mark.asyncio
async def test_rolling_branch_key_set_matches_day_end(env: Any) -> None:
    """rolling 分支返回键 ⊇ 核心键集（含 sim_day）——schedule 等消费方缺省日不再 KeyError。"""
    rolling = await latest_snapshot(env["pool"])
    assert rolling is not None and rolling["kind"] == "rolling"
    assert CORE_KEYS <= set(rolling)
    assert rolling["sim_day"] == dt.date(2026, 10, 12)
    # 删掉 rolling 行后回退 day_end，核心键集不变
    await env["pool"].execute("DELETE FROM public.world_state_latest")
    day_end = await latest_snapshot(env["pool"])
    assert day_end is not None and day_end["kind"] == "day_end"
    assert CORE_KEYS <= set(day_end)


@pytest.mark.asyncio
async def test_schedule_default_day_200_on_rolling(env: Any) -> None:
    """rolling 快照下缺省 day 调 schedule：200 + 返回快照日（回归：原为 KeyError 500）。"""
    r = await env["client"].get("/api/agents/A01/schedule")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["day"] == "2026-10-12"


@pytest.mark.asyncio
async def test_schedule_bad_day_422(env: Any) -> None:
    """`?day=bad` 422（原为 500）；合法日与缺省日同口径 200。"""
    r = await env["client"].get("/api/agents/A01/schedule?day=bad")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_param"
    ok = await env["client"].get("/api/agents/A01/schedule?day=2026-10-12")
    assert ok.status_code == 200
    assert ok.json()["data"]["day"] == "2026-10-12"


@pytest.mark.asyncio
async def test_world_stalled_flag_observable(env: Any) -> None:
    """T-ITER2-01④：watermark 与最近角色行为事件差 > 30 tick → snapshot world_stalled:true + llm_status 附原因。"""
    snap_time = dt.datetime(2026, 10, 12, 13, 25, tzinfo=LOCAL_TZ)
    async with env["pool"].acquire() as conn:
        # 最近角色行为在 tick 100，watermark 已到 140（gap 40 > 30）
        await conn.execute(
            "INSERT INTO events (tick, sim_time, type, source, trigger, actors, payload, visibility)"
            " VALUES (100, $1, 'agent.move', 'agent:A01', 'autonomous', '{A01}', '{}'::jsonb, 'public')", snap_time)
        await conn.execute(
            "INSERT INTO events (tick, sim_time, type, source, trigger, actors, payload, visibility)"
            " VALUES (140, $1, 'state.needs_delta', 'system', 'system', '{}', '{}'::jsonb, 'internal')",
            snap_time + dt.timedelta(minutes=10))
    r = await env["client"].get("/api/snapshot")
    data = r.json()["data"]
    assert data["world_stalled"] is True
    assert data["world_stalled_reason"] and "40 tick" in data["world_stalled_reason"]
    assert data["llm_status"]["stalled"] is True
    assert data["llm_status"]["stalled_reason"] == data["world_stalled_reason"]
    # 补上更近的角色行为事件 → 恢复（看门狗双参照：gap 归零）
    async with env["pool"].acquire() as conn:
        await conn.execute(
            "INSERT INTO events (tick, sim_time, type, source, trigger, actors, payload, visibility)"
            " VALUES (141, $1, 'agent.think', 'agent:A02', 'autonomous', '{A02}', '{}'::jsonb, 'internal')",
            snap_time + dt.timedelta(minutes=15))
    data2 = (await env["client"].get("/api/snapshot")).json()["data"]
    assert data2["world_stalled"] is False
    assert data2["llm_status"]["stalled"] is False
