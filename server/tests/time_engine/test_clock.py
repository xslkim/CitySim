"""T-TIME-01 时钟锚点换算与暂停/恢复验收。

口径：02 文档 T-TIME-01 验收 1~3（换算精度/跨段积分/暂停不流动/重启恢复四用例 +
锚点落库三键 SQL 断言）。全部用 fake wall clock 注入，确定性。
"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio

from worldsim.time_engine.clock import LOCAL_TZ, TICK_SIM_SECONDS, TimeEngine
from worldsim.time_engine.speed_table import Segment, SpeedTable

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 8, 0, 0, tzinfo=LOCAL_TZ)  # 叙事起点（周一早八）


class FakeWall:
    def __init__(self, start: dt.datetime) -> None:
        self.t = start

    def now(self) -> dt.datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += dt.timedelta(**kw)


def _table(ratio: float = 2.0, max_catchup: float = 6.0) -> SpeedTable:
    return SpeedTable(
        [Segment(0, 24 * 60, "continuous", ratio=ratio)],
        {"min_sim_days_per_real_week": 1, "max_catchup_ratio": max_catchup},
    )


async def _engine(dsn: str, wall: FakeWall, table: SpeedTable | None = None) -> TimeEngine:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    eng = await TimeEngine.start(pool, table or _table(), wall_now=wall.now)
    return eng


async def _reset_anchor(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        # events append-only（触发器对 owner 亦生效），只能清 world_state；事件断言用基线 seq 过滤
        await conn.execute("DELETE FROM world_state WHERE key='clock.anchor'")
    finally:
        await conn.close()


async def _events_after(dsn_or_pool: object, baseline: int, types: tuple[str, ...] | None = None) -> list:
    sql = "SELECT type, source, trigger, visibility, payload FROM events WHERE seq > $1"
    args: list = [baseline]
    if types:
        sql += " AND type = ANY($2)"
        args.append(list(types))
    sql += " ORDER BY seq"
    if hasattr(dsn_or_pool, "fetch"):
        return await dsn_or_pool.fetch(sql, *args)  # type: ignore[union-attr]
    conn = await asyncpg.connect(dsn_or_pool)  # type: ignore[arg-type]
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


@pytest_asyncio.fixture(autouse=True)
async def _clean(test_db_dsn: str) -> None:
    await _reset_anchor(test_db_dsn)


async def test_conversion_precision(test_db_dsn: str) -> None:
    """换算精度：sim = anchor_sim + (wall − anchor_wall) × ratio，秒级精确。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall, _table(ratio=2.0))
    sim0 = eng.now_sim()
    assert sim0 == T0
    wall.advance(seconds=90)
    assert (eng.now_sim() - sim0) == dt.timedelta(seconds=180)
    wall.advance(minutes=7, seconds=30)
    assert (eng.now_sim() - sim0) == dt.timedelta(seconds=(90 + 450) * 2)
    # tick_of：tick = 模拟 5 分钟，网格对齐
    assert eng.tick_of(sim0) == 0
    assert eng.tick_of(sim0 + dt.timedelta(seconds=TICK_SIM_SECONDS * 7)) == 7
    assert eng.sim_of_tick(7) == sim0 + dt.timedelta(seconds=TICK_SIM_SECONDS * 7)
    await eng._pool.close()


