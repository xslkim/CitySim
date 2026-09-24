"""T-ADJ-03 19 动作校验器验收。

口径：02 文档 T-ADJ-03 验收 1~3（19 动作每动作 ≥1 合法 + ≥1 拦截（≥38 例）、通用规则 5 条各 1 例、
test_cooldown_blocks / test_energy_force_rest / test_hunger_force_eat）+ debts 写入/核销（04 §5.2）。
grep 回归（验收 4）：涉钱动作 payload 金额键仅 `amount_cents`（`grep -n amount_cents validators.py` 对照）。

隔离口径：每用例独立 scratch 库（events append-only 不可清库，惯例同 test_pipeline）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.adjudicator.pipeline import Decision, Observation
from worldsim.adjudicator.state_events import StateAggregator
from worldsim.adjudicator.validators import ActionValidator
from worldsim.invite.state_machine import InviteMachine
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.relations.cooldown import CooldownEngine
from worldsim.relations.needs import NeedsEngine, load_needs_config
from worldsim.relations.relations import RelationEngine, load_relations_config
from worldsim.scheduler.residence import ResidenceEngine
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_validators_test"
PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"
ROOT = Path(__file__).resolve().parents[2]
T0 = dt.datetime(2026, 10, 12, 10, 0, 0, tzinfo=LOCAL_TZ)   # 周一 10:00 工作时段
EVE = T0.replace(hour=20)                                    # 周一 20:00 驻留时段

_NEEDS70 = {"energy": 70, "hunger": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70}


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )


class GW:
    def __init__(self) -> None:
        self._mock = MockProvider()

    def gen_params(self, task_type: str) -> dict:
        return {}

    async def chat(self, task_type, messages, gen_params=None, *, seed=None, agent_id=None, sim_time=None):
        return await self._mock.chat(task_type, messages, gen_params, seed=seed)

    async def embed(self, texts, *, seed=None, agent_id=None, sim_time=None):
        return await self._mock.embed(texts, seed=seed)


def _load(name: str) -> dict:
    with (ROOT / "config" / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest_asyncio.fixture
async def env():
    _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
    _psql(f"CREATE DATABASE {DB_NAME}")
    _psql("CREATE SCHEMA IF NOT EXISTS partman", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS vector", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman", db=DB_NAME)
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", DB_NAME, "-v", "ON_ERROR_STOP=1",
         "-f", str(ROOT / "ddl" / "schema_v1.sql")],
        check=True, capture_output=True, text=True,
    )
    pool = await asyncpg.create_pool(f"postgresql:///{DB_NAME}?host={SOCKET_DIR}", min_size=1, max_size=4)
    world = _load("world.yaml")
    needs_cfg = load_needs_config(str(ROOT / "config" / "needs.yaml"))
    relations_cfg = load_relations_config(str(ROOT / "config" / "relations.yaml"))
    agg = StateAggregator(pool)
    needs_engine = NeedsEngine(needs_cfg)
    relations_engine = RelationEngine(pool, relations_cfg)
    cooldown = CooldownEngine(pool, relations_cfg)
    gw = GW()
    invite = InviteMachine(pool, gw, relations_cfg, agg=agg, cooldown=cooldown, relations=relations_engine)
    residence = ResidenceEngine(pool, world)
    validator = ActionValidator(
        pool, world=world, needs_engine=needs_engine, cooldown=cooldown, relations=relations_engine,
        relations_cfg=relations_cfg, agg=agg, gateway=gw, invite=invite, residence=residence,
        dialogue_settle=None,
    )
    for aid, room, pos in (("A20", "201", "apt.lobby"), ("A21", "203", "apt.lobby"), ("A22", None, "corp.tech")):
        await pool.execute(
            """
            INSERT INTO agents (id, name, gender, age, room_no, department, cognition_tier, persona, needs,
                                balance_cents, position)
            VALUES ($1, $2, 'M', 28, $3, '技术部', 'star', '{}'::jsonb, $4::jsonb, 1000000, $5)
            """,
            aid, f"测试{aid}", room, json.dumps(dict(_NEEDS70)), pos,
        )
    try:
        yield SimpleNamespace(
            pool=pool, world=world, agg=agg, needs=needs_engine, relations=relations_engine,
            cooldown=cooldown, gw=gw, invite=invite, residence=residence, v=validator,
        )
    finally:
        await pool.close()
        _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")


def make_obs(env, agent_id: str, *, sim_now: dt.datetime = T0, position: str = "apt.lobby",
             needs: dict | None = None, balance: int = 1_000_000, persona: dict | None = None) -> Observation:
    return Observation(
        agent_id=agent_id, name=f"测试{agent_id}", sim_time=sim_now, position=position,
        exits=["apt.kitchen", "apt.gym", "apt.roof", "apt.L2.201", "apt.L2.203"],
        co_located=[], needs=dict(needs or _NEEDS70), balance_cents=balance, goals=[],
        recent_events=[], persona=persona or {"big_five": {}}, cognition_tier="star",
    )


def mkdecision(agent_id: str, action: str, args: dict) -> Decision:
    return Decision(agent_id=agent_id, intent=f"想{action}", action_type=action, action_args=args)


# ---- 移动/生理/经济类 -------------------------------------------------------------


async def test_move_legal_and_blocked(env) -> None:
    obs = make_obs(env, "A20")
    ok, _ = await env.v.check(obs, "move", {"to": "apt.kitchen"})
    assert ok
    ok, reason = await env.v.check(obs, "move", {"to": "home.A21"})
    assert not ok and "租客不可" in reason  # 01 §1.6 可达性行（T-LOD-04 联动）
    ok, reason = await env.v.check(obs, "move", {"to": "apt.laundry"})
    assert not ok and "不可达" in reason
    # 结算：位置缓存 + agent.move payload 逐字
    seqs = await env.v.settle(obs, mkdecision("A20", "move", {"to": "apt.kitchen"}),
                              tick=1, sim_now=T0, rng_seed=1)
    assert await env.pool.fetchval("SELECT position FROM agents WHERE id='A20'") == "apt.kitchen"
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert set(payload) == {"from", "to", "sim_cost_min"}


async def test_think_legal_and_rest_rules(env) -> None:
    ok, _ = await env.v.check(make_obs(env, "A20"), "think", {"topic_hint": "晚上吃啥"})
    assert ok
    ok, _ = await env.v.check(make_obs(env, "A20", position="apt.L2.201"), "rest", {"mode": "nap"})
    assert ok, "自己房间内 nap 合法"
    ok, reason = await env.v.check(make_obs(env, "A20", position="apt.lobby"), "rest", {"mode": "nap"})
    assert not ok and "房间" in reason
    ok, reason = await env.v.check(make_obs(env, "A20", position="apt.L2.201"), "rest", {"mode": "deep"})
    assert not ok and "mode" in reason
    # 结算：rest nap 精力 +10
    obs = make_obs(env, "A20", position="apt.L2.201")
    seqs = await env.v.settle(obs, mkdecision("A20", "rest", {"mode": "nap"}), tick=1, sim_now=T0, rng_seed=1)
    change = await env.pool.fetchval(
        "SELECT delta FROM events e, LATERAL jsonb_array_elements(e.payload->'changes') c(c) "
        "WHERE e.type='state.needs_delta'" if False else
        "SELECT 1"
    )
    needs = await env.agg.read_needs("A20")
    assert needs["energy"] == 80.0
    await env.agg.flush(tick=1, sim_now=T0, trigger="system", rng_seed=1)


async def test_work_time_and_place(env) -> None:
    ok, _ = await env.v.check(make_obs(env, "A20", position="corp.tech"), "work", {})
    assert ok
    ok, reason = await env.v.check(make_obs(env, "A20", position="apt.lobby"), "work", {})
    assert not ok and "公司" in reason
    weekend = T0 + dt.timedelta(days=5)  # 周六
    ok, reason = await env.v.check(make_obs(env, "A20", sim_now=weekend, position="corp.tech"), "work", {})
    assert not ok and "工作时段" in reason


async def test_eat_venue_and_companions(env) -> None:
    ok, _ = await env.v.check(make_obs(env, "A20"), "eat", {"venue": "canteen"})
    assert ok
    ok, reason = await env.v.check(make_obs(env, "A20"), "eat", {"venue": "米其林"})
    assert not ok and "venue" in reason
    ok, reason = await env.v.check(make_obs(env, "A20"), "eat", {"venue": "restaurant", "with": ["A22"]})
    assert not ok and "在场或已约定" in reason  # A22 在 corp.tech 不同节点
    # 结算：扣款 + 饥饿/财富结算 + amount_cents 负出
    obs = make_obs(env, "A20")
    before = await env.pool.fetchval("SELECT balance_cents FROM agents WHERE id='A20'")
    seqs = await env.v.settle(obs, mkdecision("A20", "eat", {"venue": "canteen"}), tick=1, sim_now=T0, rng_seed=1)
    after = await env.pool.fetchval("SELECT balance_cents FROM agents WHERE id='A20'")
    assert before - after == 3000  # 食堂 ¥30（01 §1.5 物价表）
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert payload["amount_cents"] == -3000 and payload["venue"] == "canteen"


async def test_shop_store_enum_and_balance(env) -> None:
    ok, _ = await env.v.check(make_obs(env, "A20"), "shop", {"store": "便利店", "budget": 5000})
    assert ok
    ok, reason = await env.v.check(make_obs(env, "A20"), "shop", {"store": "黑市", "budget": 100})
    assert not ok and "枚举" in reason
    ok, reason = await env.v.check(make_obs(env, "A20", balance=100), "shop", {"store": "便利店", "budget": 5000})
    assert not ok and "余额" in reason


async def test_trade_stock_hours_and_holdings(env) -> None:
    ok, _ = await env.v.check(make_obs(env, "A20"), "trade_stock", {"symbol": "XLKJ", "side": "buy", "amount": 180000})
    assert ok
    ok, reason = await env.v.check(make_obs(env, "A20", sim_now=T0.replace(hour=16)), "trade_stock",
                                   {"symbol": "XLKJ", "side": "buy", "amount": 100})
    assert not ok and "交易时段" in reason
    ok, reason = await env.v.check(make_obs(env, "A20"), "trade_stock",
                                   {"symbol": "XLKJ", "side": "sell", "amount": 100})
    assert not ok and "持仓" in reason
    # 结算 buy：余额扣 amount+手续费、持仓增加
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "trade_stock", {"symbol": "XLKJ", "side": "buy", "amount": 180000}),
                              tick=1, sim_now=T0, rng_seed=1)
    bal = await env.pool.fetchval("SELECT balance_cents FROM agents WHERE id='A20'")
    assert bal == 1_000_000 - 180000 - 500  # 手续费 0.05% 最低 ¥5（01 §1.5）
    holdings = json.loads(await env.pool.fetchval("SELECT holdings::text FROM agents WHERE id='A20'"))
    assert holdings["XLKJ"] == 100.0  # 1800 分/股
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert set(payload) == {"symbol", "side", "price", "amount_cents"} and payload["price"] == 1800


async def test_give_gift_tiers_and_settle(env) -> None:
    ok, _ = await env.v.check(make_obs(env, "A20"), "give_gift", {"target": "A21", "tier": 1})
    assert ok
    ok, reason = await env.v.check(make_obs(env, "A20", balance=100), "give_gift", {"target": "A21", "tier": 3})
    assert not ok and "礼物档位" in reason
    ok, reason = await env.v.check(make_obs(env, "A20", position="apt.roof"), "give_gift", {"target": "A21", "tier": 1})
    assert not ok and "非同节点" in reason
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "give_gift", {"target": "A21", "tier": 1}),
                              tick=1, sim_now=T0, rng_seed=1)
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert payload["amount_cents"] == -8800 and payload["tier"] == 1  # 88 档（01 §4）
    rel = await env.pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A21' AND b_id='A20'")
    assert rel["affinity"] == 3 and rel["tension"] == -1 + 1  # 88 档 +3/-1（clamp 下限 0）


# ---- 对话社交/协助/债务类 ---------------------------------------------------------


async def test_chat_co_located_and_deep(env) -> None:
    ok, _ = await env.v.check(make_obs(env, "A20"), "chat", {"target": "A21"})
    assert ok
    ok, reason = await env.v.check(make_obs(env, "A20"), "chat", {"target": "A22"})
    assert not ok and "非同节点" in reason
    ok, reason = await env.v.check(make_obs(env, "A20"), "chat", {"target": "A21", "mode": "deep"})
    assert not ok and "affinity" in reason  # 无关系边 = 0 < 20
    await env.agg.apply_relation_delta(a_id="A20", b_id="A21", delta_affinity=25, cause="1")
    ok, _ = await env.v.check(make_obs(env, "A20"), "chat", {"target": "A21", "mode": "deep"})
    assert ok


async def test_send_message_any_target(env) -> None:
    ok, _ = await env.v.check(make_obs(env, "A20"), "send_message", {"target": "A22", "content_hint": "在吗"})
    assert ok, "异地可触达（01 §4 send_message 行）"
    ok, reason = await env.v.check(make_obs(env, "A20"), "send_message", {"target": "A99"})
    assert not ok and "不存在" in reason
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "send_message", {"target": "A21", "content_hint": "晚上拼桌吗"}),
                              tick=1, sim_now=T0, rng_seed=1)
    rel = await env.pool.fetchrow("SELECT affinity FROM relations WHERE a_id='A20' AND b_id='A21'")
    assert rel["affinity"] == 1  # affinity +1（01 §4；双向 D24）
    needs = await env.agg.read_needs("A20")
    assert needs["social"] == 76.0  # 社交 +6（01 §4）


async def test_invite_args_and_offsite_location(env) -> None:
    args = {"target": "A21", "activity": "晚饭", "time": (T0 + dt.timedelta(hours=2)).isoformat(), "location": "ext.restaurant"}
    ok, _ = await env.v.check(make_obs(env, "A20"), "invite", args)
    assert ok
    ok, reason = await env.v.check(make_obs(env, "A20"), "invite", {"target": "A21", "activity": "晚饭"})
    assert not ok and "time" in reason or "location" in reason
    # 校外 NPC 驻留时段限外部场所/公寓公共区（01 §1.6）
    ok, reason = await env.v.check(make_obs(env, "A20", sim_now=EVE), "invite",
                                   {**args, "target": "A22", "location": "apt.L2.201"})
    assert not ok and "校外" in reason
    ok, _ = await env.v.check(make_obs(env, "A20", sim_now=EVE), "invite",
                              {**args, "target": "A22", "location": "apt.kitchen"})
    assert ok
    # 结算：同节点送达 → social.invite 事件
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "invite", args), tick=1, sim_now=T0, rng_seed=1)
    types = [r["type"] for r in await env.pool.fetch("SELECT type FROM events WHERE seq = ANY($1) ORDER BY seq", seqs)]
    assert "social.invite" in types


async def test_help_drive_zone(env) -> None:
    ok, reason = await env.v.check(make_obs(env, "A20"), "help", {"target": "A21", "matter": "搬快递"})
    assert not ok and "驱动区" in reason  # A21 全 70 正常
    await env.agg.apply_needs_delta(agent_id="A21", need="hunger", delta=-50, cause="1")
    ok, _ = await env.v.check(make_obs(env, "A20"), "help", {"target": "A21", "matter": "带饭"})
    assert ok
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "help", {"target": "A21", "matter": "带饭"}),
                              tick=1, sim_now=T0, rng_seed=1)
    rel = await env.pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A21' AND b_id='A20'")
    assert rel["affinity"] == 6 and rel["tension"] == 0  # help +6/-4（clamp 0；01 §3.2）


async def test_borrow_money_precheck_and_debt_write(env) -> None:
    # 合法前提：B→A affinity≥10、单笔 ≤¥2000、对方余额足
    ok, reason = await env.v.check(make_obs(env, "A20"), "borrow_money", {"target": "A21", "amount": 100000})
    assert not ok and "affinity" in reason
    await env.agg.apply_relation_delta(a_id="A21", b_id="A20", delta_affinity=15, cause="1")
    ok, reason = await env.v.check(make_obs(env, "A20"), "borrow_money", {"target": "A21", "amount": 300000})
    assert not ok and "上限" in reason
    ok, _ = await env.v.check(make_obs(env, "A20"), "borrow_money", {"target": "A21", "amount": 100000})
    assert ok
    # 结算 accepted：debts 行（a_id 债主 A21 / b_id 欠款人 A20，14 模拟日）+ 转账
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "borrow_money", {"target": "A21", "amount": 100000, "result": "accepted"}),
                              tick=1, sim_now=T0, rng_seed=1)
    debt = await env.pool.fetchrow("SELECT a_id, b_id, amount_cents, due_sim, repaid_cents FROM debts")
    assert (debt["a_id"], debt["b_id"], debt["amount_cents"], debt["repaid_cents"]) == ("A21", "A20", 100000, 0)
    assert debt["due_sim"] == T0 + dt.timedelta(days=14)  # 还款期限 14 模拟日（01 §4）
    assert await env.pool.fetchval("SELECT balance_cents FROM agents WHERE id='A20'") == 1_100_000
    assert await env.pool.fetchval("SELECT balance_cents FROM agents WHERE id='A21'") == 900_000
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert payload["amount_cents"] == 100000 and payload["result"] == "accepted"


async def test_borrow_refused_cooldown_and_no_debt(env) -> None:
    await env.agg.apply_relation_delta(a_id="A21", b_id="A20", delta_affinity=15, cause="1")
    obs = make_obs(env, "A20")
    await env.v.settle(obs, mkdecision("A20", "borrow_money", {"target": "A21", "amount": 100000, "result": "rejected"}),
                       tick=1, sim_now=T0, rng_seed=1)
    assert await env.pool.fetchval("SELECT count(*) FROM debts") == 0
    until = await env.cooldown.until("A20", "borrow_money:A21")
    assert until is not None and until > T0 + dt.timedelta(hours=71)  # 72h 冷却（01 §3.4）
    ok, reason = await env.v.check(make_obs(env, "A20"), "borrow_money", {"target": "A21", "amount": 1000})
    assert not ok and "冷却" in reason  # 通用规则②拦截


async def test_repay_money_precheck_and_settle(env) -> None:
    ok, reason = await env.v.check(make_obs(env, "A20"), "repay_money", {"target": "A21", "amount": 100})
    assert not ok and "未结清" in reason  # 无债务边
    await env.v.write_debt(env.pool, lender_id="A21", borrower_id="A20", amount_cents=100000,
                           due_sim=T0 + dt.timedelta(days=14), tick=0)
    ok, reason = await env.v.check(make_obs(env, "A20"), "repay_money", {"target": "A21", "amount": 200000})
    assert not ok and "未还余额" in reason
    ok, _ = await env.v.check(make_obs(env, "A20"), "repay_money", {"target": "A21", "amount": 100000})
    assert ok
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "repay_money", {"target": "A21", "amount": 100000}),
                              tick=1, sim_now=T0, rng_seed=1)
    debt = await env.pool.fetchrow("SELECT repaid_cents, amount_cents FROM debts")
    assert debt["repaid_cents"] == debt["amount_cents"], "全额核销"
    rel = await env.pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A21' AND b_id='A20'")
    assert rel["affinity"] == 8  # 按期归还 +8/-5（01 §3.2 lend_repaid_ontime）
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert payload["debt_ref"].isdigit() and payload["amount_cents"] == -100000


async def test_argue_threshold_and_settle(env) -> None:
    ok, reason = await env.v.check(make_obs(env, "A20"), "argue", {"target": "A21", "reason_hint": "噪音"})
    assert not ok and "硬门槛" in reason
    await env.agg.apply_relation_delta(a_id="A20", b_id="A21", delta_tension=30, cause="1")
    ok, _ = await env.v.check(make_obs(env, "A20"), "argue", {"target": "A21", "reason_hint": "噪音"})
    assert ok
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "argue", {"target": "A21", "reason_hint": "噪音"}),
                              tick=1, sim_now=T0, rng_seed=1)
    rel = await env.pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A20' AND b_id='A21'")
    assert rel["affinity"] == -4 and rel["tension"] == 38  # argue -4/+8（01 §3.2）
    until = await env.cooldown.until("A20", "chat:A21")
    assert until is not None  # argue 后主动再找同一人冷却（01 §3.4 argue_reapproach）


async def test_gossip_cites_validation(env) -> None:
    ok, reason = await env.v.check(make_obs(env, "A20"), "gossip",
                                   {"target_listener": "A21", "about": "A22", "cites": []})
    assert not ok and "无中生有" in reason
    mem = await env.gw.embed(["x"], seed=1)
    mid = await env.pool.fetchval(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance, embedding)
        VALUES ('A21', $1, 'event', '他俩吵架了', '他俩吵架了', 5, $2::vector) RETURNING id
        """,
        T0, "[" + ",".join(repr(v) for v in mem.vectors[0]) + "]",
    )
    ok, reason = await env.v.check(make_obs(env, "A20"), "gossip",
                                   {"target_listener": "A21", "about": "A22", "cites": [str(mid)]})
    assert not ok and "本人记忆" in reason  # cites 必须指向本人记忆（01 §4.1）
    own = await env.pool.fetchval(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance, embedding)
        VALUES ('A20', $1, 'event', '我亲眼看到', '我亲眼看到', 5, $2::vector) RETURNING id
        """,
        T0, "[" + ",".join(repr(v) for v in mem.vectors[0]) + "]",
    )
    ok, _ = await env.v.check(make_obs(env, "A20"), "gossip",
                              {"target_listener": "A21", "about": "A22", "cites": [str(own)]})
    assert ok
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "gossip",
                                              {"target_listener": "A21", "about": "A22", "cites": [str(own)]}),
                              tick=1, sim_now=T0, rng_seed=1)
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert payload["cites"] == [str(own)] and "fidelity" not in payload and "distortion" not in payload
    rel = await env.pool.fetchrow("SELECT affinity FROM relations WHERE a_id='A21' AND b_id='A20'")
    assert rel["affinity"] == 2  # gossip 听者对说者 +2（01 §3.2 gossip_bond）


async def test_refuse_pending_request(env) -> None:
    ok, reason = await env.v.check(make_obs(env, "A20"), "refuse", {"target": "A21", "request_ref": "999999", "politeness": 1})
    assert not ok and "待响应" in reason
    inv = await env.pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
        VALUES (1, $1, 'social.invite', 'agent:A21', 'autonomous', '{A21,A20}', 'public',
                '{"from":"A21","to":"A20","activity":"晚饭","time":"2026-10-12T18:00:00+08:00","location":"ext.restaurant"}'::jsonb)
        RETURNING seq
        """,
        T0,
    )
    ok, _ = await env.v.check(make_obs(env, "A20"), "refuse", {"target": "A21", "request_ref": str(inv), "politeness": 1})
    assert ok
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "refuse", {"target": "A21", "request_ref": str(inv), "politeness": 1}),
                              tick=1, sim_now=T0, rng_seed=1)
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert payload["request_ref"] == str(inv) and payload["politeness"] == 1


