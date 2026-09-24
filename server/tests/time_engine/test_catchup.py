"""T-TIME-03 故障追平与凌晨 batch 段推进验收。

口径：02 文档 T-TIME-03 验收 1~4（<2h 连跑档/≥2h batch 档/暂停期间队列行为 +
test_batch_advance_event + test_batch_hook_registry）。全部 fake wall 注入，确定性。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio

from worldsim.time_engine.batch import CHUNK_SIZE, batch_advance
from worldsim.time_engine.batch_hooks import (
    clear_batch_hooks,
    register_batch_hook,
    registered_hooks,
)
from worldsim.time_engine.clock import LOCAL_TZ, TimeEngine
from worldsim.time_engine.speed_table import Segment, SpeedTable

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 8, 0, 0, tzinfo=LOCAL_TZ)


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


@pytest_asyncio.fixture(autouse=True)
async def _clean(test_db_dsn: str) -> None:
    clear_batch_hooks()
    conn = await asyncpg.connect(test_db_dsn)
    try:
        await conn.execute("DELETE FROM world_state WHERE key='clock.anchor'")
    finally:
        await conn.close()


async def _engine(dsn: str, wall: FakeWall, table: SpeedTable | None = None) -> TimeEngine:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    return await TimeEngine.start(pool, table or _table(), wall_now=wall.now)


def _payload(row) -> dict:
    raw = row["payload"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def _events(pool, baseline: int) -> list:
    return await pool.fetch(
        "SELECT type, source, trigger, visibility, payload FROM events WHERE seq > $1 ORDER BY seq", baseline
    )


async def test_catchup_continuous_gear_under_2h(test_db_dsn: str) -> None:
    """停机 <2 真实小时：min(ratio×2, max_catchup_ratio) 连跑回到计划位置，catchup.start/end 落库。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall, _table(ratio=2.0, max_catchup=6.0))
    baseline = await eng._pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    wall.advance(minutes=30)  # 跑 1 sim h
    frozen = eng.now_sim()
    await eng.pause("llm_all_broken")
    wall.advance(hours=1)     # 停机 1 真实小时（<2h → 连跑档）
    downtime, planned = await eng.resume("llm_recovered")
    assert downtime == dt.timedelta(hours=1)
    assert planned == frozen + dt.timedelta(hours=2)  # 停机期 ratio 2.0 应推进 2 sim h

    plan = eng.plan_catchup(downtime, planned)
    assert plan.mode == "continuous"
    assert plan.ratio == min(2.0 * 2, 6.0) == 4.0

    async def drive(p) -> None:  # 主循环驱动追平（测试以 fake wall 模拟 tick 推进）
        while eng.now_sim() < p.to_sim:
            wall.advance(minutes=5)
            await asyncio.sleep(0)

    await eng.run_catchup(plan, drive=drive)
    assert eng.now_sim() >= planned
    assert eng.ratio == 2.0, "追平结束后恢复正常段压缩比"
    rows = await _events(eng._pool, baseline)
    types = [r["type"] for r in rows]
    assert types == ["time.paused", "time.resumed", "time.catchup.start", "time.catchup.end"]
    start_payload, end_payload = _payload(rows[2]), _payload(rows[3])
    assert set(start_payload.keys()) == {"mode", "from_sim", "to_sim"}  # 06 §1.2 逐字
    assert start_payload["mode"] == "continuous" and end_payload["mode"] == "continuous"
    assert all(r["source"] == "system" and r["trigger"] == "system" for r in rows)
    assert all(r["visibility"] == "internal" for r in rows)  # time 域 internal（注册表注释）
    await eng._pool.close()


