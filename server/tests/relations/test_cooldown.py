"""T-REL-04 意图冷却与 argue 二道阻尼验收。

口径：02 文档 T-REL-04 验收 1~2（冷却写入/到期解除/拦截联动、二道阻尼权重、refuse 减半 +
intent_cooldown 行 until_sim 为 sim_time 域）。时长/阈值一律从 config/relations.yaml 读值复算（00 §7 DoD 6）。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.relations.cooldown import CooldownEngine
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=LOCAL_TZ)  # 周一 12:00
CD_AGENT_IDS = ["A28", "A29"]
CFG = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "relations.yaml").read_text(encoding="utf-8"))


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in CD_AGENT_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, 'star', '{}'::jsonb, '{}'::jsonb, 100000, 'apt.lobby')
            ON CONFLICT (id) DO NOTHING
            """,
            aid, f"测试{aid}",
        )
    await p.execute("DELETE FROM intent_cooldown WHERE agent_id = ANY($1)", CD_AGENT_IDS)
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM intent_cooldown WHERE agent_id = ANY($1)", CD_AGENT_IDS)
            await conn.execute("UPDATE agents SET mood=NULL WHERE id = ANY($1)", CD_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", CD_AGENT_IDS)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def engine(pool) -> CooldownEngine:
    return CooldownEngine(pool, CFG)


async def test_cooldown_write_hit_and_expiry(engine, pool) -> None:
    """验收 1 用例一/二：冷却写入（invite 被拒 → 24h）、命中拦截、到期解除；SQL 行 until_sim 为 sim_time 域。"""
    hours = CFG["cooldown_hours"]["invite_refused"]
    until = await engine.write_cooldown(agent_id="A28", trigger_kind="invite_refused", target="A29", sim_now=T0)
    assert until == T0 + dt.timedelta(hours=hours), "冷却时长读 01 §3.4 表（24h）"
    # SQL 验收 2：A01→A28 形态行存在且 until_sim 为 sim_time 域
    row = await pool.fetchrow("SELECT intent_key, until_sim FROM intent_cooldown WHERE agent_id='A28'")
    assert row["intent_key"] == "invite:A29"
    assert row["until_sim"] == until and row["until_sim"] > T0
    assert await engine.is_cooled(agent_id="A28", action="invite", target="A29", sim_now=T0 + dt.timedelta(hours=1))
    assert not await engine.is_cooled(agent_id="A28", action="invite", target="A29", sim_now=until), "到期解除（until_sim > now 才算命中）"
    assert not await engine.is_cooled(agent_id="A28", action="chat", target="A29", sim_now=T0), "冷却键按动作隔离"


async def test_check_action_blocks(engine) -> None:
    """验收 1 用例三：拦截联动（04 §6.2 通用规则冷却行，T-ADJ-03 消费面）。"""
    await engine.write_cooldown(agent_id="A28", trigger_kind="chat_perfunctory", target="A29", sim_now=T0)
    ok, reason = await engine.check_action(agent_id="A28", action="chat", target="A29", sim_now=T0 + dt.timedelta(hours=1))
    assert not ok and "冷却" in reason
    ok, _ = await engine.check_action(
        agent_id="A28", action="chat", target="A29",
        sim_now=T0 + dt.timedelta(hours=CFG["cooldown_hours"]["chat_perfunctory"]),
    )
    assert ok
    # argue_reapproach 触发源：冷却动作集覆盖 chat/invite/argue/gossip（01 §3.4"主动再找同一人"）
    await engine.write_cooldown(agent_id="A29", trigger_kind="argue_reapproach", target="A28", sim_now=T0)
    for action in ("chat", "invite", "argue", "gossip"):
        ok, _ = await engine.check_action(agent_id="A29", action=action, target="A28", sim_now=T0 + dt.timedelta(hours=1))
        assert not ok, f"argue_reapproach 冷却覆盖 {action}"
    ok, _ = await engine.check_action(agent_id="A29", action="send_message", target="A28", sim_now=T0 + dt.timedelta(hours=1))
    assert ok, "send_message 非'主动再找'动作集"


async def test_argue_damping_second_gate(engine) -> None:
    """验收 1 用例四：argue 阻尼二道——tension ≥70 时 argue 意图权重 ×0.5（常时生效，与冷却并行）。"""
    th = CFG["argue_damping"]
    w = await engine.intent_weight(agent_id="A28", action="argue", target="A29", sim_now=T0, tension=th["tension_threshold"])
    assert w == th["weight_factor"]
    w = await engine.intent_weight(agent_id="A28", action="argue", target="A29", sim_now=T0, tension=th["tension_threshold"] - 1)
    assert w == 1.0
    w = await engine.intent_weight(agent_id="A28", action="chat", target="A29", sim_now=T0, tension=90)
    assert w == 1.0, "阻尼只压 argue 意图"


async def test_refuse_streak_halving(engine, pool) -> None:
    """验收 1 用例五：refuse 连续 3 次 → A 对该人 invite/chat 意图权重 ×0.5；被接受交互重置 streak。"""
    streak_cfg = CFG["refuse_streak"]
    for i in range(streak_cfg["count"] - 1):
        await _refuse(pool, i + 1)
    w = await engine.intent_weight(agent_id="A28", action="invite", target="A29", sim_now=T0)
    assert w == 1.0, "未满 3 次不减半"
    await _refuse(pool, streak_cfg["count"])
    for action in streak_cfg["actions"]:
        w = await engine.intent_weight(agent_id="A28", action=action, target="A29", sim_now=T0)
        assert w == streak_cfg["weight_factor"], f"{action} 连续 {streak_cfg['count']} 次被拒后减半"
    w = await engine.intent_weight(agent_id="A28", action="gossip", target="A29", sim_now=T0)
    assert w == 1.0, "减半只作用 invite/chat"
    # 一次被接受交互（约定成立）→ streak 重置
    await pool.execute(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
        VALUES (1, $1, 'social.appointment.created', 'system', 'system', $2, 'public',
                '{"participants": ["A28", "A29"], "activity": "dinner", "at_sim": "2026-10-12T19:00:00+08:00"}'::jsonb)
        """,
        T0, ["A28", "A29"],
    )
    w = await engine.intent_weight(agent_id="A28", action="invite", target="A29", sim_now=T0)
    assert w == 1.0, "被接受交互后 streak 重置"


async def _refuse(pool, seed: int) -> None:
    await pool.execute(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
        VALUES (1, $1, 'social.refuse', 'agent:A29', 'autonomous', $2, 'public',
                jsonb_build_object('from', 'A29', 'to', 'A28', 'request_ref', '1', 'politeness', 0))
        """,
        T0 + dt.timedelta(minutes=seed), ["A28", "A29"],
    )


async def test_grudge_weight_and_expiry(engine) -> None:
    """迁怒（01 §3.3）：marker 写入后 argue/gossip 权重 ×3，48 模拟小时后自然失效（只读 sim_time）。"""
    g = CFG["grudge"]
    until = await engine.set_grudge(agent_id="A28", target="A29", sim_now=T0)
    assert until == T0 + dt.timedelta(hours=g["duration_hours"])
    for action in ("argue", "gossip"):
        w = await engine.intent_weight(agent_id="A28", action=action, target="A29", sim_now=T0 + dt.timedelta(hours=1))
        assert w == float(g["argue_gossip_weight_factor"]), f"迁怒 {action} 权重 ×3"
    w = await engine.intent_weight(agent_id="A28", action="chat", target="A29", sim_now=T0 + dt.timedelta(hours=1))
    assert w == 1.0, "迁怒不放大 chat"
    w = await engine.intent_weight(agent_id="A28", action="argue", target="A29", sim_now=until)
    assert w == 1.0, "48 模拟小时后自然失效（sim_time 域）"
