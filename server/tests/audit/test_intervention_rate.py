"""T-AUD-05 审计④ 干预率验收（08 文档 T-AUD-05 验收 1~2；00 §4 红线 13 只看 trigger）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.audit.conftest import SIM_NOW, _psql_file, close_pool, make_pool
from worldsim.audit.daily import get_item, load_intervention_cap, run_item

AUDIT_SRC = Path(__file__).resolve().parents[2] / "worldsim" / "audit"


@pytest.mark.asyncio
async def test_violation_sample_reports_red() -> None:
    """样例（10 director + 10 autonomous）干预率 0.5 > 上限 → 必报红。"""
    pool = await make_pool("worldsim_audit_04a")
    try:
        _psql_file(get_item("04").sample_path, "worldsim_audit_04a")
        from tests.audit.conftest import anchor_now
        now = await anchor_now(pool)  # 样例 sim_time=锚点，审计 sim_now 须同窗（04 §10.1 ④ 7 日滑窗）
        res = await run_item(pool, get_item("04"), sim_now=now, sim_day=now.date())
        assert not res.ok
        assert res.detail[0]["rate"] > load_intervention_cap()
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_director_typed_world_events_counted() -> None:
    """director 发起的 world.* 事件计入分子（06 §1.1）；同量超线即红。"""
    pool = await make_pool("worldsim_audit_04b")
    try:
        for _ in range(10):
            await pool.execute(
                """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
                   VALUES (0, $1, 'world.disturb.complaint', 'world', 'director', 'internal',
                           '{"floor":1,"issue":"噪音"}'::jsonb)""", SIM_NOW)
        for _ in range(10):
            await pool.execute(
                """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
                   VALUES (0, $1, 'agent.think', 'agent:A01', 'autonomous', 'internal',
                           '{"topic_hint":"x"}'::jsonb)""", SIM_NOW)
        res = await run_item(pool, get_item("04"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert not res.ok, "director 发起的 world.* 计入干预率（红线 13）"
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_world_trigger_not_counted() -> None:
    """对照组：同样事件改 trigger='world' → 不计入、不报红（红线 13 回归）。"""
    pool = await make_pool("worldsim_audit_04c")
    try:
        for _ in range(10):
            await pool.execute(
                """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
                   VALUES (0, $1, 'world.disturb.complaint', 'world', 'world', 'internal',
                           '{"floor":1,"issue":"噪音"}'::jsonb)""", SIM_NOW)
        for _ in range(10):
            await pool.execute(
                """INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
                   VALUES (0, $1, 'agent.think', 'agent:A01', 'autonomous', 'internal',
                           '{"topic_hint":"x"}'::jsonb)""", SIM_NOW)
        res = await run_item(pool, get_item("04"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert res.ok
    finally:
        await close_pool(pool)


def test_no_literal_threshold_in_audit_code() -> None:
    """验收 2：阈值不硬编码（阈值唯一载体 = models.yaml thresholds 段）。"""
    cap = load_intervention_cap()
    lit = f"{cap:.2f}".rstrip("0").rstrip(".")
    for f in AUDIT_SRC.rglob("*.py"):
        assert not re.search(rf"\b0\.15\b", f.read_text(encoding="utf-8")), f"{f.name} 含字面阈值"
    assert lit  # 防空串误绿