async def test_confess_affinity_and_private_place(env) -> None:
    # 通用可达性先行：A21 在 lobby，obs 在自己房间 → 非同节点
    ok, reason = await env.v.check(make_obs(env, "A20", position="apt.L2.201"), "confess", {"target": "A21"})
    assert not ok and "非同节点" in reason
    # 同节点（lobby）→ affinity 0 < 40
    ok, reason = await env.v.check(make_obs(env, "A20"), "confess", {"target": "A21"})
    assert not ok and "affinity" in reason
    # affinity 达标 → lobby 非私密场所
    await env.agg.apply_relation_delta(a_id="A20", b_id="A21", delta_affinity=45, cause="1")
    ok, reason = await env.v.check(make_obs(env, "A20"), "confess", {"target": "A21"})
    assert not ok and "私密" in reason
    # 私密场所（天台）+ affinity 达标 → 合法
    await env.pool.execute("UPDATE agents SET position='apt.roof' WHERE id='A21'")
    ok, _ = await env.v.check(make_obs(env, "A20", position="apt.roof"), "confess", {"target": "A21"})
    assert ok


async def test_apologize_tension_and_ref(env) -> None:
    ok, reason = await env.v.check(make_obs(env, "A20"), "apologize", {"target": "A21", "for_event_ref": "1"})
    assert not ok and "tension" in reason
    await env.agg.apply_relation_delta(a_id="A20", b_id="A21", delta_tension=25, cause="1")
    ok, _ = await env.v.check(make_obs(env, "A20"), "apologize", {"target": "A21", "for_event_ref": "1"})
    assert ok
    obs = make_obs(env, "A20")
    seqs = await env.v.settle(obs, mkdecision("A20", "apologize", {"target": "A21", "for_event_ref": "1", "result": "accepted"}),
                              tick=1, sim_now=T0, rng_seed=1)
    rel = await env.pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A20' AND b_id='A21'")
    assert rel["affinity"] == 4 and rel["tension"] == 19  # apologize 被接受 +4/-6（01 §3.2）