async def test_cross_segment_integration(test_db_dsn: str) -> None:
    """跨段积分：换比前旧段先积分进 anchor_sim，sim 连续不跳变。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall, _table(ratio=3.0))
    sim0 = eng.now_sim()
    wall.advance(minutes=10)          # 10 wall min × 3.0 = 30 sim min
    sim_before = eng.now_sim()
    assert (sim_before - sim0) == dt.timedelta(minutes=30)
    await eng.set_ratio(1.0)          # 跨段换比（黄金档 1:1）
    assert eng.now_sim() == sim_before, "换比瞬间 sim 不得跳变"
    wall.advance(minutes=10)          # 10 wall min × 1.0 = 10 sim min
    assert (eng.now_sim() - sim0) == dt.timedelta(minutes=40)
    await eng._pool.close()


async def test_pause_freezes_sim_time(test_db_dsn: str) -> None:
    """验收 2 指定用例：pause 期间 now_sim() 差值为 0；恢复后从冻结点继续流动。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall, _table(ratio=2.0))
    baseline = await eng._pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    wall.advance(minutes=5)
    frozen = eng.now_sim()
    await eng.pause("db_down")
    wall.advance(hours=1)
    assert (eng.now_sim() - frozen) == dt.timedelta(0), "暂停期间模拟时间必须不流动"
    # 暂停态落库
    row = await eng._pool.fetchrow("SELECT value FROM world_state WHERE key='clock.anchor'")
    value = json.loads(row["value"]) if isinstance(row["value"], str) else dict(row["value"])
    assert "paused_at" in value
    downtime, _planned = await eng.resume("db_recovered")
    assert downtime == dt.timedelta(hours=1)
    assert eng.now_sim() == frozen, "恢复瞬间 anchor_sim 不变"
    wall.advance(minutes=5)
    assert (eng.now_sim() - frozen) == dt.timedelta(minutes=10)
    # time.paused/resumed 事件留痕（T-TIME-03 五事件之二，source/trigger=system）
    rows = await _events_after(eng._pool, baseline, ("time.paused", "time.resumed"))
    assert [r["type"] for r in rows] == ["time.paused", "time.resumed"]
    assert all(r["source"] == "system" and r["trigger"] == "system" for r in rows)
    payload = json.loads(rows[0]["payload"]) if isinstance(rows[0]["payload"], str) else dict(rows[0]["payload"])
    assert set(payload.keys()) == {"reason", "at_sim"}  # 06 §1.2 逐字
    await eng._pool.close()


async def test_restart_recovery(test_db_dsn: str) -> None:
    """进程重启从 world_state 锚点恢复：新实例 now_sim 与锚点数学一致（04 §12.2）。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall, _table(ratio=3.0))
    wall.advance(minutes=20)
    await eng.set_ratio(3.0)  # 积分落库
    expect_at = eng.now_sim()
    await eng._pool.close()
    # 模拟重启：同一 DB、同一 fake wall 建新引擎
    wall.advance(minutes=10)
    eng2 = await _engine(test_db_dsn, wall, _table(ratio=3.0))
    assert eng2.now_sim() == expect_at + dt.timedelta(minutes=30), "重启后 sim 从锚点连续推进"
    assert eng2.tick_of(eng2.now_sim()) == eng.tick_of(expect_at) + 6  # 30 sim min / 5 min/tick
    await eng2._pool.close()


async def test_anchor_persisted_three_keys(test_db_dsn: str) -> None:
    """验收 3 SQL 口径：world_state clock.anchor 含 anchor_sim/anchor_wall/ratio 三键。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall)
    row = await eng._pool.fetchrow("SELECT value FROM world_state WHERE key='clock.anchor'")
    value = json.loads(row["value"]) if isinstance(row["value"], str) else dict(row["value"])
    assert {"anchor_sim", "anchor_wall", "ratio"} <= set(value.keys())
    await eng._pool.close()


async def test_first_boot_reanchor_seed_placeholder(test_db_dsn: str) -> None:
    """seed 冷启动占位锚点（updated_tick=0）首启重锚：保留叙事起点 anchor_sim，锚定当前真实时刻。"""
    narrative = dt.datetime(2026, 10, 12, 0, 0, 0, tzinfo=LOCAL_TZ)
    conn = await asyncpg.connect(test_db_dsn)
    try:
        await conn.execute(
            "INSERT INTO world_state (key, value, updated_tick) VALUES ('clock.anchor', $1::jsonb, 0)",
            json.dumps({"anchor_sim": narrative.isoformat(), "anchor_wall": narrative.isoformat(), "ratio": 1}),
        )
    finally:
        await conn.close()
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall, _table(ratio=2.0))
    row = await eng._pool.fetchrow("SELECT value, updated_tick FROM world_state WHERE key='clock.anchor'")
    value = json.loads(row["value"]) if isinstance(row["value"], str) else dict(row["value"])
    assert value["anchor_sim"] == narrative.isoformat(), "重锚保留叙事起点"
    assert value["anchor_wall"] == T0.isoformat(), "重锚锚定当前真实时刻"
    assert row["updated_tick"] >= 0
    assert eng.now_sim() == narrative
    wall.advance(minutes=5)
    assert (eng.now_sim() - narrative) == dt.timedelta(minutes=10)
    await eng._pool.close()
