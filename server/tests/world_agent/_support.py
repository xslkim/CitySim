"""tests/world_agent 共享支撑：FakeClock 鸭子时钟 + 引擎装配 + scratch 库工厂。

隔离惯例：events append-only 不可清库（同 tests/adjudicator/test_pipeline.py），
各测试模块用独立 scratch 库，跑完 drop。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import asyncpg

from worldsim.adjudicator.state_events import StateAggregator
from worldsim.world_agent.calendar import CalendarEngine
from worldsim.world_agent.config import load_world_config


class FakeClock:
    """鸭子类型时钟（TimeEngine 能力子集：now_sim/tick_of/current_tick）。"""

    def __init__(self, now: dt.datetime) -> None:
        self._now = now
        self._tick0 = now

    def set(self, now: dt.datetime) -> None:
        self._now = now

    def now_sim(self) -> dt.datetime:
        return self._now

    def tick_of(self, sim_time: dt.datetime) -> int:
        return int((sim_time - self._tick0).total_seconds() // 300)

    @property
    def current_tick(self) -> int:
        return self.tick_of(self._now)


async def make_engine(dsn: str, now: dt.datetime, *, with_agg: bool = True,
                      with_grader: bool = False) -> tuple[CalendarEngine, FakeClock, Any, Any]:
    """装配 (CalendarEngine, FakeClock, pool, agg)；agg=None 当 with_agg=False。"""
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    clock = FakeClock(now)
    agg = StateAggregator(pool) if with_agg else None
    grader = None
    if with_grader:
        from worldsim.adjudicator.grade import Grader

        grader = Grader(pool)
    cal = CalendarEngine(pool, load_world_config(), clock, agg=agg, grader=grader)
    return cal, clock, pool, agg


async def flush_agg(agg: Any, cal: CalendarEngine, sim_now: dt.datetime) -> None:
    """聚合器收尾（04 §6.5 每 tick flush 口径的测试等价物）。"""
    if agg is not None:
        await agg.flush(tick=cal.clock.tick_of(sim_now), sim_now=sim_now,
                        trigger="system", rng_seed=cal.clock.tick_of(sim_now))