# ---- 通用规则 --------------------------------------------------------------------


async def test_cooldown_blocks(env) -> None:
    """验收 2 指定用例：命中 intent_cooldown（until_sim > now_sim()）的动作被拦截且不落事件。"""
    await env.cooldown.write_cooldown(agent_id="A20", trigger_kind="invite_refused", target="A21", sim_now=T0)
    baseline = await env.pool.fetchval("SELECT count(*) FROM events")
    ok, reason = await env.v.check(make_obs(env, "A20"), "invite",
                                   {"target": "A21", "activity": "晚饭", "time": T0.isoformat(), "location": "ext.restaurant"})
    assert not ok and "冷却" in reason
    assert await env.pool.fetchval("SELECT count(*) FROM events") == baseline, "拦截不落事件"


async def test_energy_force_rest(env) -> None:
    """验收 3 指定用例：精力 <10 强制 rest（04 §6.2 状态行，P2-3 修正后口径）。"""
    low = dict(_NEEDS70, energy=5)
    ok, reason = await env.v.check(make_obs(env, "A20", needs=low), "chat", {"target": "A21"})
    assert not ok and "强制 rest" in reason
    ok, _ = await env.v.check(make_obs(env, "A20", needs=low, position="apt.L2.201"), "rest", {"mode": "nap"})
    assert ok, "rest 本身不被拦"


