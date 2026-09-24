"""T-TIME-02 变速表加载/校验/段切换/热更验收。

口径：02 文档 T-TIME-02 验收 1~3（空洞拒绝/底线校验/段边界切换/热更延迟生效 +
test_ratio_switch_keeps_cognition_count）。数值校验归 01 T-CFG-01，本文件持加载/段切换/热更行为。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.time_engine.clock import LOCAL_TZ, TimeEngine
from worldsim.time_engine.speed_table import (
    Segment,
    SpeedTable,
    SpeedTableError,
    SpeedTableReloader,
    load,
)

asyncio_mark = pytest.mark.asyncio  # 仅 async 用例标注（避免 sync 用例 PytestWarning）

CONFIG_PATH = "config/speed_table.yaml"
T0 = dt.datetime(2026, 10, 12, 8, 0, 0, tzinfo=LOCAL_TZ)  # 周一 08:00（段边界为 08:00/12:00）


class FakeWall:
    def __init__(self, start: dt.datetime) -> None:
        self.t = start

    def now(self) -> dt.datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += dt.timedelta(**kw)


def _two_segment_table(ratio_a: float = 3.0, ratio_b: float = 1.0) -> SpeedTable:
    """08:00~12:00 ratio_a，12:00~24:00+00:00~08:00 ratio_b 的两段表。"""
    return SpeedTable(
        [
            Segment(0, 8 * 60, "continuous", ratio=ratio_b),
            Segment(8 * 60, 12 * 60, "continuous", ratio=ratio_a),
            Segment(12 * 60, 24 * 60, "continuous", ratio=ratio_b),
        ],
        {"min_sim_days_per_real_week": 1, "max_catchup_ratio": 6.0},
    )


async def _run_clock(
    dsn: str,
    wall: FakeWall,
    table: SpeedTable,
    target_sim: dt.datetime,
    ticks: list[int],
    reloader: SpeedTableReloader | None = None,
) -> TimeEngine:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    eng = await TimeEngine.start(pool, table, wall_now=wall.now)

    async def fake_sleep(seconds: float) -> None:
        wall.advance(seconds=max(seconds, 0.01))
        await asyncio.sleep(0)

    stop = asyncio.Event()
    try:
        await eng.run(
            on_tick=ticks.append,
            stop=stop,
            target_sim=target_sim,
            sleep=fake_sleep,
            reloader=reloader,
        )
    finally:
        await eng._persist_anchor()
    return eng


@pytest_asyncio.fixture(autouse=True)
async def _clean(test_db_dsn: str) -> None:
    conn = await asyncpg.connect(test_db_dsn)
    try:
        await conn.execute("DELETE FROM world_state WHERE key='clock.anchor'")
    finally:
        await conn.close()


# ---- 加载与校验（验收 1：空洞拒绝、底线校验） --------------------------------


def test_load_real_config_ok() -> None:
    """验收 3 同口径：既有 config/speed_table.yaml 加载退出码 0 路径。"""
    table = load(CONFIG_PATH)
    assert table.max_catchup_ratio > 0
    assert table.sim_hours_per_real_day() > 0


def test_gap_rejected() -> None:
    with pytest.raises(SpeedTableError, match="空洞|重叠"):
        SpeedTable(
            [
                Segment(0, 8 * 60, "continuous", ratio=2.0),
                Segment(9 * 60, 24 * 60, "continuous", ratio=1.0),  # 08:00~09:00 空洞
            ],
            {"min_sim_days_per_real_week": 1, "max_catchup_ratio": 6.0},
        )
    with pytest.raises(SpeedTableError, match="24:00"):
        SpeedTable(
            [Segment(0, 23 * 60, "continuous", ratio=2.0)],
            {"min_sim_days_per_real_week": 1, "max_catchup_ratio": 6.0},
        )


def test_floor_constraint_rejected() -> None:
    """底线校验：模拟日/真实周不足 min_sim_days_per_real_week → 拒绝。"""
    with pytest.raises(SpeedTableError, match="底线"):
        SpeedTable(
            [Segment(0, 24 * 60, "continuous", ratio=0.1)],  # 2.4 sim h/day → 0.7 日/周
            {"min_sim_days_per_real_week": 8, "max_catchup_ratio": 6.0},
        )


def test_batch_segment_requires_hours() -> None:
    with pytest.raises(SpeedTableError, match="sim_hours_per_run"):
        SpeedTable(
            [Segment(0, 24 * 60, "batch")],
            {"min_sim_days_per_real_week": 1, "max_catchup_ratio": 6.0},
        )


# ---- 段边界切换（验收 1） -----------------------------------------------------


@asyncio_mark
async def test_segment_boundary_switch(test_db_dsn: str) -> None:
    """跨段边界触发 SegmentSwitch：12:00 ratio 3.0→1.0，换比瞬间 sim 连续。"""
    wall = FakeWall(T0)
    table = _two_segment_table()
    ticks: list[int] = []
    # 08:00→12:00 走 4 wall h × 3.0 = 12 sim h，再 +2 sim h（1.0 档 2 wall h）→ wall 14:00
    target = T0 + dt.timedelta(hours=14)
    eng = await _run_clock(test_db_dsn, wall, table, target, ticks)
    assert eng.ratio == 1.0, "跨过 12:00 段边界后应切到黄金档 ratio"
    assert ticks, "应产出 tick"
    assert eng.now_sim() >= target
    # sim 连续性：全部 tick 的 sim 网格严格 +5min 递增（无跳变无回退）
    sims = [eng.sim_of_tick(t) for t in ticks]
    for prev, nxt in zip(sims, sims[1:]):
        assert (nxt - prev) == dt.timedelta(minutes=5)
    await eng._pool.close()


@asyncio_mark
async def test_ratio_switch_keeps_cognition_count(test_db_dsn: str) -> None:
    """验收 2 指定用例：同一模拟日 ratio 切换前后，排程回调次数一致（04 §3.1 压缩比只调观看节奏）。"""
    # 场景 A：全天恒 3.0；场景 B：同模拟日中途 3.0→1.0。两侧各跑满 1 模拟日。
    ticks_a: list[int] = []
    wall_a = FakeWall(T0)
    eng_a = await _run_clock(test_db_dsn, wall_a, _two_segment_table(3.0, 3.0), T0 + dt.timedelta(days=1), ticks_a)
    count_a = len(ticks_a)
    await eng_a._pool.close()

    conn = await asyncpg.connect(test_db_dsn)
    try:
        await conn.execute("DELETE FROM world_state WHERE key='clock.anchor'")
    finally:
        await conn.close()

    ticks_b: list[int] = []
    wall_b = FakeWall(T0)
    eng_b = await _run_clock(test_db_dsn, wall_b, _two_segment_table(3.0, 1.0), T0 + dt.timedelta(days=1), ticks_b)
    count_b = len(ticks_b)
    await eng_b._pool.close()

    assert count_a == count_b == 24 * 12, "tick 排程锚定 sim_time：1 模拟日恒为 288 tick，与压缩比无关"


# ---- SIGHUP 热更延迟生效（验收 1） -------------------------------------------


@asyncio_mark
async def test_sighup_reload_delayed_to_boundary(test_db_dsn: str, tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    """热更挂起后不立即生效，下一个段边界生效；非法配置保留旧配置并 WARN（04 §12.4）。"""
    cfg = {
        "segments": [
            {"start": "00:00", "end": "08:00", "mode": "continuous", "ratio": 1.0},
            {"start": "08:00", "end": "12:00", "mode": "continuous", "ratio": 3.0},
            {"start": "12:00", "end": "24:00", "mode": "continuous", "ratio": 1.0},
        ],
        "constraints": {"min_sim_days_per_real_week": 1, "max_catchup_ratio": 6.0},
    }
    path = tmp_path / "speed_table.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    reloader = SpeedTableReloader(path)
    wall = FakeWall(T0)

    # 改 ratio 3.0→4.0 并挂起；未过段边界前不得生效
    cfg["segments"][1]["ratio"] = 4.0
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    reloader.request_reload()
    assert reloader.pending and reloader.current.segments[1].ratio == 3.0

    ticks: list[int] = []
    target = T0 + dt.timedelta(hours=14)  # 越过 12:00 段边界（4 wall h × 3.0 = 12 sim h 后再走 2 sim h）
    eng = await _run_clock(test_db_dsn, wall, reloader.current, target, ticks, reloader=reloader)
    assert reloader.current.segments[1].ratio == 4.0, "段边界后热更应生效"
    await eng._pool.close()

    # 非法配置：改出空洞 → 保留旧配置 + WARN
    conn = await asyncpg.connect(test_db_dsn)
    try:
        await conn.execute("DELETE FROM world_state WHERE key='clock.anchor'")
    finally:
        await conn.close()
    cfg["segments"][1]["start"] = "09:00"  # 制造空洞
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    reloader.request_reload()
    with caplog.at_level(logging.WARNING):
        wall2 = FakeWall(T0)
        ticks2: list[int] = []
        # ratio 4.0 下需 >16 sim h 才能越过 12:00 段边界
        eng2 = await _run_clock(test_db_dsn, wall2, reloader.current, T0 + dt.timedelta(hours=18), ticks2, reloader=reloader)
    assert reloader.current.segments[1].ratio == 4.0, "校验失败必须保留旧配置"
    assert any("保留旧配置" in rec.message for rec in caplog.records if rec.levelno >= logging.WARNING)
    await eng2._pool.close()
