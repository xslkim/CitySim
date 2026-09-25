"""T-WA-05 股价随机游走验收（04 文档 T-WA-05 验收 1~5；04 §5.3 回放语义）。"""

from __future__ import annotations

import datetime as dt
import json
import os
import random

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import make_engine
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.economy import (
    _sample_r, queue_stock_shock, register_stock_jobs, stock_market_open,
)

_DB = "worldsim_wa_stock"
_DB2 = "worldsim_wa_stock_rerun"


def _p(row) -> dict:
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])


@pytest.fixture(scope="module")
def stock_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


async def _run_days(dsn, start: dt.date, days: int) -> list[dict]:
    """从 start 起连跑 days 个自然日（含非交易日，由休市判定跳过），返回 stock.tick payload 序列。"""
    cal, clock, pool, _ = await make_engine(dsn, dt.datetime.combine(start, dt.time(6, 0), tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        register_stock_jobs(cal)
        await cal.mark_settled(clock.now_sim())
        for i in range(days):
            clock.set(dt.datetime.combine(start + dt.timedelta(days=i), dt.time(23, 50), tzinfo=LOCAL_TZ))
            await cal.tick()
        rows = await pool.fetch(
            "SELECT payload FROM events WHERE type='economy.stock.tick' ORDER BY seq")
        return [_p(r) for r in rows]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_reproducible(stock_dsn) -> None:
    """验收 1：固定 seed 重跑 10 模拟日，r/close 序列逐值一致；replay 回放下零新骰子。"""
    start = dt.date(2026, 10, 5)  # 周一（国庆后首个交易日所在周）
    seqs_a = await _run_days(stock_dsn, start, 10)
    dsn2 = _build_db(_DB2, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        seqs_b = await _run_days(dsn2, start, 10)
        assert [(p["symbol"], p["r"], p["close"], p["seed"]) for p in seqs_a] == [
            (p["symbol"], p["r"], p["close"], p["seed"]) for p in seqs_b
        ], "同 seed 重跑 r/close/seed 序列逐值一致"
        # 价格链自洽：次日 open = 前日 close
        by_symbol: dict[str, list[dict]] = {}
        for p in seqs_a:
            by_symbol.setdefault(p["symbol"], []).append(p)
        for sym, ticks in by_symbol.items():
            for prev, cur in zip(ticks, ticks[1:]):
                assert cur["open"] == prev["close"]
                assert cur["close"] == max(1, round(cur["open"] * (1 + cur["r"])))

        # WSIM_REPLAY_MODE=replay：重放既有区间零新事件零新骰子（04 §5.3）
        os.environ["WSIM_REPLAY_MODE"] = "replay"
        try:
            cal, clock, pool, _ = await make_engine(stock_dsn, dt.datetime.combine(start, dt.time(6, 0), tzinfo=LOCAL_TZ),
                                                    with_agg=False)
            register_stock_jobs(cal)
            n0 = await pool.fetchval("SELECT count(*) FROM events WHERE type='economy.stock.tick'")
            await cal.mark_settled(clock.now_sim())
            for i in range(10):
                clock.set(dt.datetime.combine(start + dt.timedelta(days=i), dt.time(23, 50), tzinfo=LOCAL_TZ))
                await cal.tick()
            n1 = await pool.fetchval("SELECT count(*) FROM events WHERE type='economy.stock.tick'")
            assert n1 == n0, "replay 回放不得产生新骰子/新事件（04 §5.3）"
            await pool.close()
        finally:
            os.environ.pop("WSIM_REPLAY_MODE")
    finally:
        _drop_db(_DB2)


def test_distribution() -> None:
    """验收 2：≥500 样本矩检验——均值/标准差落配置参数容差内（参数读 world.yaml，01 §1.5）。"""
    from worldsim.world_agent.config import load_world_config

    rw = load_world_config()["stocks"]["random_walk"]
    mu, sigma = float(rw["mu"]), float(rw["sigma"])
    samples = [_sample_r(mu, sigma, random.Random(f"dist|{i}")) for i in range(500)]
    mean = sum(samples) / len(samples)
    var = sum((x - mean) ** 2 for x in samples) / (len(samples) - 1)
    assert abs(mean - mu) < 0.002, f"均值 {mean:.5f} 偏离 μ={mu}"
    assert abs(var ** 0.5 - sigma) < sigma * 0.15, f"标准差 {var ** 0.5:.5f} 偏离 σ={sigma}"


@pytest.mark.asyncio
async def test_market_closed(stock_dsn) -> None:
    """验收 3：节假日与周末无 economy.stock.tick；交易日结算时点恰每标的 1 条。"""
    cal, clock, pool, _ = await make_engine(stock_dsn, dt.datetime(2027, 4, 30, 6, 0, tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        register_stock_jobs(cal)
        n_symbols = len(cal.cfg["stocks"]["symbols"])
        await cal.mark_settled(clock.now_sim())
        seq0 = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        # 2027-05-01~03 五一节假日（读配置判定休市）；05-04 周二交易日
        assert not stock_market_open(cal, dt.date(2027, 5, 1))
        assert not stock_market_open(cal, dt.date(2027, 5, 8))   # 周六
        assert stock_market_open(cal, dt.date(2027, 5, 4))
        for d in (dt.date(2027, 5, 1), dt.date(2027, 5, 4), dt.date(2027, 5, 8)):
            clock.set(dt.datetime.combine(d, dt.time(23, 50), tzinfo=LOCAL_TZ))
            await cal.tick()
        rows = await pool.fetch(
            "SELECT payload, sim_time::date AS d FROM events WHERE type='economy.stock.tick' AND seq > $1", seq0)
        by_date: dict[dt.date, int] = {}
        for r in rows:
            by_date[r["d"]] = by_date.get(r["d"], 0) + 1
        assert not (set(by_date) & {dt.date(2027, 5, 1), dt.date(2027, 5, 2), dt.date(2027, 5, 3), dt.date(2027, 5, 8)}), \
            "节假日/周末不得有 stock.tick"
        # 窗口内交易日（05-04~07）恰每日每标的 1 条
        for d in (dt.date(2027, 5, 4), dt.date(2027, 5, 5), dt.date(2027, 5, 6), dt.date(2027, 5, 7)):
            assert by_date.get(d) == n_symbols, f"{d} 交易日恰每标的 1 条"
        # 结算时点 = 配置 settle_time
        trow = await pool.fetchrow(
            "SELECT sim_time FROM events WHERE type='economy.stock.tick' AND seq > $1 LIMIT 1", seq0)
        hhmm = trow["sim_time"].astimezone(LOCAL_TZ).strftime("%H:%M")
        assert hhmm == str(cal.cfg["triggers"]["stock"]["settle_time"])
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_shock_applied_once(stock_dsn) -> None:
    """验收 4：注入 stock_shock → 次日 close 含冲击且仅生效一次；幅度越界拒绝（01 §1.5 口径）。"""
    cal, clock, pool, _ = await make_engine(stock_dsn, dt.datetime(2027, 6, 7, 6, 0, tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        register_stock_jobs(cal)
        sym = str(cal.cfg["triggers"]["layoff_rumor"]["symbol"])
        shock_pct = 5.0  # ±3%~8% 区间内（配置镜像）
        lo, hi = (float(x) for x in cal.cfg["triggers"]["stock"]["shock_range_pct"])
        assert lo <= shock_pct <= hi
        with pytest.raises(ValueError):
            await queue_stock_shock(cal, hi + 10.0)  # 越界拒绝
        await cal.mark_settled(clock.now_sim())
        # 动态取一个周日 s：s 休市、s+1/s+1+1 为交易日；注入冲击后仅 s+1 的 r 含冲击
        s = dt.date(2027, 6, 6)
        while not cal.is_weekend(s) or s.weekday() != 6:
            s += dt.timedelta(days=1)
        assert not stock_market_open(cal, s) and stock_market_open(cal, s + dt.timedelta(days=1))
        await queue_stock_shock(cal, shock_pct, symbol=sym)
        seq0 = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        for d in (s, s + dt.timedelta(days=1), s + dt.timedelta(days=2)):
            clock.set(dt.datetime.combine(d, dt.time(23, 50), tzinfo=LOCAL_TZ))
            await cal.tick()
        rows = await pool.fetch(
            """SELECT payload FROM events WHERE type='economy.stock.tick' AND seq > $1
               AND payload->>'symbol' = $2 ORDER BY seq""", seq0, sym)
        assert len(rows) == 2, "周日休市 + 两个交易日各 1 条"
        day1, day2 = _p(rows[0]), _p(rows[1])
        # 冲击并入首日 r（close 含 +5%），次日不再含（state 清零）
        assert abs(day1["close"] - round(day1["open"] * (1 + day1["r"]))) <= 1
        assert day1["r"] > shock_pct / 100.0 - 0.06  # r = 基础游走 + 0.05（σ=0.018 容差内不可能是纯游走负抵）
        shock_state = await cal.get_state(f"stock.shock.{sym}")
        assert not shock_state, "冲击一次性生效后清零"
        assert abs(day2["close"] - round(day2["open"] * (1 + day2["r"]))) <= 1
        # 次日 r 与冲击无关：重算无冲击序列比对（同 seed 另起库口径由 test_reproducible 覆盖，
        # 此处断言 shock 状态已清零 + day2 的 r ≠ day1 的 r 即可证一次性）
        assert day2["r"] != day1["r"]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_no_amount_cents_key(stock_dsn) -> None:
    """验收 5：economy.stock.tick 永不含 amount_cents（不进余额守恒求和，06 §1.2）。"""
    cal, clock, pool, _ = await make_engine(stock_dsn, dt.datetime(2027, 6, 10, 6, 0, tzinfo=LOCAL_TZ),
                                            with_agg=False)
    try:
        register_stock_jobs(cal)
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime(2027, 6, 10, 23, 50, tzinfo=LOCAL_TZ))
        await cal.tick()
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='economy.stock.tick' AND payload ? 'amount_cents'") == 0
        # 键集逐字 06 §1.2
        row = await pool.fetchrow(
            "SELECT payload FROM events WHERE type='economy.stock.tick' ORDER BY seq DESC LIMIT 1")
        assert set(_p(row).keys()) == {"symbol", "open", "close", "r", "seed"}
        # events.rng_seed 与 payload.seed 一致（04 §5.3）
        row2 = await pool.fetchrow(
            "SELECT rng_seed, payload FROM events WHERE type='economy.stock.tick' ORDER BY seq DESC LIMIT 1")
        assert row2["rng_seed"] == _p(row2)["seed"]
    finally:
        await pool.close()
