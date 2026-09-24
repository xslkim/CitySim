"""T-REL-03 目标系统与受阻挫败规则验收。

口径：02 文档 T-REL-03 验收 1~2（周日刷新 / 层别目标数 / 受阻 3 次挫败 / 挫败值三档（换策略/放弃/恢复）+
刷新后 active 目标数符合 01 §3.3 层别口径 SQL）。硬数字一律从 config/goals.yaml 读值复算（00 §7 DoD 6）。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.adjudicator.state_events import StateAggregator
from worldsim.relations.cooldown import CooldownEngine
from worldsim.relations.goals import GoalEngine, load_goals
from worldsim.relations.relations import RelationEngine
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 18, 21, 0, 0, tzinfo=LOCAL_TZ)  # 周日 21:00（第 1 周周日晚）
GOAL_AGENT_IDS = ["A30", "A31", "A32"]
CFG = load_goals(str(Path(__file__).resolve().parents[2] / "config" / "goals.yaml"))
RULES = CFG["frustration_rules"]
TIER_COUNTS = CFG["meta"]["active_per_tier"]

_TIERS = {"A30": "star", "A31": "secondary", "A32": "background"}


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid, tier in _TIERS.items():
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, $3, $4::jsonb, $5::jsonb, 100000, 'apt.lobby')
            ON CONFLICT (id) DO UPDATE SET cognition_tier=$3, needs=$5::jsonb, mood=NULL
            """,
            aid, f"测试{aid}", tier,
            json.dumps({"big_five": {"extraversion": 50, "agreeableness": 50, "conscientiousness": 50, "neuroticism": 50, "openness": 50}}),
            json.dumps({"mood": 70}),
        )
    await p.execute("DELETE FROM goals WHERE agent_id = ANY($1)", GOAL_AGENT_IDS)
    await p.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", GOAL_AGENT_IDS)
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM goals WHERE agent_id = ANY($1)", GOAL_AGENT_IDS)
            await conn.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", GOAL_AGENT_IDS)
            await conn.execute("DELETE FROM memories WHERE agent_id = ANY($1)", GOAL_AGENT_IDS)
            await conn.execute("DELETE FROM intent_cooldown WHERE agent_id = ANY($1)", GOAL_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", GOAL_AGENT_IDS)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def engines(pool):
    agg = StateAggregator(pool)
    reflected: list[tuple[str, str, int]] = []

    async def reflect_stub(agent_id: str, content: str, *, importance: int) -> None:
        reflected.append((agent_id, content, importance))

    engine = GoalEngine(
        pool, CFG,
        cooldown=CooldownEngine(pool, yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "relations.yaml").read_text(encoding="utf-8"))),
        relations=RelationEngine(pool, yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "relations.yaml").read_text(encoding="utf-8"))),
        reflect_fn=reflect_stub,
    )
    return engine, agg, reflected


async def test_weekly_refresh_tier_counts(pool, engines) -> None:
    """验收 1 用例一/二 + 验收 2 SQL：周日刷新后各层 active 目标数符合 01 §3.3（star 3 / secondary 2 / background 1）。"""
    engine, agg, _ = engines
    drawn = await engine.refresh_weekly(agg, sim_now=T0, cause="400")
    assert set(drawn) == set(GOAL_AGENT_IDS), "全员刷新"
    lib_ids = set(engine._lib)
    for aid, ids in drawn.items():
        assert len(ids) == TIER_COUNTS[_TIERS[aid]], f"{aid}({_TIERS[aid]}) 目标数按层"
        assert set(ids) <= lib_ids, "抽取自 36 条库"
    rows = await pool.fetch("SELECT agent_id, count(*) AS n FROM goals WHERE status='active' GROUP BY 1 ORDER BY 1")
    assert {r["agent_id"]: r["n"] for r in rows} == {aid: TIER_COUNTS[t] for aid, t in _TIERS.items()}
    weeks = await pool.fetch("SELECT DISTINCT sim_week FROM goals WHERE status='active'")
    assert [r["sim_week"] for r in weeks] == [1], "sim_week = 当前模拟周"
    # 幂等：同周重复刷新不重复抽取
    again = await engine.refresh_weekly(agg, sim_now=T0, cause="401")
    assert again == {}, "同周已有 active 目标不重复抽取"


