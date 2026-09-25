"""tests/director 逐弧线验收共享支撑（T-DIR-02）：真实 config/arcs.yaml 加载 + 谓词驱动器。

驱动器 `satisfy(cond)` 把谓词条件注入为真（事件插入/关系与需求缓存直写——缓存列测试可写，
事实源为事件流的生产纪律不受影响：本文件是测试夹具，不进生产路径）。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import asyncpg

from tests.world_agent._support import FakeClock
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.director.arcs import ArcEngine, load_arcs

START = dt.datetime(2028, 3, 6, 8, 0, tzinfo=LOCAL_TZ)  # 周一，远离全部节假日/账单日干扰


async def build(dsn: str, arc_id: str, *, intervene=None, now: dt.datetime | None = None):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    clock = FakeClock(now or START)
    cfg = load_arcs()
    tpl = next(a for a in cfg["arcs"] if a["arc_id"] == arc_id)
    eng = ArcEngine(pool, cfg, clock, intervene=intervene)
    return eng, tpl, clock, pool


async def insert_events(pool, type_: str, sim_now: dt.datetime, n: int = 1) -> None:
    for _ in range(n):
        await pool.execute(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload)
            VALUES (0, $1, $2, 'world', 'world', 'internal', '{}'::jsonb)
            """, sim_now, type_)


async def satisfy(pool, clock, cond: dict[str, Any]) -> None:
    """把 {pred, args} 条件注入为真。"""
    pred, args = cond["pred"], cond.get("args", {})
    now = clock.now_sim()
    if pred == "count_events":
        await insert_events(pool, args["type"], now, int(args.get("min", 1)))
    elif pred == "relation_delta":
        a, b = args["a"], args["b"]
        aff = int(args.get("affinity_above", 0)) + 1 if "affinity_above" in args else 0
        if "affinity_below" in args:
            aff = int(args["affinity_below"]) - 1
        ten = int(args.get("tension_above", 0)) + 1 if "tension_above" in args else 0
        await pool.execute(
            """
            INSERT INTO relations (a_id, b_id, affinity, tension) VALUES ($1, $2, $3, $4)
            ON CONFLICT (a_id, b_id) DO UPDATE SET affinity=$3, tension=$4
            """, a, b, aff, ten)
    elif pred == "need_below":
        needs = await pool.fetchval("SELECT needs FROM agents WHERE id=$1", args["agent"])
        needs = json.loads(needs) if isinstance(needs, str) else dict(needs)
        needs[args["need"]] = float(args["below"]) - 1.0
        await pool.execute("UPDATE agents SET needs=$2::jsonb WHERE id=$1",
                           args["agent"], json.dumps(needs))
    elif pred == "frustration_above":
        await pool.execute(
            "UPDATE goals SET frustration=$2 WHERE agent_id=$1 AND status='active'",
            args["agent"], int(args["above"]))
    elif pred == "debt_overdue":
        await pool.execute("UPDATE debts SET due_sim = $1", now - dt.timedelta(days=1))
    elif pred == "stage_age_days":
        clock.set(now + dt.timedelta(days=int(args["min"])))
    else:  # pragma: no cover
        raise AssertionError(f"测试驱动器未覆盖 predicate {pred}")


async def drive_to_stage(eng, tpl, pool, clock, target: str, *, min_gate_days: int = 0) -> None:
    """沿 stage_machine 逐格满足 exit 推进至 target（末格前受 setup_days[0] 铺垫门禁）。"""
    stages = tpl["stage_machine"]
    min_days = int(tpl["payoff_beat"]["setup_days"][0])
    for i, stage in enumerate(stages):
        inst = await eng.get_instance(tpl["arc_id"])
        assert inst["stage"] == stage["stage"], f"期望驻留 {stage['stage']}，实际 {inst['stage']}"
        if stage["stage"] == target:
            return
        if i + 1 == len(stages) - 1:
            # 下一站是末态：先过 setup_days[0] 铺垫门禁（01 §11.3）
            clock.set(clock.now_sim() + dt.timedelta(days=min_days))
        await satisfy(pool, clock, stage["exit"])
        await eng.tick()
    inst = await eng.get_instance(tpl["arc_id"])
    assert inst["stage"] == target, f"未推进到 {target}（当前 {inst['stage']}）"