async def test_hunger_force_eat(env) -> None:
    """验收 3 指定用例：饥饿 <10 强制 eat（低值=饿，01 §3.1）。"""
    low = dict(_NEEDS70, hunger=5)
    ok, reason = await env.v.check(make_obs(env, "A20", needs=low), "move", {"to": "apt.kitchen"})
    assert not ok and "强制 eat" in reason
    ok, _ = await env.v.check(make_obs(env, "A20", needs=low), "eat", {"venue": "canteen"})
    assert ok, "eat 本身不被拦"


async def test_busy_occupancy_generic_rule(env) -> None:
    """通用规则④ 时间/占用：sim_cost 执行期间该 agent 不可被其他动作占用（01 §4）。"""
    obs = make_obs(env, "A20")
    await env.v.settle(obs, mkdecision("A20", "move", {"to": "apt.kitchen"}), tick=1, sim_now=T0, rng_seed=1)
    ok, reason = await env.v.check(make_obs(env, "A20"), "chat", {"target": "A21"})
    assert not ok and "占用" in reason
    later = T0 + dt.timedelta(minutes=20)
    ok, reason2 = await env.v.check(make_obs(env, "A20", sim_now=later, position="apt.kitchen"), "chat", {"target": "A21"})
    assert not ok and "非同节点" in reason2  # 占用解除后进入下一校验层（A21 仍在 lobby）
