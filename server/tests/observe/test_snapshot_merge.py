"""T-WEB-04 `/api/snapshot?tick=` 历史合并与 diff 自检测试（03 §4.2）。

构造两日快照 + 其间 agent.move/state.needs_delta 增量：合并结果与次日快照字段级一致；
snapshot_diff_check.py 正常库退出 0；删一条 agent.move（scratch 库 session_replication_role
旁路 append-only 触发器，仅测试用途）后退出非 0。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.observe.snapshot_merge import merged_state_at_tick, snapshot_at_tick

DB_NAME = "worldsim_snapshot_merge_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
DAY1 = dt.date.today() + dt.timedelta(days=4)
DAY2 = DAY1 + dt.timedelta(days=1)
T0 = dt.datetime.combine(DAY1, dt.time(0, 0), tzinfo=LOCAL_TZ)
T1 = dt.datetime.combine(DAY2, dt.time(0, 0), tzinfo=LOCAL_TZ)
SERVER_ROOT = Path(__file__).resolve().parents[2]


def _state(pos: str, hunger: int, mood: int) -> dict:
    agents = [
        {"id": "A01", "name": "林晚", "cognition_tier": "star", "position": pos, "activity": None,
         "needs": {"hunger": hunger, "energy": 55, "mood": mood, "social": 80, "achievement": 61,
                   "wealth": 35},
         "mood": mood,
         "goals": [{"goal": "g", "blocked_count": 0, "frustration": 3}]},
        {"id": "A02", "name": "周叙", "cognition_tier": "star", "position": "apt.L2.201",
         "activity": None,
         "needs": {"hunger": 40, "energy": 60, "mood": 70, "social": 75, "achievement": 50,
                   "wealth": 30},
         "mood": 70, "goals": []},
    ]
    day = DAY1 if pos == "apt.L2.203" else DAY2
    t = T0 if pos == "apt.L2.203" else T1
    return {
        "sim": {"day": day.isoformat(), "sim_time": t.isoformat(), "compression_ratio": 3.0},
        "agents": agents, "relations": [], "economy": {"stocks": []},
        "health": {"cost_daily_micro_cny": None}, "announcements": [],
    }


@pytest_asyncio.fixture
async def pool() -> Any:
    dsn = _build_db(
        DB_NAME,
        DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql",
        DDL_DIR / "obs_views_v1.sql", DDL_DIR / "obs_derived_v1.sql",
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    await pool.execute(
        "INSERT INTO obs.world_state_snapshot (sim_day, state, digest) VALUES"
        " ($1, $2::jsonb, 'sha256:d1'), ($3, $4::jsonb, 'sha256:d2')",
        DAY1, json.dumps(_state("apt.L2.203", 50, 60), ensure_ascii=False),
        DAY2, json.dumps(_state("corp.tech", 44, 66), ensure_ascii=False),
    )
    # 增量事件：A01 移动 + 需求/情绪结算（new_value 直写口径 04 §6.5）
    await pool.execute(
        "INSERT INTO events (tick, sim_time, type, source, trigger, actors, payload, visibility) VALUES"
        " (5, $1, 'agent.move', 'agent:A01', 'autonomous', '{A01}',"
        "  '{\"from\":\"apt.L2.203\",\"to\":\"corp.tech\",\"sim_cost_min\":30}'::jsonb, 'internal'),"
        " (6, $2, 'state.needs_delta', 'system', 'system', '{A01}',"
        "  '{\"changes\":[{\"agent_id\":\"A01\",\"need\":\"hunger\",\"delta\":-6,\"new_value\":44,\"cause\":\"5\"},"
        "    {\"agent_id\":\"A01\",\"need\":\"mood\",\"delta\":6,\"new_value\":66,\"cause\":\"5\"}]}'::jsonb, 'internal')",
        T0 + dt.timedelta(hours=8), T0 + dt.timedelta(hours=9),
    )
    try:
        yield pool
    finally:
        await pool.close()
        _drop_db(DB_NAME)


@pytest.mark.asyncio
async def test_merge_equals_next_snapshot(pool: Any) -> None:
    """合并至次日快照时点 = 次日快照字段级一致（position/needs/mood，03 §4.2 边界 3）。"""
    tick = await pool.fetchval("SELECT max(tick) FROM obs.events WHERE sim_time <= $1", T1)
    merged, _ = await merged_state_at_tick(pool, int(tick))
    a01 = next(a for a in merged["agents"] if a["id"] == "A01")
    assert a01["position"] == "corp.tech"
    assert a01["needs"]["hunger"] == 44 and a01["needs"]["mood"] == 66 and a01["mood"] == 66
    a02 = next(a for a in merged["agents"] if a["id"] == "A02")
    assert a02["position"] == "apt.L2.201" and a02["needs"]["hunger"] == 40


@pytest.mark.asyncio
async def test_merge_deterministic(pool: Any) -> None:
    """同 tick 两次合并逐字节一致（验收 1 口径）。"""
    tick = await pool.fetchval("SELECT max(tick) FROM obs.events WHERE sim_time <= $1", T1)
    d1 = await snapshot_at_tick(pool, int(tick))
    d2 = await snapshot_at_tick(pool, int(tick))
    assert json.dumps(d1, ensure_ascii=False, sort_keys=True) == json.dumps(
        d2, ensure_ascii=False, sort_keys=True)
    assert d1["tick"] == int(tick)


@pytest.mark.asyncio
async def test_snapshot_diff_check_script(pool: Any) -> None:
    """验收 3：正常库退出码 0；删一条 agent.move 后退出码非 0（事件落库完整性回归）。"""
    dsn = f"postgresql:///{DB_NAME}?host=/tmp"
    env = dict(os.environ)
    script = SERVER_ROOT / "scripts" / "snapshot_diff_check.py"
    r = subprocess.run(
        [sys.executable, str(script), "--dsn", dsn],
        capture_output=True, text=True, env=env, cwd=str(SERVER_ROOT),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SET session_replication_role = replica")  # 仅 scratch 库：旁路 append-only 触发器
        await conn.execute("DELETE FROM events WHERE type = 'agent.move'")
    finally:
        await conn.close()
    r = subprocess.run(
        [sys.executable, str(script), "--dsn", dsn],
        capture_output=True, text=True, env=env, cwd=str(SERVER_ROOT),
    )
    assert r.returncode != 0
    assert "不一致" in r.stdout
