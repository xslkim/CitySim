"""T-WA-06 裁员传闻规则触发验收（04 文档 T-WA-06 验收 1~4）。"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import flush_agg, make_engine
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.calendar import register_layoff_rumor_job

_DB = "worldsim_wa_rumor"


def _p(row) -> dict:
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])


@pytest.fixture(scope="module")
def rumor_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


async def _fake_stock_tick(cal, day: dt.date, r: float) -> None:
    """构造确定性 stock.tick（绕过随机游走，直接钉 r 值做阈值边界两侧）。"""
    fire = dt.datetime.combine(day, dt.time(9, 30), tzinfo=LOCAL_TZ)
    await cal.insert_event(
        type_="economy.stock.tick",
        payload={"symbol": str(cal.cfg["triggers"]["layoff_rumor"]["symbol"]),
                 "open": 1800, "close": max(1, round(1800 * (1 + r))), "r": r,
                 "seed": cal.clock.tick_of(fire)},
        sim_time=fire, rng_seed=cal.clock.tick_of(fire))


@pytest.mark.asyncio
async def test_trigger_threshold(rumor_dsn) -> None:
    """验收 1：r 在阈值两侧（阈值读配置）——过线 → 次日触发时点恰一条；未过线 → 零条。"""
    cal, clock, pool, agg = await make_engine(rumor_dsn, dt.datetime(2026, 10, 12, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_layoff_rumor_job(cal)
        cfg = cal.cfg["triggers"]["layoff_rumor"]
        th = float(cfg["drop_pct_threshold"])
        hh, mm = (int(x) for x in str(cfg["trigger_time"]).split(":"))
        # 未过线：跌 (th-1)%
        await _fake_stock_tick(cal, dt.date(2026, 10, 12), -(th - 1.0) / 100.0)
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime(2026, 10, 13, 23, 50, tzinfo=LOCAL_TZ))
        await cal.tick()
        assert await pool.fetchval("SELECT count(*) FROM events WHERE type='world.layoff_rumor'") == 0
        # 过线：跌 (th+2)%
        await _fake_stock_tick(cal, dt.date(2026, 10, 13), -(th + 2.0) / 100.0)
        clock.set(dt.datetime(2026, 10, 14, 23, 50, tzinfo=LOCAL_TZ))
        await cal.tick()
        rows = await pool.fetch(
            "SELECT payload, sim_time, source, trigger FROM events WHERE type='world.layoff_rumor'")
        assert len(rows) == 1, "过线次日恰一条"
        p = _p(rows[0])
        assert set(p.keys()) >= {"drop_pct", "scope"}
        assert p["drop_pct"] == pytest.approx(th + 2.0, abs=0.01)
        assert p["scope"] == str(cfg["scope"])
        t = rows[0]["sim_time"].astimezone(LOCAL_TZ)
        assert (t.hour, t.minute) == (hh, mm), "触发时点读配置（次日 9:30 口径）"
        assert t.date() == dt.date(2026, 10, 14)
        assert (rows[0]["source"], rows[0]["trigger"]) == ("world", "world")
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_not_intervention(rumor_dsn) -> None:
    """验收 2：规则触发不占干预率——trigger='director' 的 layoff_rumor 恒零（04 §10.1 ④ 分子外）。"""
    cal, clock, pool, agg = await make_engine(rumor_dsn, dt.datetime(2026, 11, 16, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_layoff_rumor_job(cal)
        cfg = cal.cfg["triggers"]["layoff_rumor"]
        await _fake_stock_tick(cal, dt.date(2026, 11, 16), -(float(cfg["drop_pct_threshold"]) + 3.0) / 100.0)
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime(2026, 11, 17, 23, 50, tzinfo=LOCAL_TZ))
        await cal.tick()
        await flush_agg(agg, cal, clock.now_sim())
        assert await pool.fetchval("SELECT count(*) FROM events WHERE type='world.layoff_rumor'") >= 1
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='world.layoff_rumor' AND trigger='director'") == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_effects(rumor_dsn) -> None:
    """验收 3：成就需求全员变更落 state.needs_delta（cause=传闻 seq）；gossip 权重标记窗口正确。"""
    cal, clock, pool, agg = await make_engine(rumor_dsn, dt.datetime(2026, 12, 14, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_layoff_rumor_job(cal)
        cfg = cal.cfg["triggers"]["layoff_rumor"]
        await cal.set_state("layoff_rumor.consumed", None)  # 清消费标记（模块内前序用例隔离）
        await pool.execute("DELETE FROM world_state WHERE key='layoff_rumor.consumed'")
        await _fake_stock_tick(cal, dt.date(2026, 12, 14), -(float(cfg["drop_pct_threshold"]) + 1.5) / 100.0)
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime(2026, 12, 15, 23, 50, tzinfo=LOCAL_TZ))
        await cal.tick()
        await flush_agg(agg, cal, clock.now_sim())
        seq = await pool.fetchval(
            "SELECT seq FROM events WHERE type='world.layoff_rumor' ORDER BY seq DESC LIMIT 1")
        assert seq is not None
        # 全员成就变更、cause 链接（04 §6.5/06 §2）
        rows = await pool.fetch("SELECT payload FROM events WHERE type='state.needs_delta'")
        linked = [
            c for r in rows for c in _p(r)["changes"]
            if c["cause"] == str(seq) and c["need"] == "achievement"
        ]
        n_agents = await pool.fetchval("SELECT count(*) FROM agents")
        assert len(linked) == n_agents
        assert all(c["delta"] == float(cfg["achievement_delta"]) for c in linked)
        # gossip 权重标记：倍率与窗口读配置
        boost = await cal.get_state("layoff_rumor.gossip_boost")
        assert boost["multiplier"] == float(cfg["gossip_weight_multiplier"])
        assert boost["until"] == (dt.date(2026, 12, 15) + dt.timedelta(days=int(cfg["gossip_window_days"]))).isoformat()
        assert boost["for_event"] == str(seq)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_daily_judgement(rumor_dsn) -> None:
    """验收 4：连续两日过线各产一条（无冷却为按字面规则的工程口径，D-08 已登记）。"""
    cal, clock, pool, agg = await make_engine(rumor_dsn, dt.datetime(2027, 1, 18, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_layoff_rumor_job(cal)
        cfg = cal.cfg["triggers"]["layoff_rumor"]
        # 消费标记对齐到本用例起点前最后一笔 tick（防止前序用例的过线 tick 在本窗口重复触发）
        last_tick_seq = await pool.fetchval(
            "SELECT coalesce(max(seq), 0) FROM events WHERE type='economy.stock.tick'")
        await cal.set_state("layoff_rumor.consumed", int(last_tick_seq))
        await cal.mark_settled(clock.now_sim())
        seq0 = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        for i, day in enumerate((dt.date(2027, 1, 18), dt.date(2027, 1, 19))):
            await _fake_stock_tick(cal, day, -(float(cfg["drop_pct_threshold"]) + 1.0 + i) / 100.0)
            clock.set(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(23, 50), tzinfo=LOCAL_TZ))
            await cal.tick()
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='world.layoff_rumor' AND seq > $1", seq0) == 2
    finally:
        await pool.close()
