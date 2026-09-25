"""T-AUD-03 审计② 不在两地/瞬移验收（08 文档 T-AUD-03）。"""

from __future__ import annotations

import pytest

from tests.audit.conftest import SIM_NOW, _psql_file, close_pool, make_pool
from worldsim.audit.daily import get_item, run_item


@pytest.mark.asyncio
async def test_violation_sample_reports_red() -> None:
    """样例（连续两条 move 瞬移）必报红。"""
    pool = await make_pool("worldsim_audit_02a")
    try:
        _psql_file(get_item("02").sample_path, "worldsim_audit_02a")
        res = await run_item(pool, get_item("02"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert not res.ok and res.detail[0]["agent_id"] == "A03"
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_legal_moves_pass() -> None:
    """反向用例：合法连续移动（from == prev_to）不得误报（旧口径误判场景回归）。"""
    pool = await make_pool("worldsim_audit_02b")
    try:
        for i, (frm, to) in enumerate((("apt.L2.203", "apt.lobby"), ("apt.lobby", "corp.tech"))):
            await pool.execute(
                """
                INSERT INTO events (tick, sim_time, type, source, trigger, actors, location_id, visibility, payload)
                VALUES (0, $1, 'agent.move', 'agent:A01', 'autonomous', '{A01}', $2, 'public', $3::jsonb)
                """, SIM_NOW + __import__("datetime").timedelta(minutes=5 * i),
                to, __import__("json").dumps({"from": frm, "to": to, "sim_cost_min": 5}))
        res = await run_item(pool, get_item("02"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert res.ok
    finally:
        await close_pool(pool)
