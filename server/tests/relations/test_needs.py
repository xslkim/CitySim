"""T-REL-01 六需求衰减引擎验收。

口径：02 文档 T-REL-01 验收 1~2（六需求各自衰减积分/睡眠分支/驱动区进入/强制行为触发 +
test_decay_anchored_sim_time 同一模拟时长不同压缩比衰减量一致，00 §4 红线 11）。
数值一律从 config/needs.yaml（01 §3.1 镜像）读值复算，不抄字面量（00 §7 DoD 6）。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.adjudicator.state_events import NEED_KEYS, StateAggregator
from worldsim.relations.needs import NeedsEngine
from worldsim.time_engine.clock import LOCAL_TZ

T0 = dt.datetime(2026, 10, 12, 0, 0, 0, tzinfo=LOCAL_TZ)  # 周一 00:00
NEEDS_AGENT_IDS = ["A23", "A24"]
CFG = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "needs.yaml").read_text(encoding="utf-8"))

_INITIAL = {"hunger": 70, "energy": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70}


@pytest.fixture(scope="module")
def engine() -> NeedsEngine:
    return NeedsEngine(CFG)


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in NEEDS_AGENT_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, 'star', '{}'::jsonb, $3::jsonb, 100000, 'apt.lobby')
            ON CONFLICT (id) DO UPDATE SET needs=$3::jsonb
            """,
            aid, f"测试{aid}", json.dumps(_INITIAL),
        )
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", NEEDS_AGENT_IDS)
        finally:
            await conn.close()


def test_decay_integration_per_need(engine: NeedsEngine) -> None:
    """验收 1 用例一：六需求各自衰减积分（醒时/工作时段额外/仅工作日白天/财富不衰减）。"""
    rates = CFG["decay_per_sim_hour"]
    # 周一 10:00~11:00（工作时段醒时）：饥饿 -4、精力 -3-1、情绪 -1、社交 -1.2、成就 -0.8、财富 0
    t1 = T0.replace(hour=10)
    t2 = t1 + dt.timedelta(hours=1)
    assert engine.integrate("hunger", t1, t2) == rates["hunger"]["awake"]
    assert engine.integrate("energy", t1, t2) == rates["energy"]["awake"] + rates["energy"]["working_extra"]
    assert engine.integrate("mood", t1, t2) == rates["mood"]["base"]
    assert engine.integrate("social", t1, t2) == rates["social"]["base"]
    assert engine.integrate("achievement", t1, t2) == rates["achievement"]["workday_daytime"]
    assert engine.integrate("wealth", t1, t2) == 0.0, "财富不衰减（事件驱动）"
    # 周六同时段：无工作约束 → 精力不额外扣、成就不衰减
    sat = T0 + dt.timedelta(days=5)
    s1 = sat.replace(hour=10)
    s2 = s1 + dt.timedelta(hours=1)
    assert engine.integrate("energy", s1, s2) == rates["energy"]["awake"]
    assert engine.integrate("achievement", s1, s2) == 0.0, "成就仅工作日白天生效"
    # 周日 18:30~19:30（晚间非工作时段）同理
    sun = T0 + dt.timedelta(days=6)
    e1 = sun.replace(hour=18, minute=30)
    assert engine.integrate("energy", e1, e1 + dt.timedelta(hours=1)) == rates["energy"]["awake"]


def test_sleep_branch(engine: NeedsEngine) -> None:
    """验收 1 用例二：睡眠分支（0:30~6:30 饥饿 -1.0/h、精力不被动衰减；跨零点积分正确）。"""
    rates = CFG["decay_per_sim_hour"]
    # 周一 00:00~02:00：前 30min 醒时（-4×0.5），后 90min 睡眠（-1×1.5）→ -3.5
    delta = engine.integrate("hunger", T0, T0 + dt.timedelta(hours=2))
    assert delta == pytest.approx(rates["hunger"]["awake"] * 0.5 + rates["hunger"]["sleeping"] * 1.5)
    assert engine.integrate("hunger", T0.replace(hour=2), T0.replace(hour=3)) == rates["hunger"]["sleeping"]
    assert engine.integrate("energy", T0.replace(hour=2), T0.replace(hour=3)) == 0.0, "睡眠中精力不被动衰减（恢复经 rest 满足量）"
    # 跨睡眠边界出段：06:00~07:00 = 睡眠 30min + 醒时 30min
    delta = engine.integrate("hunger", T0.replace(hour=6), T0.replace(hour=7))
    assert delta == pytest.approx(rates["hunger"]["sleeping"] * 0.5 + rates["hunger"]["awake"] * 0.5)


