"""T-ADJ-04 对话整段落库与逐句时间表验收。

口径：02 文档 T-ADJ-04 验收 1~3（轮数区间 / lines 单调递增 at_offset_s / 单事件落库 +
test_self_eval_bands 三档映射）+ 话题注入（T-REL-06 select/冷却/空选兜底/trigger_point 强制切入）
+ 降速读取点（ThrottleState 场次上限）。
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

from worldsim.adjudicator.dialogue import DialogueEngine, line_offsets
from worldsim.adjudicator.pipeline import Decision, Observation
from worldsim.adjudicator.state_events import StateAggregator
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.relations.cooldown import CooldownEngine
from worldsim.relations.needs import NeedsEngine, load_needs_config
from worldsim.relations.relations import RelationEngine, load_relations_config
from worldsim.relations.topics import TopicSystem, load_rules, load_topics
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_dialogue_test"
PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"
ROOT = Path(__file__).resolve().parents[2]
T0 = dt.datetime(2026, 10, 12, 20, 0, 0, tzinfo=LOCAL_TZ)  # 周一 20:00 黄金档

_NEEDS = {"energy": 70, "hunger": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70}


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )


class StubGW:
    """可编程对话输出网关；record 记录调用 prompt。"""

    def __init__(self, outputs: list[dict] | None = None) -> None:
        self._mock = MockProvider()
        self.outputs = outputs
        self.calls: list[dict] = []

    def gen_params(self, task_type: str) -> dict:
        return {}

    async def chat(self, task_type, messages, gen_params=None, *, seed=None, agent_id=None, sim_time=None):
        self.calls.append({"task_type": task_type, "messages": messages, "seed": seed})
        if self.outputs is not None and task_type == "dialogue":
            body = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
            from worldsim.llm_gateway.providers.base import ChatResult
            return ChatResult(text=json.dumps(body, ensure_ascii=False), prompt_tokens=1,
                              completion_tokens=1, latency_ms=1, request_id="s", provider="stub", model="s")
        return await self._mock.chat(task_type, messages, gen_params, seed=seed)

    async def embed(self, texts, *, seed=None, agent_id=None, sim_time=None):
        return await self._mock.embed(texts, seed=seed)


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
    relations_cfg = load_relations_config(str(ROOT / "config" / "relations.yaml"))
    needs_cfg = load_needs_config(str(ROOT / "config" / "needs.yaml"))
    agg = StateAggregator(pool)
    topics = TopicSystem(pool, load_topics(str(ROOT / "config" / "topics.yaml")),
                         load_rules(str(ROOT / "config" / "topics.yaml")))
    for aid, tp in (("A20", "被说\"画画没用\""), ("A21", ""), ("A22", "")):
        await pool.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'M', 28, 'star', $3::jsonb, $4::jsonb, 1000000, 'apt.kitchen')
            """,
            aid, f"测试{aid}",
            json.dumps({"name": f"测试{aid}", "trigger_point": tp,
                        "speech_style": {"tone": "平", "sentence_len": "短", "catchphrase": "", "taboo": ["炫耀"]}},
                       ensure_ascii=False),
            json.dumps(dict(_NEEDS)),
        )
    try:
        yield SimpleNamespace(
            pool=pool, agg=agg, topics=topics,
            relations=RelationEngine(pool, relations_cfg),
            cooldown=CooldownEngine(pool, relations_cfg),
            needs=NeedsEngine(needs_cfg),
        )
    finally:
        await pool.close()
        _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")


def make_engine(env, gw, *, throttle=None, cap: int = 42) -> DialogueEngine:
    return DialogueEngine(
        env.pool, gw, topics=env.topics, relations=env.relations, cooldown=env.cooldown,
        needs_engine=env.needs, agg=env.agg, throttle=throttle, default_daily_cap=cap,
    )


def make_obs(agent_id: str, *, sim_now: dt.datetime = T0, position: str = "apt.kitchen") -> Observation:
    return Observation(
        agent_id=agent_id, name=f"测试{agent_id}", sim_time=sim_now, position=position,
        exits=[], co_located=[], needs=dict(_NEEDS), balance_cents=1000000, goals=[],
        recent_events=[], persona={"name": f"测试{agent_id}", "big_five": {}}, cognition_tier="star",
    )


