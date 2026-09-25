"""T-AUD-06 审计⑤ LOD 记录完整验收（08 文档 T-AUD-06 验收 1）。"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.audit.conftest import SIM_NOW, _psql_file, close_pool, make_pool
from worldsim.audit.daily import get_item, run_item


@pytest.mark.asyncio
async def test_tier_change_without_event_red() -> None:
    """样例 (a)：升格事件说 star、当前 tier=secondary → 不一致必报红。"""
    pool = await make_pool("worldsim_audit_05a")
    try:
        _psql_file(get_item("05").sample_path, "worldsim_audit_05a")
        res = await run_item(pool, get_item("05"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        kinds = {d["kind"] for d in res.detail}
        assert "tier_mismatch" in kinds
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_promotion_without_catchup_reflection_red() -> None:
    """样例 (b)：升星事件后 1 模拟日内无追赶反思 → 必报红。"""
    pool = await make_pool("worldsim_audit_05b")
    try:
        _psql_file(get_item("05").sample_path, "worldsim_audit_05b")
        res = await run_item(pool, get_item("05"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert "no_catchup_reflection" in {d["kind"] for d in res.detail}
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_normal_rotation_passes() -> None:
    """升格事件 + tier 一致 + 1 日内追赶反思 → 绿。"""
    pool = await make_pool("worldsim_audit_05c")
    try:
        t = SIM_NOW - dt.timedelta(days=2)
        seq = await pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES (0, $1, 'agent.promoted', 'system', 'system', '{A06}', 'internal',
                    '{"from_tier":"secondary","to_tier":"star"}'::jsonb) RETURNING seq
            """, t)
        await pool.execute("UPDATE agents SET cognition_tier='star' WHERE id='A06'")
        await pool.execute(
            """
            INSERT INTO memories (agent_id, sim_time, kind, content, importance)
            VALUES ('A06', $1, 'reflection', '追赶反思', 7)
            """, t + dt.timedelta(hours=2))
        res = await run_item(pool, get_item("05"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert res.ok, res.detail
    finally:
        await close_pool(pool)
