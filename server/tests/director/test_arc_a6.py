"""T-DIR-02 A6 低谷与反弹 弧线骨架验收（01 §6.2 表行 6；接挫败链 §3.3/裁员候选池 §1.5）。"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.director import _arc_support as S
from worldsim.time_engine.clock import LOCAL_TZ

_DB = "worldsim_dir_a6"
T1 = dt.datetime(2028, 3, 6, 8, 0, tzinfo=LOCAL_TZ)
T2 = dt.datetime(2028, 6, 8, 8, 0, tzinfo=LOCAL_TZ)
T3 = dt.datetime(2028, 9, 7, 8, 0, tzinfo=LOCAL_TZ)


@pytest.fixture(scope="module")
def dsn():
    d = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield d
    finally:
        _drop_db(_DB)


@pytest.mark.asyncio
async def test_main_path(dsn) -> None:
    """主路径：连续挫败（挫败值超阈）→情绪崩溃→援手 help 爆发→关系跃迁（逆袭节拍）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A6", now=T1)
    try:
        inst = await S.drive_main_path(eng, tpl, pool, clock)
        assert inst["via"] == "advance"
        assert tpl["payoff_beat"]["burst_event"] == "social.help"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fail_forward(dsn) -> None:
    """fail-forward：48h 无人援手 → 降级 S4（team_building/lucky 托底位，L0/L1 归 T-DIR-03 接线）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A6", now=T2)
    try:
        await S.set_frustration(pool, "A06", 0)
        await S.set_need(pool, "A06", "mood", 70.0)
        await S.fail_forward_case(eng, tpl, pool, clock, ff_at="S3", expect_stage="S4")
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_setup_days_window(dsn) -> None:
    """setup_days [7,21]（读配置）：<min 不爆发、>max 强制 fail-forward。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A6", now=T3)
    try:
        await S.set_frustration(pool, "A06", 0)
        await S.set_need(pool, "A06", "mood", 70.0)
        await S.setup_days_window_case(eng, tpl, pool, clock, penultimate="S3")
    finally:
        await pool.close()
