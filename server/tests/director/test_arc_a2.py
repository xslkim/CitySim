"""T-DIR-02 A2 职场竞聘对决 弧线骨架验收（01 §6.2 表行 2；接 T-WA-07 晋升窗口/绩效评审）。"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.director import _arc_support as S
from worldsim.time_engine.clock import LOCAL_TZ

_DB = "worldsim_dir_a2"
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
    """主路径：晋升窗口公告→备战（加班）→使坏→评审爆发→关系重构余波（打脸节拍）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A2", now=T1)
    try:
        inst = await S.drive_main_path(eng, tpl, pool, clock)
        assert inst["via"] == "advance"
        assert tpl["payoff_beat"]["burst_event"] == "world.perf_review"  # 06 §1.2 注册类型逐字
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fail_forward(dsn) -> None:
    """fail-forward：S4 评审阻塞 7 日 → 落选者迁怒收尾（degrade_to=done，天然接 A6）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A2", now=T2)
    try:
        await S.fail_forward_case(eng, tpl, pool, clock, ff_at="S4", expect_stage=None)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_setup_days_window(dsn) -> None:
    """setup_days [7,21]（读配置）：<min 不爆发、>max 强制 fail-forward。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A2", now=T3)
    try:
        assert [int(x) for x in tpl["payoff_beat"]["setup_days"]] == [
            int(tpl["min_days"]), int(tpl["max_days"])]  # 部署镜像一致性
        await S.setup_days_window_case(eng, tpl, pool, clock, penultimate="S4")
    finally:
        await pool.close()