async def drive_main_path(eng, tpl, pool, clock, *, intervene=None) -> dict:
    """主路径：铺垫→爆发→余波——逐格满足 exit 推进直至 done（末格 exit 满足后完成）。"""
    stages = tpl["stage_machine"]
    await eng.start_arc(tpl["arc_id"], reason="test")
    min_days = int(tpl["payoff_beat"]["setup_days"][0])
    gated_once = False
    for _ in range(len(stages) + 2):  # 末格铺垫门禁需两轮（hold → 放行）
        inst = await eng.get_instance(tpl["arc_id"])
        if inst["status"] != "active":
            break
        idx = next(i for i, s in enumerate(stages) if s["stage"] == inst["stage"])
        stage = stages[idx]
        if stage.get("exit") is None:
            break
        if not gated_once and idx + 1 >= len(stages) - 1:
            # 距末态一步之内：先过 setup_days[0] 铺垫门禁再满足 exit
            clock.set(clock.now_sim() + dt.timedelta(days=min_days))
            gated_once = True
        await satisfy(pool, clock, stage["exit"])
        await eng.tick()
    inst = await eng.get_instance(tpl["arc_id"])
    assert inst["status"] == "done", f"主路径应完成（当前 {inst['stage']}/{inst['status']}）"
    assert inst["via"] == "advance"
    return inst


# ---- 状态重置（模块共享库内跨用例隔离；仅测试夹具） -------------------------------


async def reset_relation(pool, a: str, b: str, affinity: int, tension: int) -> None:
    await pool.execute(
        """
        INSERT INTO relations (a_id, b_id, affinity, tension) VALUES ($1, $2, $3, $4)
        ON CONFLICT (a_id, b_id) DO UPDATE SET affinity=$3, tension=$4
        """, a, b, affinity, tension)


async def set_need(pool, agent_id: str, need: str, value: float) -> None:
    needs = await pool.fetchval("SELECT needs FROM agents WHERE id=$1", agent_id)
    needs = json.loads(needs) if isinstance(needs, str) else dict(needs)
    needs[need] = float(value)
    await pool.execute("UPDATE agents SET needs=$2::jsonb WHERE id=$1", agent_id, json.dumps(needs))


async def set_frustration(pool, agent_id: str, value: int) -> None:
    await pool.execute("UPDATE goals SET frustration=$2 WHERE agent_id=$1 AND status='active'",
                       agent_id, value)


async def reset_arc(pool, arc_id: str) -> None:
    """清弧线实例/活跃/冷却状态（模块共享库内跨用例隔离；仅测试夹具）。"""
    await pool.execute(
        "DELETE FROM world_state WHERE key IN ('arcs.active', $1, $2)",
        f"arc.inst.{arc_id}", f"arc.cooldown.{arc_id}")


async def fail_forward_case(eng, tpl, pool, clock, *, ff_at: str, expect_stage: str | None) -> None:
    """fail-forward 分支通用驱动：推进到 ff.at → 驻留超 if_blocked 阈值 → 降级/收尾。"""
    await reset_arc(pool, tpl["arc_id"])
    await eng.start_arc(tpl["arc_id"], reason="test")
    await drive_to_stage(eng, tpl, pool, clock, ff_at)
    ff = next(f for f in tpl["fail_forward"] if f["at"] == ff_at)
    clock.set(clock.now_sim() + dt.timedelta(days=int(ff["if_blocked"]["args"]["min"])))
    await eng.tick()
    inst = await eng.get_instance(tpl["arc_id"])
    if expect_stage is None or ff["degrade_to"] == "done":
        assert inst["status"] == "done" and inst["via"] == "fail_forward"
    else:
        assert inst["stage"] == expect_stage and inst["status"] == "active"
    audit = await eng._get("arcs.audit_log")
    assert any(a["action"] in ("fail_forward", "force_fail_forward") for a in audit)


async def setup_days_window_case(eng, tpl, pool, clock, *, penultimate: str) -> None:
    """setup_days 窗口通用断言（读配置）：<min 不爆发、>max 强制 fail-forward 收尾。"""
    await reset_arc(pool, tpl["arc_id"])
    min_d, max_d = (int(x) for x in tpl["payoff_beat"]["setup_days"])
    await eng.start_arc(tpl["arc_id"], reason="test")
    await drive_to_stage(eng, tpl, pool, clock, penultimate)
    stages = tpl["stage_machine"]
    idx = next(i for i, s in enumerate(stages) if s["stage"] == penultimate)
    await satisfy(pool, clock, stages[idx]["exit"])
    await eng.tick()
    assert (await eng.get_instance(tpl["arc_id"]))["stage"] == penultimate, "<min 不爆发（01 §11.3）"
    clock.set(clock.now_sim() + dt.timedelta(days=max_d + 1))
    await eng.tick()
    inst = await eng.get_instance(tpl["arc_id"])
    assert inst["status"] == "done" and inst["via"] == "fail_forward", ">max 强制 fail-forward（01 §6.2）"
