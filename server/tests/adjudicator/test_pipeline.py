"""T-ADJ-02 六步裁决管道骨架验收。

口径：02 文档 T-ADJ-02 验收 1~3（六步顺序/解析失败重试再降级 think/同 tick 并发决策串行落库 +
test_pipeline_injected_mock_deterministic 同 rng_seed 两次逐字节一致）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from worldsim.adjudicator.pipeline import FORMAT_FIX_SUFFIX, Pipeline
from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 0, 0, 0, tzinfo=LOCAL_TZ)  # 叙事起点（周一 00:00）

PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"
DDL_DIR = Path(__file__).resolve().parents[2] / "ddl"

PIPE_AGENT_IDS = ["A10", "A11", "A12"]

_AGENT_ROW = """
    INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
    VALUES ($1, $2, 'F', 25, 'star', $3::jsonb, $4::jsonb, 100000, $5)
    ON CONFLICT (id) DO NOTHING
"""


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )


def _psql_file(path: Path, db: str) -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-f", str(path)],
        check=True, capture_output=True, text=True,
    )


def _build_db(name: str) -> str:
    """私有 scratch 库（schema_v1 + 3 名测试 agent），供确定性对拍双库隔离。"""
    _psql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    _psql(f"CREATE DATABASE {name}")
    _psql("CREATE SCHEMA IF NOT EXISTS partman", db=name)
    _psql("CREATE EXTENSION IF NOT EXISTS vector", db=name)
    _psql("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman", db=name)
    _psql_file(DDL_DIR / "schema_v1.sql", name)
    return f"postgresql:///{name}?host={SOCKET_DIR}"


def _drop_db(name: str) -> None:
    _psql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


async def _insert_agents(pool) -> None:
    for i, aid in enumerate(PIPE_AGENT_IDS):
        await pool.execute(
            _AGENT_ROW, aid, f"测试{aid}",
            json.dumps({"agent_id": aid, "name": f"测试{aid}"}, ensure_ascii=False),
            json.dumps({"energy": 70, "hunger": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70}),
            f"apt.L2.2{i:02d}",
        )


class StubGateway:
    """可编程网关桩：队列化返回文本，记录调用（含并发计数）。"""

    def __init__(self, texts: list[str] | None = None, delay: float = 0.0) -> None:
        self.texts = texts or [json.dumps(
            {"intent": "想想事情", "action": {"type": "think", "args": {"topic_hint": "测试"}}, "emotion_delta": {}},
            ensure_ascii=False,
        )]
        self.delay = delay
        self.calls: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def gen_params(self, task_type: str) -> dict:
        return {}

    async def chat(self, task_type, messages, gen_params=None, *, seed=None, agent_id=None, sim_time=None) -> ChatResult:
        self.calls.append({"task_type": task_type, "messages": messages, "seed": seed, "agent_id": agent_id})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.in_flight -= 1
        text = self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]
        return ChatResult(text=text, prompt_tokens=10, completion_tokens=5, latency_ms=1,
                          request_id="stub", provider="stub", model="stub-1")

    async def embed(self, texts, *, seed=None, agent_id=None, sim_time=None):
        return await MockProvider().embed(texts, seed=seed)


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    await _insert_agents(p)
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM memories WHERE agent_id = ANY($1)", PIPE_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", PIPE_AGENT_IDS)
        finally:
            await conn.close()


async def test_six_step_order(pool) -> None:
    """验收 1 用例一：step1 感知 → step2 检索 → step3 决策 → step4 校验 → step5 落库 → step6 反思。"""
    order: list[str] = []
    gw = StubGateway()

    async def obs_builder(agent_id: str, sim_now):
        order.append("step1_observe")
        return await Pipeline(pool, gw)._build_obs(agent_id, sim_now)

    async def retrieve(agent_id, obs):
        order.append("step2_retrieve")
        return ["记忆样例"]

    orig_chat = gw.chat

    async def chat_marked(*args, **kwargs):
        order.append("step3_decide")
        return await orig_chat(*args, **kwargs)

    gw.chat = chat_marked  # type: ignore[assignment]

    def validate(obs, action_type, args):
        order.append("step4_validate")
        return True, None

    async def after_settle(decision, seqs):
        order.append("step5_settle")
        assert len(seqs) == 1

    async def reflect(agent_id, decision):
        order.append("step6_reflect")

    pipe = Pipeline(pool, gw, retrieve=retrieve, validate=validate,
                    after_settle=after_settle, reflect=reflect, obs_builder=obs_builder)
    seqs = await pipe.run_tick(tick=0, sim_now=T0, agent_ids=["A10"], rng_seed=0)
    assert order == ["step1_observe", "step2_retrieve", "step3_decide",
                     "step4_validate", "step5_settle", "step6_reflect"]
    assert len(seqs) == 1
    assert gw.calls and gw.calls[0]["task_type"] == "star_decision"


async def test_parse_failure_retry_then_degrade_think(pool) -> None:
    """验收 1 用例二：解析失败 → 重试 1 次（追加格式纠错后缀）→ 仍失败记 think（"走神了"）并留痕。"""
    gw = StubGateway(texts=["这不是 JSON", "仍然不是 JSON"])
    settled: list = []

    async def after_settle(decision, seqs):
        settled.append(decision)

    pipe = Pipeline(pool, gw, after_settle=after_settle)
    baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    await pipe.run_tick(tick=1, sim_now=T0, agent_ids=["A11"], rng_seed=1)
    assert len(gw.calls) == 2, "恰好 1 次重试"
    assert gw.calls[1]["messages"][-1]["content"] == FORMAT_FIX_SUFFIX, "重试追加格式纠错后缀"
    assert settled[0].degraded is True and settled[0].action_type == "think"
    assert settled[0].intent == "走神了" and settled[0].attempts == 2
    rows = await pool.fetch(
        "SELECT type, payload FROM events WHERE seq > $1 AND 'A11' = ANY(actors) ORDER BY seq", baseline
    )
    assert [r["type"] for r in rows] == ["agent.think"], "降级落 think 事件"
    mem = await pool.fetchrow(
        "SELECT kind, content FROM memories WHERE agent_id='A11' ORDER BY id DESC LIMIT 1"
    )
    assert mem["kind"] == "reflection" and "走神了" in mem["content"], "降级留痕进记忆"


async def test_concurrent_decisions_serial_writeback(pool) -> None:
    """验收 1 用例三：同 tick LLM 并发决策（乱序完成），结果按队列顺序串行落库。"""

    class BarrierGateway(StubGateway):
        """确定性屏障：3 个 chat 全部到达后才放行（乱序完成的并发证据）。"""

        def __init__(self) -> None:
            super().__init__()
            self.started = 0
            self.release = asyncio.Event()

        async def chat(self, task_type, messages, gen_params=None, *, seed=None, agent_id=None, sim_time=None):
            self.calls.append({"agent_id": agent_id})
            self.started += 1
            self.max_in_flight = max(self.max_in_flight, self.started - self.calls.count("done"))
            if self.started >= 3:
                self.release.set()
            await asyncio.wait_for(self.release.wait(), timeout=5)
            # 完成顺序刻意乱序：A10 最后放行
            if agent_id == "A10":
                await asyncio.sleep(0.01)
            return ChatResult(text=self.texts[0], prompt_tokens=1, completion_tokens=1,
                              latency_ms=1, request_id="s", provider="stub", model="s")

    gw = BarrierGateway()
    pipe = Pipeline(pool, gw)
    baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    seqs = await pipe.run_tick(tick=2, sim_now=T0, agent_ids=["A10", "A11", "A12"], rng_seed=2)
    assert gw.started == 3, "3 个决策并发发出（屏障放行前全部到达）"
    rows = await pool.fetch("SELECT seq, actors FROM events WHERE seq > $1 ORDER BY seq", baseline)
    actor_order = [r["actors"][0] for r in rows]
    assert actor_order == ["A10", "A11", "A12"], "落库序 = 队列序而非完成序"
    assert seqs == [r["seq"] for r in rows]


async def test_pipeline_injected_mock_deterministic() -> None:
    """验收 2 指定用例：注入 03 T-LLM-02 mock，同 rng_seed 两次跑同批 tick，事件序列逐字节一致。"""
    from worldsim.llm_gateway import LLMGateway

    async def run_once(name: str) -> list[tuple]:
        dsn = _build_db(name)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        try:
            await _insert_agents(pool)
            gw = LLMGateway(pool, {}, providers={"mock": MockProvider()}, default_provider="mock")
            pipe = Pipeline(pool, gw)
            for tick in range(6):  # 6 tick = 30 sim min
                sim_now = T0 + dt.timedelta(seconds=tick * 300)
                await pipe.run_tick(tick=tick, sim_now=sim_now, agent_ids=list(PIPE_AGENT_IDS), rng_seed=tick)
            rows = await pool.fetch(
                """
                SELECT tick, sim_time, type, source, trigger, location_id, actors,
                       rng_seed, visibility, payload::text
                FROM events ORDER BY seq
                """
            )
            mems = await pool.fetch(
                "SELECT agent_id, kind, content, importance FROM memories ORDER BY id"
            )
            agents = await pool.fetch("SELECT id, position FROM agents ORDER BY id")
            return (
                [tuple(r.values()) for r in rows],
                [tuple(r.values()) for r in mems],
                [tuple(r.values()) for r in agents],
            )
        finally:
            await pool.close()
            _drop_db(name)

    run_a = await run_once("worldsim_pipe_a")
    run_b = await run_once("worldsim_pipe_b")
    assert run_a == run_b, "同 rng_seed 两次跑同批 tick，事件/记忆/终态必须逐字节一致"
    events_a = run_a[0]
    assert len(events_a) > 0
    assert all(e[6] is not None for e in events_a), "events.rng_seed 留痕（04 §5.2）"
