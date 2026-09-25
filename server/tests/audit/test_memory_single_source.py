"""T-AUD-04 审计③ 双人记忆同源验收（08 文档 T-AUD-04；04 §10.1 ③ P2-2 修复版）。"""

from __future__ import annotations

import pytest

from tests.audit.conftest import SIM_NOW, _psql_file, close_pool, make_pool
from worldsim.audit.daily import get_item, run_item


@pytest.mark.asyncio
async def test_violation_sample_reports_red() -> None:
    """已知违规样例（同对话双落库 + A01 记忆双源指向）必报红。"""
    pool = await make_pool("worldsim_audit_03a")
    try:
        _psql_file(get_item("03").sample_path, "worldsim_audit_03a")
        res = await run_item(pool, get_item("03"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert not res.ok and res.detail[0]["agent_id"] == "A01"
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_single_source_passes() -> None:
    """正常双人事件单源（双方记忆同 seq）不误报。"""
    pool = await make_pool("worldsim_audit_03b")
    try:
        seq = await pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES (0, $1, 'dialogue.chat', 'agent:A01', 'autonomous', '{A01,A02}', 'public',
                    '{"participants":["A01","A02"],"mode":"small","topic_ids":[],"lines":[],"witnesses":[]}'::jsonb)
            RETURNING seq
            """, SIM_NOW)
        for aid in ("A01", "A02"):
            await pool.execute(
                """
                INSERT INTO memories (agent_id, sim_time, kind, content, importance, source_event_seq)
                VALUES ($1, $2, 'event', '各自视角', 5, $3)
                """, aid, SIM_NOW, seq)
        res = await run_item(pool, get_item("03"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert res.ok
    finally:
        await close_pool(pool)