async def test_blocked_three_triggers_frustration(pool, engines) -> None:
    """验收 1 用例三：同一目标受阻 3 次 → 挫败事件（情绪 -12 并入 needs_delta、挫败值 +10）；2 次不触发。"""
    engine, agg, _ = engines
    await pool.execute("INSERT INTO goals (agent_id, sim_week, goal) VALUES ('A30', 1, '和 3 个不同邻居聊天')")
    gid = await pool.fetchval("SELECT id FROM goals WHERE agent_id='A30' AND status='active'")
    out1 = await engine.record_block(agg, goal_id=gid, sim_now=T0, cause="410", blocker_id="A31")
    out2 = await engine.record_block(agg, goal_id=gid, sim_now=T0, cause="410", blocker_id="A31")
    assert not out1["frustrated"] and not out2["frustrated"], "前 2 次受阻不触发挫败"
    out3 = await engine.record_block(agg, goal_id=gid, sim_now=T0, cause="410", blocker_id="A31")
    assert out3["frustrated"] and out3["frustration"] == RULES["frustration_gain"]
    assert out3["blocked_count"] == RULES["frustration_trigger_blocks"]
    seqs = await agg.flush(tick=50, sim_now=T0, trigger="system", rng_seed=50)
    assert len(seqs) == 1
    payload = json.loads(await pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    mood_changes = [c for c in payload["changes"] if c["need"] == "mood"]
    assert mood_changes and mood_changes[0]["delta"] == float(RULES["frustration_mood_delta"]), "挫败情绪 -12 并入 state.needs_delta"
    # 第 4 次受阻不再重复触发（M1 口径：恰第 3 次触发一次，D18）
    out4 = await engine.record_block(agg, goal_id=gid, sim_now=T0, cause="410", blocker_id="A31")
    assert not out4["frustrated"]


async def test_frustration_three_tiers(pool, engines) -> None:
    """验收 1 用例四：挫败值三档——≥30 迁怒 / ≥50 换策略+反思 / ≥80 放弃+深度反思+清零至 20。"""
    engine, agg, reflected = engines
    await pool.execute("INSERT INTO goals (agent_id, sim_week, goal, frustration) VALUES ('A30', 1, '表白', 0)")
    gid = await pool.fetchval("SELECT id FROM goals WHERE agent_id='A30' AND status='active'")

    async def bump(to: int, blocker: str | None = "A31") -> None:
        row = await pool.fetchrow("SELECT goal, frustration FROM goals WHERE id=$1", gid)
        await pool.execute("UPDATE goals SET frustration=$2 WHERE id=$1", gid, to)
        await engine._apply_frustration_effects(
            agent_id="A30", goal_row={"id": gid, "goal": row["goal"]}, prev=row["frustration"], new=to,
            agg=agg, sim_now=T0, cause="420", blocker_id=blocker,
        )

    # ≥30 迁怒：对 blocker affinity -6 / tension +8（01 §3.3）+ argue/gossip 权重 ×3（T-REL-04 marker）
    await bump(RULES["grudge_threshold"])
    rel = await pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A30' AND b_id='A31'")
    assert (rel["affinity"], rel["tension"]) == (RULES["grudge_affinity_delta"], RULES["grudge_tension_delta"])
    w = await engine._cooldown.intent_weight(agent_id="A30", action="argue", target="A31", sim_now=T0)
    assert w == 3.0, "迁怒 argue 权重 ×3（48h）"
    # ≥50 换策略：目标降级改写 + 生成一次反思（默认重要性 5，D18）
    await bump(RULES["replan_threshold"])
    goal_text = await pool.fetchval("SELECT goal FROM goals WHERE id=$1", gid)
    assert "换策略" in goal_text
    assert any(imp == 5 for _, _, imp in reflected), "换策略生成一次反思"
    # ≥80 放弃：status='abandoned' + 深度反思（重要性 ≥8）+ 挫败值清零至 20
    await bump(RULES["abandon_threshold"])
    row = await pool.fetchrow("SELECT status, frustration FROM goals WHERE id=$1", gid)
    assert row["status"] == "abandoned" and row["frustration"] == RULES["abandon_reset_to"]
    assert any(imp >= RULES["abandon_reflection_importance"] for _, _, imp in reflected), "放弃生成深度反思（重要性 ≥8）"


async def test_weekly_fail_settle_and_recovery(pool, engines) -> None:
    """周日晚失败结算：未完成且受阻 ≥2 → +8；未受阻 → +4；自然恢复每模拟日 -4（下限 0）。"""
    engine, agg, _ = engines
    await pool.execute(
        """
        INSERT INTO goals (agent_id, sim_week, goal, blocked_count, frustration) VALUES
          ('A30', 1, '表白', 2, 0), ('A30', 1, '送一次礼物', 0, 0), ('A31', 1, '健身 3 次', 1, 3)
        """
    )
    await engine.refresh_weekly(agg, sim_now=T0 + dt.timedelta(days=7), cause="430")  # 第 2 周周日
    rows = await pool.fetch(
        "SELECT goal, frustration, status FROM goals WHERE agent_id='A30' AND sim_week=1 ORDER BY id"
    )
    by_goal = {r["goal"]: r for r in rows}
    assert by_goal["表白"]["frustration"] == RULES["weekly_fail_blocked_gain"], "受阻 ≥2 → +8"
    assert by_goal["送一次礼物"]["frustration"] == RULES["weekly_fail_idle_gain"], "未受阻（纯没做）→ +4"
    assert all(r["status"] == "abandoned" for r in rows), "周结关闭未完成目标"
    recovered = await engine.daily_recovery(agent_id="A31")
    assert recovered >= 0
    new_f = await pool.fetchval("SELECT frustration FROM goals WHERE agent_id='A31' AND sim_week=1")
    assert new_f == 3 + RULES["weekly_fail_idle_gain"], "已关闭行挫败值冻结（恢复只作用 active 目标）"
    # 自然恢复：active 目标挫败值 -4（下限 0）
    w2_gid = await pool.fetchval("SELECT id FROM goals WHERE agent_id='A31' AND sim_week=2 AND status='active' LIMIT 1")
    await pool.execute("UPDATE goals SET frustration=3 WHERE id=$1", w2_gid)
    await engine.daily_recovery(agent_id="A31")
    new_f = await pool.fetchval("SELECT frustration FROM goals WHERE id=$1", w2_gid)
    assert new_f == 0, "active 目标挫败值 3 - 4 → 下限 0"


async def test_completion_programmable_subset(pool, engines) -> None:
    """完成条件判定器 P0 子集：chat 场次数（事件计数类）达成 → status='done'。"""
    engine, _, _ = engines
    await pool.execute("INSERT INTO goals (agent_id, sim_week, goal) VALUES ('A30', 1, '和 3 个不同邻居聊天')")
    for i in range(3):
        await pool.execute(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES ($1, $2, 'dialogue.chat', 'agent:A30', 'autonomous', $3, 'public', '{}'::jsonb)
            """,
            200 + i, dt.datetime(2026, 10, 13, 12, 0, tzinfo=LOCAL_TZ) + dt.timedelta(hours=i), ["A30", "A31"],
        )
    done = await engine.check_completions(agent_id="A30", sim_now=T0)
    assert done and done[0][1] == "G-SOC-01", "3 场 chat → G-SOC-01 完成"
    status = await pool.fetchval("SELECT status FROM goals WHERE agent_id='A30' AND sim_week=1")
    assert status == "done"
