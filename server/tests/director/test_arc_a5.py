"""T-DIR-02 A5 谣言风暴 弧线骨架验收（01 §6.2 表行 5；gossip cites 链深度口径 01 §4.1）。"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.director import _arc_support as S
from worldsim.time_engine.clock import LOCAL_TZ

_DB = "worldsim_dir_a5"
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
    """主路径：一条 gossip→三手传播失真→当事人听闻（argue 爆发）→辟谣大会（吃瓜围观节拍）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A5", now=T1)
    try:
        inst = await S.drive_main_path(eng, tpl, pool, clock)
        assert inst["via"] == "advance"
        assert tpl["payoff_beat"]["type"] == "吃瓜围观"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fail_forward(dsn) -> None:
    """fail-forward：3 天未传回当事人 → 降级 S3（目击者复述送链入轨位，L1 归 T-DIR-03 接线）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A5", now=T2)
    try:
        await S.fail_forward_case(eng, tpl, pool, clock, ff_at="S2", expect_stage="S3")
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_setup_days_window(dsn) -> None:
    """setup_days [3,10]（读配置）：<min 不爆发、>max 强制 fail-forward。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A5", now=T3)
    try:
        await S.setup_days_window_case(eng, tpl, pool, clock, penultimate="S3")
    finally:
        await pool.close()