def mkdecision(agent_id: str, args: dict) -> Decision:
    return Decision(agent_id=agent_id, intent="想聊聊", action_type="chat", action_args=args)


async def test_rounds_offsets_single_event(env) -> None:
    """验收 1/2 三用例：轮数 6~8 区间、lines at_offset_s 单调递增、整段写一条 dialogue.chat。"""
    engine = make_engine(env, StubGW())
    baseline = await env.pool.fetchval("SELECT count(*) FROM events WHERE type='dialogue.chat'")
    seqs = await engine.settle_chat(make_obs("A20"), {"target": "A21"}, tick=1, sim_now=T0,
                                    rng_seed=1, cost=30, decision=mkdecision("A20", {"target": "A21"}))
    assert len(seqs) == 1
    assert await env.pool.fetchval("SELECT count(*) FROM events WHERE type='dialogue.chat'") == baseline + 1, \
        "整段写一条事件（不多条）"
    row = await env.pool.fetchrow("SELECT payload::text FROM events WHERE seq=$1", seqs[0])
    payload = json.loads(row["payload"])
    lines = payload["lines"]
    assert 6 <= len(lines) <= 8, "轮数区间（01 §7 钦定 6~8）"
    offsets = [l["at_offset_s"] for l in lines]
    assert all(b > a for a, b in zip(offsets, offsets[1:])), "at_offset_s 单调递增"
    assert all(set(l) == {"speaker", "text_display", "at_offset_s"} for l in lines)
    assert payload["participants"] == ["A20", "A21"] and payload["mode"] == "small"
    assert 1 <= len(payload["topic_ids"]) <= 2, "每场 1~2 个话题（01 §7）"
    assert offsets[0] >= 2.0 and offsets[-1] <= 8 * 4.0  # uniform(2,4) 累计分布（04 §6.4）


async def test_self_eval_bands(env) -> None:
    """验收 3 指定用例：mock 自评分映射愉快/平淡/敷衍三档与 01 §7 一致（min≥7/max≤3/其间）。"""
    def out(a: int, b: int) -> dict:
        return {"lines": [{"speaker": "A20", "text": "x"}] * 6,
                "opening_fact": {"type": "scene", "ref": "厨房"},
                "self_eval": {"a_enjoy": a, "b_enjoy": b, "basis": "t"}, "quotable_lines": []}

    cases = [((8, 7), "enjoyable", {"aff": 3, "ten": -1}),
             ((5, 6), "flat", {"aff": 1, "ten": 0}),
             ((2, 3), "perfunctory", {"aff": 0, "ten": 0})]
    for (a, b), band, delta in cases:
        engine = make_engine(env, StubGW([out(a, b)]))
        seqs = await engine.settle_chat(make_obs("A20"), {"target": "A21"}, tick=1, sim_now=T0,
                                        rng_seed=1, cost=30, decision=mkdecision("A20", {"target": "A21"}))
        rel = await env.pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id='A20' AND b_id='A21'")
        aff, ten = (rel["affinity"], rel["tension"]) if rel else (0, 0)  # 敷衍无矩阵行、不进关系结算
        assert (aff, ten) == (delta["aff"], max(0, delta["ten"])), f"{band} 档关系结算（01 §3.2）"
        await env.pool.execute("DELETE FROM relations WHERE a_id='A20' AND b_id='A21'")
        if band == "perfunctory":
            until = await env.cooldown.until("A20", "chat:A21")
            assert until is not None, "敷衍 → 6h 冷却（01 §3.4 chat_perfunctory）"
            await env.pool.execute("DELETE FROM intent_cooldown WHERE agent_id='A20'")


