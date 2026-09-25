"""T-DIR-02 A3 前任重逢 弧线骨架验收（01 §6.2 表行 3）。"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.director import _arc_support as S
from worldsim.time_engine.clock import LOCAL_TZ

_DB = "worldsim_dir_a3"
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
    """主路径：偶遇激化（tension 淤积）→第三方介入→旧账重提 argue→和解 apologize 爆发（误会解除）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A3", now=T1)
    try:
        inst = await S.drive_main_path(eng, tpl, pool, clock)
        assert inst["via"] == "advance"
        assert tpl["payoff_beat"]["type"] == "误会解除"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fail_forward(dsn) -> None:
    """fail-forward：双方回避 7 天 → 软收尾（degrade_to=done；L1 偶遇排期归 T-DIR-03 生产接线）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A3", now=T2)
    try:
        await S.reset_relation(pool, "A03", "A04", -10, 35)  # seed 初态（主路径曾推高 tension）
        await S.fail_forward_case(eng, tpl, pool, clock, ff_at="S1", expect_stage=None)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_setup_days_window(dsn) -> None:
    """setup_days [4,14]（读配置）：<min 不爆发、>max 强制 fail-forward。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A3", now=T3)
    try:
        await S.reset_relation(pool, "A03", "A04", -10, 35)
        await S.setup_days_window_case(eng, tpl, pool, clock, penultimate="S3")
    finally:
        await pool.close()
