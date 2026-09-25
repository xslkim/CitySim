"""T-WEB-03 快照与角色接口组测试（03 §5.1 形态、05 §3.6 白名单边界）。"""

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

DB_NAME = "worldsim_obs_rest_agents_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
SNAP_DAY = dt.date.today() + dt.timedelta(days=2)
SNAP_SIM_TIME = dt.datetime.combine(SNAP_DAY, dt.time(0, 0), tzinfo=LOCAL_TZ)

PERSONA_SIX_KEYS = {"big_five", "backstory", "appearance", "signature_quirk",
                    "contrast_public", "speech_style_public"}  # 05 §3.6 P2-6


@pytest_asyncio.fixture
async def client() -> Any:
    dsn = _build_db(
        DB_NAME,
        DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql",
        DDL_DIR / "obs_views_v1.sql", DDL_DIR / "obs_derived_v1.sql",
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    # 灌快照（state 形态 = 05 §3.6 白名单；A01 带完整六键 persona_display）
    agents = []
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, gender, age, room_no, department, job_title,"
                                " cognition_tier, position, needs FROM agents ORDER BY id")
    for r in rows:
        agents.append({
            "id": r["id"], "name": r["name"], "gender": r["gender"], "age": r["age"],
            "room_no": r["room_no"], "department": r["department"], "job_title": r["job_title"],
            "cognition_tier": r["cognition_tier"],
            "persona_display": {
                "big_five": {"openness": 72}, "backstory": [{"age": 12, "event": "x"}],
                "appearance": {"signature_color": {"id": 14, "name": "雾蓝", "hex": "#C3CDDA"}},
                "signature_quirk": "转笔", "contrast_public": "白天安静", "speech_style_public": "克制",
            } if r["id"] == "A01" else {},
            "routine": {"regular": {"work": "工作日 09:00~18:00", "sleep": "00:30~06:30"},
                        "weekly_bias_public": None},
            "needs": json.loads(r["needs"]) if isinstance(r["needs"], str) else r["needs"],
            "mood": 72, "position": r["position"], "activity": "agent.work",
            "goals": [{"goal": "晋升答辩通过", "blocked_count": 2, "frustration": 12}] if r["id"] == "A01" else [],
        })
    state = {
        "sim": {"day": SNAP_DAY.isoformat(), "sim_time": SNAP_SIM_TIME.isoformat(), "compression_ratio": 3.0},
        "agents": agents, "relations": [], "economy": {"stocks": [{"symbol": "星澜科技", "price": 1012.4}]},
        "health": {"cost_daily_micro_cny": 0}, "announcements": [],
    }
    await pool.execute(
        "INSERT INTO obs.world_state_snapshot (sim_day, state, digest) VALUES ($1, $2::jsonb, 'sha256:test')",
        SNAP_DAY, json.dumps(state, ensure_ascii=False),
    )
    # 反思记忆（content 原文永不出站；content_display 为展示通道）
    await pool.execute(
        "INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance)"
        " VALUES ('A01', $1, 'reflection', 'RAW_SECRET', '我意识到李梅最近躲着我', 8),"
        "        ('A01', $2, 'reflection', 'RAW2', NULL, 5)",
        SNAP_SIM_TIME, SNAP_SIM_TIME + dt.timedelta(hours=1),
    )
    # 一条 public dialogue（active_dialogues 窗口内：tick=watermark）
    await pool.execute(
        "INSERT INTO events (tick, sim_time, type, source, trigger, actors, location_id, payload, visibility, ui)"
        " VALUES (10, $1, 'dialogue.chat', 'agent:A01', 'autonomous', '{A01,A02}', 'corp.pantry',"
        " '{\"participants\":[\"A01\",\"A02\"],\"lines\":[{\"speaker\":\"A01\",\"text_display\":\"早\",\"at_offset_s\":0.0}],"
        "\"text_display\":\"早晨闲聊\"}'::jsonb, 'public', '{\"grade\":\"C\"}'::jsonb)",
        SNAP_SIM_TIME,
    )
    import tempfile
    token_db = str(Path(tempfile.mkdtemp()) / "tokens.db")
    app = create_app(pool=pool, token_db_path=token_db)
    token = TokenStore(token_db).issue("tester")
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    await pool.close()
    _drop_db(DB_NAME)


@pytest.mark.asyncio
async def test_snapshot_shape(client: Any) -> None:
    """/api/snapshot：agents 数组长度 = seed 人数；字段名与 03 §5.1 逐字一致。"""
    body = (await client.get("/api/snapshot")).json()
    assert body["ok"] is True
    data = body["data"]
    assert set(data) == {"tick", "sim_time", "sim_day", "compression_ratio",
                         "agents", "economy", "active_dialogues"}
    assert len(data["agents"]) == 8
    a = data["agents"][0]
    assert set(a) == {"id", "name", "lod", "location_id", "activity", "mood", "needs"}
    assert data["economy"]["stocks"] == [{"symbol": "星澜科技", "price": 1012.4}]
    (dlg,) = data["active_dialogues"]
    assert set(dlg) == {"event_seq", "participants", "location_id", "lines"}
    assert dlg["lines"][0]["at_offset_s"] == 0.0


@pytest.mark.asyncio
async def test_persona_whitelist_six_keys(client: Any) -> None:
    """人设卡 persona_display ⊆ 六键；响应任何层级无 secret/trigger_point（05 §3.6）。"""
    body = (await client.get("/api/agents/A01")).json()["data"]
    assert set(body["persona_display"]) == PERSONA_SIX_KEYS
    assert "secret" not in json.dumps(body, ensure_ascii=False)
    assert "trigger_point" not in json.dumps(body, ensure_ascii=False)


@pytest.mark.asyncio
async def test_reflections_display_only(client: Any) -> None:
    """反思通道仅 content_display：响应无 content 字段；未过审行（NULL）不出现。"""
    body = (await client.get("/api/agents/A01/reflections")).json()["data"]
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert set(item) == {"memory_id", "sim_time", "content_display", "importance"}
    assert item["content_display"] == "我意识到李梅最近躲着我"
    assert "content" not in item and "RAW" not in json.dumps(body)


@pytest.mark.asyncio
async def test_agent_id_invalid_422(client: Any) -> None:
    """agent id 校验 `^A(0[1-9]|[1-3][0-9]|40)$`：ag07/A41 拒（04 §5.2 同形）。"""
    for bad in ("ag07", "A41", "A00"):
        r = await client.get(f"/api/agents/{bad}")
        assert r.status_code == 422, bad


@pytest.mark.asyncio
async def test_state_matches_snapshot(client: Any) -> None:
    """/api/agents/:id/state 需求值与 obs.world_state_snapshot 直查勾稽一致。"""
    body = (await client.get("/api/agents/A01/state")).json()["data"]
    assert body["mood"] == 72
    assert body["frustration"] == 12
    assert body["goals"][0]["goal"] == "晋升答辩通过"
    assert body["needs"] is not None and isinstance(body["needs"], dict)


@pytest.mark.asyncio
async def test_schedule_and_unknown_agent(client: Any) -> None:
    body = (await client.get("/api/agents/A01/schedule")).json()["data"]
    assert body["routine"]["regular"]["work"] == "工作日 09:00~18:00"
    assert len(body["events"]) == 1 and body["events"][0]["type"] == "dialogue.chat"
    r = await client.get("/api/agents/A08/state")  # A08 在 seed 但快照 agents 有 → 200
    assert r.status_code == 200