async def test_topic_injection_and_cooldown(env) -> None:
    """话题注入：payload.topic_ids 取自 T-REL-06 且双方记 72 模拟小时冷却（D11）。"""
    engine = make_engine(env, StubGW())
    seqs = await engine.settle_chat(make_obs("A20"), {"target": "A21"}, tick=1, sim_now=T0,
                                    rng_seed=1, cost=30, decision=mkdecision("A20", {"target": "A21"}))
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    for tid in payload["topic_ids"]:
        until = await env.topics.cooldown_until("A20", tid)
        assert until is not None and until > T0 + dt.timedelta(hours=71), "72 模拟小时冷却"


async def test_topic_select_empty_fallback(env) -> None:
    """select 全冷却返回空 → 兜底取最旧冷却的日常类话题（工程默认 D28）。"""
    all_ids = [t.topic_id for t in env.topics._topics]
    await env.topics.mark_used(agent_ids=["A20", "A21"], topic_ids=all_ids, sim_now=T0)
    engine = make_engine(env, StubGW())
    seqs = await engine.settle_chat(make_obs("A20"), {"target": "A21"}, tick=1, sim_now=T0,
                                    rng_seed=1, cost=30, decision=mkdecision("A20", {"target": "A21"}))
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert len(payload["topic_ids"]) == 1 and payload["topic_ids"][0].startswith("T-DAY-"), "兜底取日常类"


async def test_trigger_point_forces_conflict_topic(env) -> None:
    """trigger_point 被台词戳中 → 冲突话题强制切入重生成（01 §7/§11.1）。"""
    hit_out = {"lines": [{"speaker": "A21", "text": "画画没用，别画了"}] * 6,
               "opening_fact": {"type": "scene", "ref": "厨房"},
               "self_eval": {"a_enjoy": 5, "b_enjoy": 5, "basis": "t"}, "quotable_lines": []}
    clean_out = {"lines": [{"speaker": "A21", "text": "风平浪静"}] * 6,
                 "opening_fact": {"type": "scene", "ref": "厨房"},
                 "self_eval": {"a_enjoy": 5, "b_enjoy": 5, "basis": "t"}, "quotable_lines": []}
    gw = StubGW([hit_out, clean_out])
    engine = make_engine(env, gw)
    seqs = await engine.settle_chat(make_obs("A20"), {"target": "A21"}, tick=1, sim_now=T0,
                                    rng_seed=1, cost=30, decision=mkdecision("A20", {"target": "A21"}))
    payload = json.loads(await env.pool.fetchval("SELECT payload::text FROM events WHERE seq=$1", seqs[0]))
    assert len(payload["topic_ids"]) == 1 and payload["topic_ids"][0].startswith("T-CNF-"), \
        "戳中后冲突话题强制切入（重生成 1 次）"
    assert len([c for c in gw.calls if c["task_type"] == "dialogue"]) == 2


async def test_throttle_daily_cap(env) -> None:
    """降速读取点：ThrottleState 场次上限生效——达上限不生成对话事件、写兜底记忆（04 §8.4 ③）。"""

    class ZeroCap:
        def dialogue_daily_cap(self, default: int) -> int:
            return 0  # 降速档生效

    engine = make_engine(env, StubGW(), throttle=ZeroCap())
    baseline = await env.pool.fetchval("SELECT count(*) FROM events WHERE type='dialogue.chat'")
    seqs = await engine.settle_chat(make_obs("A20"), {"target": "A21"}, tick=1, sim_now=T0,
                                    rng_seed=1, cost=30, decision=mkdecision("A20", {"target": "A21"}))
    assert seqs == [] and await env.pool.fetchval("SELECT count(*) FROM events WHERE type='dialogue.chat'") == baseline
    mem = await env.pool.fetchval("SELECT content FROM memories WHERE agent_id='A20' ORDER BY id DESC LIMIT 1")
    assert "说得够多了" in mem


async def test_line_offsets_deterministic() -> None:
    """逐句时间表：uniform(2,4) 累计写死、同 seed 可复算（可回放口径）。"""
    a = line_offsets(6, rng_seed=7, key="A20:A21:1")
    b = line_offsets(6, rng_seed=7, key="A20:A21:1")
    assert a == b and len(a) == 6
    assert all(2.0 <= (a[0] if i == 0 else a[i] - a[i - 1]) <= 4.0 for i in range(6))
