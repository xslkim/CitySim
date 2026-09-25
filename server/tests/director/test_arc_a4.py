"""T-DIR-02 A4 债务追讨 弧线骨架验收（01 §6.2 表行 4；接 debts 状态与 social.repay_money）。"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.director import _arc_support as S
from worldsim.time_engine.clock import LOCAL_TZ

_DB = "worldsim_dir_a4"
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
    """主路径：债主资金紧张→委婉暗示→公开催讨→还钱爆发（真相大白；burst=social.repay_money）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A4", now=T1)
    try:
        inst = await S.drive_main_path(eng, tpl, pool, clock)
        assert inst["via"] == "advance"
        assert tpl["payoff_beat"]["burst_event"] == "social.repay_money"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fail_forward(dsn) -> None:
    """fail-forward：欠款人真没钱 → S4 阻塞 5 日降级收尾（以工抵债/分期剧情位，关系重构非断裂）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A4", now=T2)
    try:
        await S.set_need(pool, "A01", "wealth", 70.0)  # 重置主路径注入的财富低位
        await S.fail_forward_case(eng, tpl, pool, clock, ff_at="S4", expect_stage=None)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_setup_days_window(dsn) -> None:
    """setup_days [5,14]（读配置）：<min 不爆发、>max 强制 fail-forward。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A4", now=T3)
    try:
        await S.set_need(pool, "A01", "wealth", 70.0)
        await S.setup_days_window_case(eng, tpl, pool, clock, penultimate="S3")
    finally:
        await pool.close()
