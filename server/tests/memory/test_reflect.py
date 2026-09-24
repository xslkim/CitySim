"""T-MEM-02 反思系统验收。

口径：02 文档 T-MEM-02 验收 1~2（阈值触发 / 每日兜底 / 双通道成对出现 / think 不产事件 +
深度反思后 reflection 记忆 ≥1 且事件 payload 仅含 text_display 键的 SQL 形态）；
降速读取点以 ThrottleState 桩替换验证（T-OPS-02 接线 seam）。
"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest
import pytest_asyncio

from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.memory import store
from worldsim.memory.reflect import FALLBACK_IMPORTANCE, NullThrottleState, Reflector
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 21, 0, 0, tzinfo=LOCAL_TZ)
MEM_AGENT_IDS = ["A13", "A14"]


class ReflectStubGateway:
    """按 task_type 给罐装响应的网关桩（embed 走 mock 确定性）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._mock = MockProvider()

    def gen_params(self, task_type: str) -> dict:
        return {}

    async def chat(self, task_type, messages, gen_params=None, *, seed=None, agent_id=None, sim_time=None) -> ChatResult:
        self.calls.append(task_type)
        if task_type == "reflection":
            text = json.dumps({"insights": ["我最近总在躲着谁", "转笔的时候其实我是在紧张", "下周要把话说开"]}, ensure_ascii=False)
        elif task_type == "bgsummary":
            text = json.dumps({"diary": "今天想明白了一件事：躲不是办法。", "needs_delta": {}, "mood": "平稳", "tomorrow_plan": "明天照常说开"}, ensure_ascii=False)
        else:
            text = json.dumps({"intent": "想想事情", "action": {"type": "think", "args": {"topic_hint": "测试"}}, "emotion_delta": {}}, ensure_ascii=False)
        return ChatResult(text=text, prompt_tokens=10, completion_tokens=5, latency_ms=1,
                          request_id="stub", provider="stub", model="stub-1")

    async def embed(self, texts, *, seed=None, agent_id=None, sim_time=None):
        return await self._mock.embed(texts, seed=seed)


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in MEM_AGENT_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position, mood)
            VALUES ($1, $2, 'F', 25, 'star', $3::jsonb, '{}'::jsonb, 100000, 'apt.lobby', NULL)
            ON CONFLICT (id) DO UPDATE SET mood=NULL
            """,
            aid, f"测试{aid}", json.dumps({"name": f"测试{aid}", "big_five": {"neuroticism": 60}}),
        )
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM memories WHERE agent_id = ANY($1)", MEM_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", MEM_AGENT_IDS)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def gw() -> ReflectStubGateway:
    return ReflectStubGateway()


def _reflector(pool, gw, **kw) -> Reflector:
    return Reflector(pool, gw, threshold=20, tick_of=lambda _sim: 42, **kw)


async def _feed(pool, gw, agent_id: str, importances: list[int], *, kind: str = "event") -> None:
    for i, imp in enumerate(importances):
        await store.insert_memory(
            pool, gw, agent_id=agent_id, sim_time=T0 + dt.timedelta(minutes=5 * i),
            kind=kind, content=f"经历片段{i}（重要性{imp}）", importance=imp, rng_seed=i,
        )


async def test_threshold_trigger_deep_reflect(pool, gw) -> None:
    """验收 1 用例一：importance_acc ≥20 触发深度反思——2~3 条洞察（重要性 7~9）+ acc 清零。"""
    refl = _reflector(pool, gw)
    await _feed(pool, gw, "A13", [6, 7, 8])  # Σ=21 ≥20
    baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    await refl.hook("A13", decision=None)
    assert "reflection" in gw.calls, "深度反思走明星层模型（task_type='reflection'）"
    mems = await pool.fetch(
        "SELECT kind, importance, content FROM memories WHERE agent_id='A13' AND kind='reflection' ORDER BY id"
    )
    assert 2 <= len(mems) <= 3, "2~3 条洞察"
    assert all(7 <= m["importance"] <= 9 for m in mems), "洞察重要性区间 7~9（04 §7.2）"
    ev = await pool.fetchrow("SELECT type, visibility, trigger, payload FROM events WHERE seq > $1 AND type='agent.reflection'", baseline)
    assert ev is not None, "双通道②：display-only 事件同时落库"
    assert ev["visibility"] == "public" and ev["trigger"] == "autonomous"
    assert set(json.loads(ev["payload"]).keys()) == {"text_display"}, "payload 白名单仅 text_display（06 §1.2）"
    mood = json.loads(await pool.fetchval("SELECT mood::text FROM agents WHERE id='A13'"))
    assert mood["importance_acc"] == 0, "触发后 acc 清零"
    # 反思类记忆不回喂 acc：再次 hook 不重复触发
    calls_before = list(gw.calls)
    await refl.hook("A13", decision=None)
    assert gw.calls.count("reflection") == calls_before.count("reflection"), "反思/摘要类不回喂 acc（防自激，D19）"


async def test_no_trigger_below_threshold(pool, gw) -> None:
    refl = _reflector(pool, gw)
    await _feed(pool, gw, "A13", [6, 7])  # Σ=13 <20
    await refl.hook("A13", decision=None)
    assert "reflection" not in gw.calls
    mood = json.loads(await pool.fetchval("SELECT mood::text FROM agents WHERE id='A13'"))
    assert mood["importance_acc"] == 13, "未达阈继续累计（mood JSON 持久化）"


async def test_throttle_state_seam(pool, gw) -> None:
    """降速读取点：ThrottleState 协议可替换（04 §8.4 ④ 降速档 20→35；T-OPS-02 接线 seam）。"""

    class Throttled35(NullThrottleState):
        def reflection_threshold(self, default: int) -> int:
            return 35

    refl = _reflector(pool, gw, throttle=Throttled35())
    await _feed(pool, gw, "A13", [10, 10, 10])  # Σ=30：默认阈 20 会触发，降速阈 35 不触发
    await refl.hook("A13", decision=None)
    assert "reflection" not in gw.calls, "降速档阈值运行时读取 ThrottleState"
    await _feed(pool, gw, "A14", [0 + 6])  # A14 无关数据
    await _feed(pool, gw, "A13", [6])      # A13 Σ=36 ≥35
    await refl.hook("A13", decision=None)
    assert "reflection" in gw.calls, "超过降速阈仍触发"


async def test_daily_fallback_idempotent(pool, gw) -> None:
    """验收 1 用例二：每日兜底——23:00 批量点强制一次廉价摘要式反思，按 agent 日幂等。"""
    refl = _reflector(pool, gw)
    await _feed(pool, gw, "A13", [4, 4])
    at_23 = T0.replace(hour=23, minute=5)
    done = await refl.run_due_daily_fallbacks(["A13", "A14"], at_23, rng_seed=1)
    assert done == ["A13", "A14"], "23:00 批量反思点全员兜底（A14 无当日记忆也兜底，防平淡期僵化）"
    assert "bgsummary" in gw.calls, "兜底走次要层档位摘要模型"
    mem = await pool.fetchrow("SELECT kind, importance, content FROM memories WHERE agent_id='A13' AND kind='reflection'")
    assert mem is not None and mem["importance"] == FALLBACK_IMPORTANCE
    again = await refl.run_due_daily_fallbacks(["A13", "A14"], at_23 + dt.timedelta(minutes=30), rng_seed=2)
    assert again == [], "当日已兜底不重复"
    before = await pool.fetchval("SELECT count(*) FROM events WHERE type='agent.reflection' AND 'A13' = ANY(actors)")
    nxt = await refl.run_due_daily_fallbacks(["A13"], at_23 + dt.timedelta(days=1), rng_seed=3)
    assert nxt == ["A13"], "次日再次兜底"
    after = await pool.fetchval("SELECT count(*) FROM events WHERE type='agent.reflection' AND 'A13' = ANY(actors)")
    assert after == before + 1
    early = await refl.run_due_daily_fallbacks(["A13"], at_23.replace(hour=22) + dt.timedelta(days=2), rng_seed=4)
    assert early == [], "未到 23:00 不兜底"


async def test_dual_channel_paired_sql(pool, gw) -> None:
    """验收 2 SQL 形态：一次深度反思后 reflection 记忆 ≥1 且事件 payload 仅含 text_display 键。"""
    refl = _reflector(pool, gw)
    await _feed(pool, gw, "A13", [10, 10])
    await refl.hook("A13", decision=None)
    n = await pool.fetchval(
        """
        SELECT count(*) FROM events e JOIN memories m ON m.agent_id = ANY(e.actors)
        WHERE e.type='agent.reflection' AND m.kind='reflection' AND 'A13' = ANY(e.actors)
        """
    )
    assert n >= 1
    bad = await pool.fetchval(
        "SELECT count(*) FROM events WHERE type='agent.reflection' AND payload <> jsonb_build_object('text_display', payload->'text_display')"
    )
    assert bad == 0, "agent.reflection payload 白名单仅 text_display"


async def test_think_light_reflection_no_event(pool, gw) -> None:
    """验收 1 用例四：think=轻反思档——低重要性 reflection 记忆不产 agent.reflection 事件，不回喂 acc。"""
    refl = _reflector(pool, gw)
    await store.insert_memory(
        pool, gw, agent_id="A13", sim_time=T0, kind="reflection",
        content="走神了一瞬（轻反思）", importance=2, rng_seed=1,
    )
    baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    await refl.hook("A13", decision=None)
    n = await pool.fetchval("SELECT count(*) FROM events WHERE seq > $1 AND type='agent.reflection'", baseline)
    assert n == 0, "think 轻反思不产 agent.reflection 事件（04 §6.2 think 行）"
    mood = json.loads(await pool.fetchval("SELECT mood::text FROM agents WHERE id='A13'"))
    assert mood["importance_acc"] == 0, "反思类记忆不计入 importance_acc（D19）"


async def test_write_generated_reflection_tiers(pool, gw) -> None:
    """T-REL-03 挂接：轻档（换策略，重要性 5）不产事件；深度档（放弃，重要性 ≥8）产 display-only 事件。"""
    refl = _reflector(pool, gw)
    await _feed(pool, gw, "A13", [3])
    baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    await refl.write_generated_reflection("A13", "换个策略试试", importance=5, sim_now=T0, rng_seed=1)
    n = await pool.fetchval("SELECT count(*) FROM events WHERE seq > $1 AND type='agent.reflection'", baseline)
    assert n == 0
    await refl.write_generated_reflection("A13", "我放弃了，这件事现在做不到", importance=8, sim_now=T0, rng_seed=2)
    ev = await pool.fetchrow("SELECT payload FROM events WHERE seq > $1 AND type='agent.reflection'", baseline)
    assert ev is not None and set(json.loads(ev["payload"]).keys()) == {"text_display"}
