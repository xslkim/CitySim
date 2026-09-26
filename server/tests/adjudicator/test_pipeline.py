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
    # R1 #2：默认在飞并发闸 = 2（免费档 429 降损）；本用例验证"并发完成、串行落库"语义，显式放回 3
    pipe = Pipeline(pool, gw, decide_concurrency=3)
    baseline = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
    seqs = await pipe.run_tick(tick=2, sim_now=T0, agent_ids=["A10", "A11", "A12"], rng_seed=2)
    assert gw.started == 3, "3 个决策并发发出（屏障放行前全部到达）"
    rows = await pool.fetch("SELECT seq, actors FROM events WHERE seq > $1 ORDER BY seq", baseline)
    actor_order = [r["actors"][0] for r in rows]
    assert actor_order == ["A10", "A11", "A12"], "落库序 = 队列序而非完成序"
    assert seqs == [r["seq"] for r in rows]


async def test_decide_concurrency_cap_r1(pool) -> None:
    """R1 #2 验收：decide_concurrency=1 → 同 tick 决策调用在飞并发恒 ≤1（免费档错峰降损）。"""
    gw = StubGateway(delay=0.02)
    pipe = Pipeline(pool, gw, decide_concurrency=1)
    await pipe.run_tick(tick=3, sim_now=T0, agent_ids=["A10", "A11", "A12"], rng_seed=3)
    assert gw.max_in_flight == 1, "并发闸=1 时决策须串行（错峰调度生效）"


async def test_parse_tolerant_of_fences_and_noise(pool) -> None:
    """R3 #1③：围栏/杂讯包裹的决策 JSON 不再被 schema 判失败（对话意图不被系统性误杀）。"""
    fenced = ('```json\n{"intent":"想找人聊聊","action":{"type":"chat",'
              '"args":{"target":"A12","mode":"small"}},"emotion_delta":{}}\n```')
    d = Pipeline._parse_decision("A10", fenced, 1)
    assert d is not None and d.action_type == "chat" and d.action_args["target"] == "A12"
    noisy = '好的，这是本拍决策：{"intent":"聊聊八卦","action":{"type":"gossip","args":{}}} 就按这个来。'
    d2 = Pipeline._parse_decision("A10", noisy, 1)
    assert d2 is not None and d2.action_type == "gossip" and d2.intent == "聊聊八卦"
    assert Pipeline._parse_decision("A10", "完全没有 JSON 对象", 1) is None


async def test_prompt_supplies_action_menu_and_social_context(pool) -> None:
    """R3 #1②：prompt 供给 19 动作菜单 + 关系网/债务/在场者（旧版只给 think/move 是哑火根因）。"""
    await pool.execute(
        "INSERT INTO relations (a_id,b_id,affinity,tension,labels) VALUES ('A10','A12',15,5,'{同事,暗恋}')"
    )
    await pool.execute(
        "INSERT INTO debts (a_id,b_id,amount_cents,due_sim,created_tick) VALUES ('A12','A10',30000,$1,0)",
        T0 + dt.timedelta(days=14),
    )
    try:
        gw = StubGateway()
        pipe = Pipeline(pool, gw)
        obs = await pipe._build_obs("A10", T0)
        assert any(r["peer"] == "A12" and int(r["affinity"]) == 15 for r in obs.relation_edges)
        assert any(r["role"] == "owe" and r["peer"] == "A12" for r in obs.debts)
        user = pipe._render_messages(obs, [])[1]["content"]
        for action in ("chat {target", "gossip {", "argue {", "borrow_money {", "confess {", "invite {"):
            assert action in user, f"动作菜单缺 {action}"
        assert "好感15/紧张5" in user and "同事,暗恋" in user, "关系边注入"
        assert "你欠" in user and "到期" in user, "债务注入"
        assert "在场者=" in user and "A12" in user, "在场者注入"
        assert "轻动作只可选" not in user, "旧版 think/move 限制必须移除"
    finally:  # 共享 test_db：清理本用例行，避免污染夹具的 agents 删除
        await pool.execute("DELETE FROM relations WHERE a_id='A10' AND b_id='A12'")
        await pool.execute("DELETE FROM debts WHERE a_id='A12' AND b_id='A10'")


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


@pytest.mark.asyncio
async def test_work_hours_open_office_commute(pool) -> None:
    """R3 #4③：工作日工作时段，公寓住户的可达候选含本部门工位区（corp 半边地图对决策可达）。"""
    from worldsim.relations.needs import NeedsEngine, load_needs_config
    from worldsim.world_agent.config import load_world_config

    await pool.execute(
        """
        INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents,
                            position, department)
        VALUES ('A20', '通勤测试', 'M', 25, 'star', '{}'::jsonb, '{}'::jsonb, 0, 'apt.L2.101', '技术部')
        ON CONFLICT (id) DO NOTHING
        """
    )
    try:
        needs = NeedsEngine(load_needs_config(str(DDL_DIR.parent / "config" / "needs.yaml")))
        pipe = Pipeline(pool, StubGateway(), load_world_config(), needs_engine=needs)
        work_time = T0.replace(hour=10, minute=0)   # 周一 10:00 工作时段
        obs = await pipe._build_obs("A20", work_time)
        assert "corp.tech" in obs.exits, "工作时段须开放部门工位区通勤候选"
        # 晚间 / 周末不开放（作息表口径，needs.yaml schedule.work）
        obs_evening = await pipe._build_obs("A20", T0.replace(hour=20, minute=0))
        assert "corp.tech" not in obs_evening.exits
        sat = T0 + dt.timedelta(days=5)  # 周六
        obs_weekend = await pipe._build_obs("A20", sat.replace(hour=10, minute=0))
        assert "corp.tech" not in obs_weekend.exits
    finally:
        await pool.execute("DELETE FROM agents WHERE id='A20'")