async def test_catchup_batch_gear_over_2h(test_db_dsn: str) -> None:
    """停机 ≥2 真实小时：落后部分按 batch 模式补齐（逐 agent 摘要快进），不追赶黄金档 1:1。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall, _table(ratio=2.0, max_catchup=6.0))
    baseline = await eng._pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    frozen = eng.now_sim()
    await eng.pause("disk_full")
    wall.advance(hours=3)     # 停机 3 真实小时（≥2h → batch 档）
    downtime, planned = await eng.resume("disk_cleared")
    assert planned == frozen + dt.timedelta(hours=6)

    plan = eng.plan_catchup(downtime, planned)
    assert plan.mode == "batch"
    assert plan.sim_hours == pytest.approx(6.0)

    summarized: list[tuple[str, float]] = []

    async def summarize(agent_id: str, sim_hours: float) -> None:
        summarized.append((agent_id, sim_hours))
        await asyncio.sleep(0)

    await eng.run_catchup(plan, agent_ids=("A01", "A02", "A03"), summarize=summarize)
    assert sorted(a for a, _ in summarized) == ["A01", "A02", "A03"]
    assert all(h == pytest.approx(6.0) for _, h in summarized)
    assert eng.now_sim() >= planned - dt.timedelta(seconds=1), "batch 补齐后回到计划位置"
    rows = await _events(eng._pool, baseline)
    types = [r["type"] for r in rows]
    assert types == [
        "time.paused", "time.resumed", "time.catchup.start", "time.batch_advanced", "time.catchup.end",
    ]
    assert _payload(rows[2])["mode"] == "batch" and _payload(rows[4])["mode"] == "batch"
    await eng._pool.close()


async def test_pause_queue_behavior(test_db_dsn: str) -> None:
    """暂停期间：不排程新 tick（队列无新入队）；已在飞行中的完成调用正常落库；恢复后续跑。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall, _table(ratio=2.0))
    ticks: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        wall.advance(seconds=seconds)
        await asyncio.sleep(0)

    stop = asyncio.Event()
    run_task = asyncio.create_task(eng.run(on_tick=ticks.append, stop=stop, sleep=fake_sleep))
    try:
        while len(ticks) < 2:
            await asyncio.sleep(0.01)
        await eng.pause("db_down")
        count_at_pause = len(ticks)  # pause() 返回后不得再排程新 tick（04 §3.2 step1）
        await asyncio.sleep(0.2)
        wall.advance(minutes=30)
        await asyncio.sleep(0.2)
        assert len(ticks) == count_at_pause, "暂停期间不得排程新 tick（未发起的取消）"
        # 队列内已完成 LLM 调用正常落库（04 §3.2 step1）：以一条结果写入模拟在飞完成
        seq = await eng._pool.fetchval(
            "INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)"
            " VALUES ($1, $2, 'agent.think', 'agent:A01', 'autonomous', 'internal', '{\"topic_hint\":\"在飞完成\"}'::jsonb)"
            " RETURNING seq",
            eng.tick_of(eng.now_sim()), eng.now_sim(),
        )
        assert seq > 0
        await eng.resume("db_recovered")
        while len(ticks) <= count_at_pause:
            await asyncio.sleep(0.01)
        assert len(ticks) > count_at_pause, "恢复后 tick 续跑"
    finally:
        stop.set()
        await run_task
    await eng._pool.close()


async def test_batch_advance_event(test_db_dsn: str) -> None:
    """验收 3 指定用例：batch 段结束恰好落一条 time.batch_advanced，payload 仅含 sim_hours。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall)
    baseline = await eng._pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    sim0 = eng.now_sim()

    async def summarize(agent_id: str, sim_hours: float) -> None:
        await asyncio.sleep(0)

    await batch_advance(eng, ("A01", "A02"), 8, summarize=summarize)
    rows = await _events(eng._pool, baseline)
    batch_rows = [r for r in rows if r["type"] == "time.batch_advanced"]
    assert len(batch_rows) == 1, "恰好一条 time.batch_advanced"
    assert _payload(batch_rows[0]) == {"sim_hours": 8}, "payload 仅含 sim_hours（06 §1.2）"
    assert batch_rows[0]["source"] == "system" and batch_rows[0]["trigger"] == "system"
    # 锚点推进 8 sim h 且恢复 tick 排程态
    assert (eng.now_sim() - sim0) >= dt.timedelta(hours=8) - dt.timedelta(seconds=1)
    assert not eng.batch_mode
    await eng._pool.close()


async def test_batch_hook_registry(test_db_dsn: str) -> None:
    """验收 4 指定用例：2 个桩钩子各恰好调用 1 次、顺序 = 注册序；空注册表正常走完。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall)
    calls: list[str] = []

    async def hook_a(ctx) -> None:
        calls.append("a")

    async def hook_b(ctx) -> None:
        calls.append("b")

    register_batch_hook("hook_a", hook_a)
    register_batch_hook("hook_b", hook_b)
    assert registered_hooks() == ("hook_a", "hook_b")
    with pytest.raises(ValueError, match="已注册"):
        register_batch_hook("hook_a", hook_a)

    async def summarize(agent_id: str, sim_hours: float) -> None:
        await asyncio.sleep(0)

    await batch_advance(eng, ("A01",), 8, summarize=summarize)
    assert calls == ["a", "b"], "两钩子各恰好 1 次且顺序 = 注册序"

    clear_batch_hooks()
    calls.clear()
    await batch_advance(eng, ("A01",), 8, summarize=summarize)  # 空注册表空跑
    assert calls == []
    await eng._pool.close()


async def test_batch_chunk_concurrency(test_db_dsn: str) -> None:
    """04 §3.3 伪码口径：>chunk 大小分 chunk 并发（chunk=10）逐 agent 摘要。"""
    wall = FakeWall(T0)
    eng = await _engine(test_db_dsn, wall)
    agents = tuple(f"A{i:02d}" for i in range(1, 26))  # 25 人 → 3 个 chunk（10/10/5）
    in_flight = 0
    max_in_flight = 0
    done: list[str] = []

    async def summarize(agent_id: str, sim_hours: float) -> None:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        done.append(agent_id)

    await batch_advance(eng, agents, 8, summarize=summarize)
    assert sorted(done) == sorted(agents)
    assert 1 < max_in_flight <= CHUNK_SIZE, "chunk 内并发、chunk 间串行"
    await eng._pool.close()