def test_drive_zone_and_forced_action(engine: NeedsEngine) -> None:
    """验收 1 用例三/四：驱动区进入（<25）与强制行为（精力<10→rest、饥饿<10→eat）。"""
    th = CFG["thresholds"]
    assert engine.zone(th["drive_zone_below"] - 1) == "drive"
    assert engine.zone(th["forced_below"] - 1) == "forced"
    assert engine.zone(60) == "normal"
    assert engine.forced_action({"energy": 5, "hunger": 50}) == "rest", "精力 <10 强制 rest（04 §6.2 状态行）"
    assert engine.forced_action({"energy": 50, "hunger": 5}) == "eat", "饥饿 <10 强制 eat（01 §3.1，低值 = 匮乏方向）"
    assert engine.forced_action({"energy": 50, "hunger": 50}) is None
    # 饥饿 <15 非进食动作降权（04 §6.2 状态行；倍率读 yaml，工程默认 D14）
    rule = CFG["state_rule"]
    needs = {"hunger": rule["hunger_downweight_below"] - 1, "energy": 60}
    assert engine.action_weight(needs, "chat") == rule["non_eat_downweight_factor"]
    assert engine.action_weight(needs, "eat") == 1.0
    assert engine.action_weight({"hunger": 60}, "chat") == 1.0


def test_decay_anchored_sim_time(engine: NeedsEngine) -> None:
    """验收 2 指定用例：同一模拟时长在不同压缩比下衰减量一致（00 §4 红线 11）。

    引擎接口只接受 sim_time 区间（wall_time 不可入），不同压缩比仅改变该区间对应的真实时长，
    积分结果按构造一致；另验证积分可加性（分段求和 == 整段）。
    """
    start = T0.replace(hour=8, minute=45)
    end = start + dt.timedelta(hours=3)  # 跨工作时段边界 09:00
    for need in NEED_KEYS:
        whole = engine.integrate(need, start, end)
        # 压缩比 1×/3×/6× 下同一模拟区间（真实时长 3h/1h/0.5h，引擎不可见）→ 衰减量一致
        for _ratio in (1.0, 3.0, 6.0):
            assert engine.integrate(need, start, end) == whole
        mid = start + dt.timedelta(hours=1, minutes=17)  # 任意分段点
        assert engine.integrate(need, start, mid) + engine.integrate(need, mid, end) == pytest.approx(whole)


@pytest.mark.asyncio
async def test_settle_decay_via_aggregator(pool, engine: NeedsEngine) -> None:
    """衰减变更经聚合器并入本 tick state.needs_delta（04 §6.5）：缓存列更新 + 事件落库。"""
    agg = StateAggregator(pool)
    t1 = T0.replace(hour=10)
    t2 = t1 + dt.timedelta(hours=2)  # 周一工作时段 2h
    changes = await engine.settle_decay(agg, agent_id="A23", from_sim=t1, to_sim=t2, cause="201")
    assert {c["need"] for c in changes} == {"hunger", "energy", "mood", "social", "achievement"}
    by_need = {c["need"]: c for c in changes}
    assert by_need["hunger"]["delta"] == pytest.approx(CFG["decay_per_sim_hour"]["hunger"]["awake"] * 2)
    assert by_need["energy"]["new_value"] == pytest.approx(70 + (CFG["decay_per_sim_hour"]["energy"]["awake"] + CFG["decay_per_sim_hour"]["energy"]["working_extra"]) * 2)
    seqs = await agg.flush(tick=20, sim_now=t2, trigger="system", rng_seed=20)
    assert len(seqs) == 1
    row = await pool.fetchrow("SELECT type, trigger, payload FROM events WHERE seq=$1", seqs[0])
    assert row["type"] == "state.needs_delta" and row["trigger"] == "system"
    payload_changes = json.loads(row["payload"])["changes"]
    assert all(c["cause"] == "201" for c in payload_changes), "cause 指系统结算事件（裸 seq 数字字符串）"
    cache = await agg.read_needs("A23")
    assert cache["wealth"] == 70, "财富不衰减"
