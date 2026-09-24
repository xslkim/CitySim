"""T-REL-05 邀约协议状态机验收。

口径：02 文档 T-REL-05 验收 1~3（六种可达性 / 改期 2 轮上限 / 第 1 轮改期不计拒绝 /
提醒·宽限·爽约时序 / 取消半价防滥用 / willingness 公式复算与 clamp 钳位边界 +
test_invite_promotes_background_same_tick 升格回归 + social.% 事件序列符合状态机）。
公式/矩阵/冷却数值一律从 config/relations.yaml 读值复算（00 §7 DoD 6）。
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
from worldsim.invite.state_machine import InviteMachine
from worldsim.invite.willingness import compute_willingness
from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.relations.cooldown import CooldownEngine
from worldsim.relations.relations import RelationEngine
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

MON = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=LOCAL_TZ)   # 周一 12:00
TUE = MON + dt.timedelta(days=1)
WED = MON + dt.timedelta(days=2)
THU = MON + dt.timedelta(days=3)
FRI = MON + dt.timedelta(days=4)
SAT = MON + dt.timedelta(days=5)
SUN = MON + dt.timedelta(days=6)
INV_AGENT_IDS = ["A02", "A03", "A04", "A05", "A06", "A07"]
CFG = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "relations.yaml").read_text(encoding="utf-8"))
W = CFG["willingness"]


class InviteStubGateway:
    def __init__(self) -> None:
        self._mock = MockProvider()

    def gen_params(self, task_type: str) -> dict:
        return {}

    async def chat(self, task_type, messages, gen_params=None, *, seed=None, agent_id=None, sim_time=None) -> ChatResult:
        return ChatResult(text=json.dumps({"diary": "摘要", "insights": ["洞察"]}, ensure_ascii=False),
                          prompt_tokens=5, completion_tokens=3, latency_ms=1,
                          request_id="stub", provider="stub", model="stub-1")

    async def embed(self, texts, *, seed=None, agent_id=None, sim_time=None):
        return await self._mock.embed(texts, seed=seed)


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    persona = {"big_five": {"agreeableness": 75, "extraversion": 50, "conscientiousness": 50, "neuroticism": 40, "openness": 50}}
    for aid in INV_AGENT_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, 'star', $3::jsonb, $4::jsonb, 100000, 'apt.lobby')
            ON CONFLICT (id) DO UPDATE SET cognition_tier='star', position='apt.lobby', needs=$4::jsonb, mood=NULL
            """,
            aid, f"测试{aid}", json.dumps(persona),
            json.dumps({"hunger": 70, "energy": 70, "mood": 60, "social": 80, "wealth": 70, "achievement": 70}),
        )
    await p.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", INV_AGENT_IDS)
    await p.execute("DELETE FROM intent_cooldown WHERE agent_id = ANY($1)", INV_AGENT_IDS)
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM memories WHERE agent_id = ANY($1)", INV_AGENT_IDS)
            await conn.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", INV_AGENT_IDS)
            await conn.execute("DELETE FROM intent_cooldown WHERE agent_id = ANY($1)", INV_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", INV_AGENT_IDS)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def machine(pool):
    gw = InviteStubGateway()
    agg = StateAggregator(pool)
    cooldown = CooldownEngine(pool, CFG)
    relations = RelationEngine(pool, CFG)
    m = InviteMachine(pool, gw, CFG, agg=agg, cooldown=cooldown, relations=relations, tick_of=lambda _s: 7)
    return m, agg


async def _put(pool, aid: str, pos: str, tier: str = "star") -> None:
    await pool.execute("UPDATE agents SET position=$2, cognition_tier=$3 WHERE id=$1", aid, pos, tier)


async def test_reachability_six_cases(pool, machine) -> None:
    """验收 1 用例一：可达性六种情形逐行（01 §5.1 表）。"""
    m, _ = machine
    # ① 同节点 → 当面送达，落 social.invite（payload 逐字 06 §1.2）
    await _put(pool, "A02", "apt.lobby")
    await _put(pool, "A03", "apt.lobby")
    out = await m.send(a_id="A02", b_id="A03", activity="晚饭", at_sim=MON + dt.timedelta(hours=7),
                       location="ext.restaurant", sim_now=MON, tick=1, rng_seed=1)
    assert out.kind == "delivered"
    ev = await pool.fetchrow("SELECT type, payload, visibility FROM events WHERE seq=$1", out.event_seqs[0])
    payload = json.loads(ev["payload"])
    assert ev["type"] == "social.invite"
    assert set(payload) == {"from", "to", "activity", "time", "location"}
    assert payload["from"] == "A02" and payload["to"] == "A03"
    # ② 同建筑（公寓内不同节点）→ 折算 10min 送达
    await _put(pool, "A03", "apt.L2.203")
    out = await m.send(a_id="A02", b_id="A03", activity="火锅", at_sim=MON + dt.timedelta(hours=8),
                       location="apt.kitchen", sim_now=MON, tick=2, rng_seed=2)
    assert out.kind == "delivered" and "10" in out.reason
    # ③ 异地（B 在公司）→ 降级 send_message（写双方记忆，不强制唤醒）
    await _put(pool, "A03", "corp.tech")
    out = await m.send(a_id="A02", b_id="A03", activity="晚饭", at_sim=MON + dt.timedelta(hours=7),
                       location="ext.restaurant", sim_now=MON, tick=3, rng_seed=3)
    assert out.kind == "degraded_message"
    ev = await pool.fetchrow("SELECT type FROM events WHERE seq=$1", out.event_seqs[0])
    assert ev["type"] == "social.send_message"
    n_mem = await pool.fetchval("SELECT count(*) FROM memories WHERE agent_id IN ('A02','A03') AND source_event_seq=$1", out.event_seqs[0])
    assert n_mem == 2, "降级消息写双方记忆"
    # ④ 睡眠中（0:30~6:30）→ 作废 + 写"想约但没能发出"记忆
    await _put(pool, "A03", "apt.L2.203")
    night = MON.replace(hour=2, minute=0)
    out = await m.send(a_id="A02", b_id="A03", activity="夜宵", at_sim=night + dt.timedelta(hours=20),
                       location="ext.restaurant", sim_now=night, tick=4, rng_seed=4)
    assert out.kind == "voided" and not out.event_seqs
    mem = await pool.fetchrow("SELECT content FROM memories WHERE agent_id='A02' ORDER BY id DESC LIMIT 1")
    assert "没能发出" in mem["content"]
    # ⑤ 对话/约定执行中 → 意图队列 30min 重试，至多 2 次
    busy_at = TUE.replace(hour=12, minute=0)
    await pool.execute(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
        VALUES (1, $1, 'dialogue.chat', 'agent:A03', 'autonomous', $2, 'public', '{}'::jsonb)
        """,
        busy_at - dt.timedelta(minutes=10), ["A03", "A05"],
    )
    out = await m.send(a_id="A02", b_id="A03", activity="咖啡", at_sim=busy_at + dt.timedelta(hours=6),
                       location="ext.restaurant", sim_now=busy_at, tick=5, rng_seed=5)
    assert out.kind == "queued_retry" and out.retry_at == busy_at + dt.timedelta(minutes=30)
    out = await m.send(a_id="A02", b_id="A03", activity="咖啡", at_sim=busy_at + dt.timedelta(hours=6),
                       location="ext.restaurant", sim_now=busy_at, tick=5, rng_seed=5, retry_count=2)
    assert out.kind == "voided", "重试 2 次用尽作废"
    # ⑥ 背景层 → 可达但需升格（送达即当 tick 升格；升格回归见 test_invite_promotes_background_same_tick）
    r = await m.reach(a_id="A02", b_id="A03", sim_now=WED.replace(hour=12))
    assert r.kind == "same_building", "睡眠/对话判定让位后按位置判定"


async def test_counter_two_round_cap(pool, machine) -> None:
    """验收 1 用例二：改期限 2 轮——第 1/2 轮各落独立 counter 事件，第 3 次改期被拒绝。"""
    m, _ = machine
    out = await m.send(a_id="A02", b_id="A03", activity="晚饭", at_sim=THU + dt.timedelta(hours=7),
                       location="ext.restaurant", sim_now=THU, tick=10, rng_seed=10)
    invite_seq = out.event_seqs[0]
    r1 = await m.respond(invite_seq=invite_seq, action="counter", sim_now=THU, tick=10, rng_seed=10,
                         new_time=THU + dt.timedelta(days=1, hours=1))
    assert r1["round"] == 1 and r1["counts_as_refusal"] is False
    ev = await pool.fetchrow("SELECT type, payload FROM events WHERE seq=$1", r1["event_seqs"][0])
    p1 = json.loads(ev["payload"])
    assert ev["type"] == "social.invite.counter" and p1["round"] == 1 and p1["caused_by"] == str(invite_seq)
    r2 = await m.respond(invite_seq=invite_seq, action="counter", sim_now=THU, tick=11, rng_seed=11,
                         new_time=THU + dt.timedelta(days=1, hours=2))
    assert r2["round"] == 2 and r2["counts_as_refusal"] is True, "第 2 轮后的拒绝计拒绝"
    with pytest.raises(ValueError, match="限 2 轮"):
        await m.respond(invite_seq=invite_seq, action="counter", sim_now=THU, tick=12, rng_seed=12,
                        new_time=THU + dt.timedelta(days=2))
    assert await m.counter_round(invite_seq) == 2


async def test_first_counter_not_refusal_then_accept(pool, machine) -> None:
    """验收 1 用例三：第 1 次改期不计拒绝（无 refuse/冷却/关系结算）；随后接受 → 约定成立 + 双向结算。"""
    m, agg = machine
    out = await m.send(a_id="A02", b_id="A03", activity="晚饭", at_sim=FRI + dt.timedelta(hours=7),
                       location="ext.restaurant", sim_now=FRI, tick=20, rng_seed=20)
    invite_seq = out.event_seqs[0]
    r1 = await m.respond(invite_seq=invite_seq, action="counter", sim_now=FRI, tick=20, rng_seed=20,
                         new_time=FRI + dt.timedelta(days=1))
    assert r1["counts_as_refusal"] is False
    seqs = await agg.flush(tick=20, sim_now=FRI, trigger="autonomous", rng_seed=20)
    assert seqs == [], "第 1 轮改期无关系/需求结算"
    assert await pool.fetchval("SELECT count(*) FROM intent_cooldown WHERE agent_id='A02'") == 0, "第 1 轮改期不写冷却"
    acc = await m.respond(invite_seq=invite_seq, action="accept", sim_now=FRI, tick=21, rng_seed=21)
    appt = await pool.fetchrow("SELECT type, source, trigger, payload FROM events WHERE seq=$1", acc["appointment_seq"])
    payload = json.loads(appt["payload"])
    assert appt["type"] == "social.appointment.created" and appt["source"] == "system"
    assert payload["participants"] == ["A02", "A03"] and payload["activity"] == "晚饭"
    row = CFG["matrix"]["invite_accepted"]
    for a, b in (("A02", "A03"), ("A03", "A02")):
        rel = await pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id=$1 AND b_id=$2", a, b)
        assert (rel["affinity"], rel["tension"]) == (row["delta_affinity"], 0), \
            "invite 被接受双向 +4/-2（tension 下界 clamp 0）"
    await agg.flush(tick=21, sim_now=FRI, trigger="autonomous", rng_seed=21)
    n_mem = await pool.fetchval("SELECT count(*) FROM memories WHERE source_event_seq=$1", acc["appointment_seq"])
    assert n_mem == 2, "约定成立双方单源记忆"


async def test_refuse_settles_and_cools(pool, machine) -> None:
    """invite 被拒绝：social.refuse + 仅 A→B 结算（礼貌减半）+ 24h 冷却（01 §3.2/§3.4）。"""
    m, agg = machine
    out = await m.send(a_id="A02", b_id="A03", activity="晚饭", at_sim=SAT + dt.timedelta(hours=7),
                       location="ext.restaurant", sim_now=SAT, tick=30, rng_seed=30)
    invite_seq = out.event_seqs[0]
    ref = await m.respond(invite_seq=invite_seq, action="refuse", politeness=1, sim_now=SAT, tick=31, rng_seed=31)
    ev = await pool.fetchrow("SELECT type, payload FROM events WHERE seq=$1", ref["event_seqs"][0])
    payload = json.loads(ev["payload"])
    assert ev["type"] == "social.refuse" and payload["request_ref"] == str(invite_seq) and payload["politeness"] == 1
    row = CFG["matrix"]["invite_refused"]
    rel = await pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A02' AND b_id='A03'")
    assert rel["affinity"] == int(row["delta_affinity"] * row["polite_factor"]), "礼貌拒绝减半"
    rel_rev = await pool.fetchval("SELECT count(*) FROM relations WHERE a_id='A03' AND b_id='A02'")
    assert rel_rev == 0, "invite 被拒绝仅 A→B"
    assert await m._cooldown.is_cooled(agent_id="A02", action="invite", target="A03",
                                       sim_now=SAT + dt.timedelta(hours=CFG["cooldown_hours"]["invite_refused"] - 1))
    await agg.flush(tick=31, sim_now=SAT, trigger="autonomous", rng_seed=31)


async def test_remind_grace_stoodup_timing(pool, machine) -> None:
    """验收 1 用例四：提醒/宽限/爽约时序——T-30min 提醒恰一次、宽限 15min、爽约结算与 48h 先验惩罚。

    用 A04→A05 独立对（append-only 事件库跨用例累积，participants 过滤隔离他例约定）。
    """
    m, agg = machine
    pair = ["A04", "A05"]
    appt_at = SUN.replace(hour=19, minute=0)
    out = await m.send(a_id="A04", b_id="A05", activity="晚饭", at_sim=appt_at,
                       location="ext.restaurant", sim_now=SUN.replace(hour=12), tick=40, rng_seed=40)
    acc = await m.respond(invite_seq=out.event_seqs[0], action="accept", sim_now=SUN.replace(hour=12), tick=40, rng_seed=40)
    assert await m.due_reminders(sim_now=SUN.replace(hour=18, minute=0), tick=41, rng_seed=41, participants=pair) == [], "T-30min 前不提醒"
    rem = await m.due_reminders(sim_now=SUN.replace(hour=18, minute=35), tick=42, rng_seed=42, participants=pair)
    assert len(rem) == 1
    ev = await pool.fetchrow("SELECT type, source, trigger, payload FROM events WHERE seq=$1", rem[0])
    payload = json.loads(ev["payload"])
    assert ev["type"] == "social.appointment.remind" and ev["source"] == "system" and ev["trigger"] == "system"
    assert set(payload) == {"participants", "activity", "at_sim"}
    assert await m.due_reminders(sim_now=SUN.replace(hour=18, minute=40), tick=43, rng_seed=43, participants=pair) == [], "提醒恰一次"
    assert await m.due_stood_ups(sim_now=SUN.replace(hour=19, minute=14), tick=44, rng_seed=44, participants=pair) == [], "宽限 15min 内不判"
    # A04 到场（执行窗内有其单方事件）→ A05 放方
    await pool.execute(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
        VALUES (45, $1, 'agent.move', 'agent:A04', 'autonomous', $2, 'public',
                '{"from": "apt.lobby", "to": "ext.restaurant", "sim_cost_min": 10}'::jsonb)
        """,
        appt_at + dt.timedelta(minutes=5), ["A04"],
    )
    outs = await m.due_stood_ups(sim_now=SUN.replace(hour=19, minute=16), tick=46, rng_seed=46, participants=pair)
    assert len(outs) == 1 and outs[0]["no_show"] == "A05" and outs[0]["victim"] == "A04"
    seqs = await agg.flush(tick=46, sim_now=SUN.replace(hour=19, minute=16), trigger="system", rng_seed=46)
    assert len(seqs) == 2, "爽约关系+情绪变更各并入一条聚合事件"
    row = CFG["matrix"]["stood_up"]
    rel = await pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A04' AND b_id='A05'")
    assert (rel["affinity"], rel["tension"]) == (4 + row["delta_affinity"], row["delta_tension"]), \
        "被放方对放方 -8/+5（叠加先前接受 +4/-2，tension clamp 后净 +5）"
    needs = await agg.read_needs("A04")
    assert needs["mood"] == 50, "被放方情绪 -10"
    assert await m._cooldown.is_cooled(agent_id="A04", action="invite", target="A05", sim_now=SUN.replace(hour=19, minute=30))
    penalty = await m.stood_up_prior_penalty(inviter="A05", invitee="A04", sim_now=SUN.replace(hour=20))
    assert penalty == -0.15, "被放方 48h 内接受先验 -0.15（01 §5.3）"
    again = await m.due_stood_ups(sim_now=SUN.replace(hour=20), tick=47, rng_seed=47, participants=pair)
    assert again == [], "爽约恰判一次"


async def test_cancel_half_price_anti_abuse(pool, machine) -> None:
    """验收 1 用例五：取消——T-30min 前 affinity -2/tension +1；每周第 2 次起按爽约半价；T-30min 内不可取消。"""
    m, agg = machine
    appt_at = WED.replace(hour=20, minute=0)
    for i in range(2):
        out = await m.send(a_id="A02", b_id="A03", activity=f"活动{i}", at_sim=appt_at + dt.timedelta(days=i),
                           location="ext.restaurant", sim_now=WED.replace(hour=10), tick=50 + i, rng_seed=50 + i)
        acc = await m.respond(invite_seq=out.event_seqs[0], action="accept", sim_now=WED.replace(hour=10), tick=50 + i, rng_seed=50 + i)
        await agg.flush(tick=50 + i, sim_now=WED.replace(hour=10), trigger="autonomous", rng_seed=50 + i)
        res = await m.cancel(appointment_seq=acc["appointment_seq"], by="A02",
                             sim_now=WED.replace(hour=18), tick=60 + i, rng_seed=60 + i)
        if i == 0:
            assert res["half_price"] is False and res["weekly_count"] == 1
        else:
            assert res["half_price"] is True and res["weekly_count"] == 2, "每周第 2 次取消起按爽约半价"
    rel = await pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A03' AND b_id='A02'")
    acc_aff = CFG["matrix"]["invite_accepted"]["delta_affinity"]
    # 时序：接受 +4×2 → 首取消 -2/+1 → 再接受 -2（1→0 clamp）→ 半价取消 int(-8/2)/int(5/2)
    assert rel["affinity"] == 2 * acc_aff - 2 + int(CFG["matrix"]["stood_up"]["delta_affinity"] / 2)
    assert rel["tension"] == int(CFG["matrix"]["stood_up"]["delta_tension"] / 2), \
        "首取消 +1 被第二次接受的 -2 抵消（clamp 0），半价取消 +2"
    needs = await agg.read_needs("A03")
    assert needs["mood"] == 60 - 5, "半价爽约情绪 -5"
    with pytest.raises(ValueError, match="T-30min"):
        out = await m.send(a_id="A02", b_id="A03", activity="夜宵", at_sim=WED.replace(hour=20, minute=30),
                           location="ext.restaurant", sim_now=WED.replace(hour=11), tick=70, rng_seed=70)
        acc = await m.respond(invite_seq=out.event_seqs[0], action="accept", sim_now=WED.replace(hour=11), tick=70, rng_seed=70)
        await m.cancel(appointment_seq=acc["appointment_seq"], by="A02", sim_now=WED.replace(hour=20, minute=5), tick=71, rng_seed=71)


async def test_willingness_formula_recompute_and_clamp(pool, machine) -> None:
    """验收 1 用例六：willingness 公式从持有方（relations.yaml，01 §3.5 镜像）读值复算 + clamp 钳位边界。"""
    m, _ = machine
    # 中性：全 0 输入 → base×100
    w0 = compute_willingness(W, affinity=0, tension=0, social_need=0, mood=0)
    assert w0.score == W["base"] * 100
    # 逐项复算（周末 + 人格 A 高 + 活动匹配 + 先验惩罚）
    w1 = compute_willingness(W, affinity=40, tension=10, social_need=80, mood=60,
                             big_five={"agreeableness": 75, "extraversion": 50, "neuroticism": 30},
                             sim_now=SAT.replace(hour=12), activity_match=0.5, prior_penalty=-0.15)
    expected = (W["base"] + W["affinity_coef"] * 0.40 + W["tension_coef"] * 0.10
                + W["social_coef"] * 0.80 + W["mood_coef"] * 0.60
                + W["persona"]["agreeableness_high"] + W["occasion"]["weekend_or_holiday"]
                + 0.5 * W["activity_match"] - 0.15)
    assert w1.raw == pytest.approx(expected)
    assert w1.score == pytest.approx(max(W["clamp"][0], min(W["clamp"][1], expected * 100)))
    # 工作日深夜场合修正
    w2 = compute_willingness(W, affinity=0, tension=0, social_need=0, mood=0, sim_now=WED.replace(hour=23))
    assert w2.raw == pytest.approx(W["base"] + W["occasion"]["workday_late_night"])
    # clamp 下界：极端负值不破 0
    w_lo = compute_willingness(W, affinity=-100, tension=100, social_need=0, mood=0,
                               sim_now=WED.replace(hour=23), activity_match=-1, prior_penalty=-0.15)
    assert w_lo.score == W["clamp"][0]
    # clamp 上界：极端正值不破 100
    w_hi = compute_willingness(W, affinity=100, tension=0, social_need=100, mood=100,
                               big_five={"agreeableness": 80, "extraversion": 80, "neuroticism": 20},
                               sim_now=SAT.replace(hour=12), activity_match=1)
    assert w_hi.score == W["clamp"][1]
    # 状态机计算点：读数 = 关系缓存列 + 需求当前值 + Big Five（A03→A02：A 高人格修正生效）
    await pool.execute("INSERT INTO relations (a_id, b_id, affinity, tension) VALUES ('A03', 'A02', 40, 10)")
    w_now = await m.willingness_now(a_id="A02", b_id="A03", sim_now=SAT.replace(hour=12), activity_match=0.0)
    manual = compute_willingness(W, affinity=40, tension=10, social_need=80, mood=60,
                                 big_five={"agreeableness": 75, "extraversion": 50, "conscientiousness": 50,
                                           "neuroticism": 40, "openness": 50},
                                 sim_now=SAT.replace(hour=12))
    assert w_now.score == pytest.approx(manual.score), "计算点读数与公式复算一致（观测用，不做判定）"


async def test_invite_promotes_background_same_tick(pool, machine) -> None:
    """验收 2 指定用例：背景层 B 被邀约，同 tick 落 agent.promoted 且同 tick 产出 B 的响应事件。"""
    m, _ = machine
    await _put(pool, "A04", "apt.lobby", tier="background")
    out = await m.send(a_id="A02", b_id="A04", activity="晚饭", at_sim=THU + dt.timedelta(hours=7),
                       location="ext.restaurant", sim_now=THU, tick=88, rng_seed=88)
    assert out.promoted is True
    invite_seq, promoted_seq = out.event_seqs[0], out.event_seqs[1]
    ev = await pool.fetchrow("SELECT type, tick, trigger, payload FROM events WHERE seq=$1", promoted_seq)
    payload = json.loads(ev["payload"])
    assert ev["type"] == "agent.promoted" and ev["tick"] == 88 and ev["trigger"] == "system"
    assert payload["reason"] == "event_driven" and payload["caused_by"] == str(invite_seq)
    assert payload["from_tier"] == "background" and payload["to_tier"] == "secondary"
    assert await pool.fetchval("SELECT cognition_tier FROM agents WHERE id='A04'") == "secondary"
    acc = await m.respond(invite_seq=invite_seq, action="accept", sim_now=THU, tick=88, rng_seed=88)
    appt_tick = await pool.fetchval("SELECT tick FROM events WHERE seq=$1", acc["appointment_seq"])
    assert appt_tick == 88, "B 同 tick 响应（回应不隔夜，01 §5.1）"


async def test_full_state_machine_sql_sequence(pool, machine) -> None:
    """验收 3 SQL：一场完整邀约的 social.% 事件序列符合状态机（invite → counter → created → remind → stood_up）。"""
    m, agg = machine
    appt_at = FRI.replace(hour=19, minute=0)
    baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    out = await m.send(a_id="A05", b_id="A06", activity="晚饭", at_sim=appt_at,
                       location="ext.restaurant", sim_now=FRI.replace(hour=12), tick=90, rng_seed=90)
    invite_seq = out.event_seqs[0]
    await m.respond(invite_seq=invite_seq, action="counter", sim_now=FRI.replace(hour=12, minute=30), tick=90,
                    rng_seed=90, new_time=appt_at + dt.timedelta(hours=1))
    await m.respond(invite_seq=invite_seq, action="accept", sim_now=FRI.replace(hour=13), tick=91, rng_seed=91)
    await agg.flush(tick=91, sim_now=FRI.replace(hour=13), trigger="autonomous", rng_seed=91)
    await m.due_reminders(sim_now=FRI.replace(hour=18, minute=45), tick=92, rng_seed=92, participants=["A05", "A06"])
    await m.due_stood_ups(sim_now=FRI.replace(hour=19, minute=30), tick=93, rng_seed=93, participants=["A05", "A06"])
    seqs = await agg.flush(tick=93, sim_now=FRI.replace(hour=19, minute=30), trigger="system", rng_seed=93)
    assert len(seqs) == 2
    rows = await pool.fetch(
        "SELECT type FROM events WHERE seq > $1 AND type LIKE 'social.%' ORDER BY seq", baseline
    )
    types = [r["type"] for r in rows]
    assert types == [
        "social.invite", "social.invite.counter", "social.appointment.created",
        "social.appointment.remind", "social.appointment.stood_up",
    ], f"状态机事件序列：{types}"
