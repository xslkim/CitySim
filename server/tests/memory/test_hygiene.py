"""T-MEM-03 记忆膨胀治理验收。

口径：02 文档 T-MEM-03 验收 1~2（合并置归档+生成 summary / 归档阈值 / 归档不进检索 +
治理后 archived AND kind='event' 单调不减且 memories 行数不减少（无 DELETE））。
"""

from __future__ import annotations

import datetime as dt
import json
import types

import asyncpg
import pytest
import pytest_asyncio

from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.memory import store
from worldsim.memory.hygiene import (
    ARCHIVE_AGE_DAYS,
    MERGE_AGE_DAYS,
    MemoryHygiene,
)
from worldsim.memory.retrieval import quota_retrieve
from worldsim.time_engine.batch_hooks import (
    BatchContext,
    clear_batch_hooks,
    registered_hooks,
    run_batch_hooks,
)
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 4, 0, 0, tzinfo=LOCAL_TZ)  # 凌晨 batch 段时刻
HYG_AGENT_IDS = ["A18", "A19"]


class HygStubGateway:
    def __init__(self) -> None:
        self.chat_calls = 0
        self._mock = MockProvider()

    def gen_params(self, task_type: str) -> dict:
        return {}

    async def chat(self, task_type, messages, gen_params=None, *, seed=None, agent_id=None, sim_time=None) -> ChatResult:
        self.chat_calls += 1
        return ChatResult(
            text=json.dumps({"diary": "那段日子的事，现在想想也就那样。", "needs_delta": {}, "mood": "平稳", "tomorrow_plan": "继续"}, ensure_ascii=False),
            prompt_tokens=10, completion_tokens=5, latency_ms=1, request_id="stub", provider="stub", model="stub-1",
        )

    async def embed(self, texts, *, seed=None, agent_id=None, sim_time=None):
        return await self._mock.embed(texts, seed=seed)


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in HYG_AGENT_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, 'star', '{}'::jsonb, '{}'::jsonb, 100000, 'apt.lobby')
            ON CONFLICT (id) DO NOTHING
            """,
            aid, f"测试{aid}",
        )
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM memories WHERE agent_id = ANY($1)", HYG_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", HYG_AGENT_IDS)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def gw() -> HygStubGateway:
    return HygStubGateway()


async def test_merge_archives_and_writes_summary(pool, gw) -> None:
    """验收 1 用例一：合并置归档 + 生成 summary；memories 行数不减少（无 DELETE）。"""
    old = T0 - dt.timedelta(days=MERGE_AGE_DAYS + 1)
    for i, imp in enumerate((2, 3, 4)):  # 3 条符合合并条件（kind=event、imp≤4、7 天前）
        await store.insert_memory(pool, gw, agent_id="A18", sim_time=old + dt.timedelta(hours=i), kind="event",
                                  content=f"旧片段{i}", importance=imp, rng_seed=i)
    keep_high = await store.insert_memory(pool, gw, agent_id="A18", sim_time=old, kind="event",
                                          content="重要的旧片段", importance=6, rng_seed=9)  # imp>4 不合并
    keep_recent = await store.insert_memory(pool, gw, agent_id="A18", sim_time=T0 - dt.timedelta(days=1),
                                            kind="event", content="近片段", importance=2, rng_seed=10)  # 不足 7 天不合并
    before = await pool.fetchval("SELECT count(*) FROM memories WHERE agent_id='A18'")
    archived_before = await pool.fetchval("SELECT count(*) FROM memories WHERE archived AND kind='event'")

    hyg = MemoryHygiene(pool, gw)
    merged = await hyg.merge_summaries(sim_now=T0, agent_ids=["A18"], rng_seed=1)
    assert merged == {"A18": 3}
    assert await pool.fetchval("SELECT count(*) FROM memories WHERE agent_id='A18'") == before + 1, "summary 新增 1 条、原条目置位不删"
    archived_after = await pool.fetchval("SELECT count(*) FROM memories WHERE archived AND kind='event'")
    assert archived_after == archived_before + 3, "archived 单调不减（验收 2 SQL 口径）"
    summary = await pool.fetchrow("SELECT kind, importance, content, archived FROM memories WHERE agent_id='A18' AND kind='summary'")
    assert summary is not None and not summary["archived"] and summary["content"]
    assert await pool.fetchval("SELECT archived FROM memories WHERE id=$1", keep_high) is False
    assert await pool.fetchval("SELECT archived FROM memories WHERE id=$1", keep_recent) is False


async def test_merge_requires_two(pool, gw) -> None:
    """不足 2 条不合并（单条无"合并"语义，D19）。"""
    old = T0 - dt.timedelta(days=MERGE_AGE_DAYS + 2)
    await store.insert_memory(pool, gw, agent_id="A18", sim_time=old, kind="event", content="孤本旧片段", importance=2, rng_seed=1)
    hyg = MemoryHygiene(pool, gw)
    merged = await hyg.merge_summaries(sim_now=T0, agent_ids=["A18"], rng_seed=1)
    assert merged == {}
    assert await pool.fetchval("SELECT count(*) FROM memories WHERE agent_id='A18' AND archived") == 0


async def test_archive_threshold(pool, gw) -> None:
    """验收 1 用例二：归档阈值——importance ≤3 且 30 模拟天前 → archived；两条件缺一不可。"""
    very_old = T0 - dt.timedelta(days=ARCHIVE_AGE_DAYS + 1)
    hit = await store.insert_memory(pool, gw, agent_id="A18", sim_time=very_old, kind="event", content="该归档", importance=3, rng_seed=1)
    keep_imp = await store.insert_memory(pool, gw, agent_id="A18", sim_time=very_old, kind="event", content="重要性 4 不归档", importance=4, rng_seed=2)
    keep_age = await store.insert_memory(pool, gw, agent_id="A18", sim_time=T0 - dt.timedelta(days=10), kind="event", content="不足 30 天不归档", importance=2, rng_seed=3)
    hyg = MemoryHygiene(pool, gw)
    n = await hyg.archive_old(sim_now=T0)
    assert n == 1
    flags = {r["id"]: r["archived"] for r in await pool.fetch("SELECT id, archived FROM memories WHERE id = ANY($1::bigint[])", [hit, keep_imp, keep_age])}
    assert flags == {hit: True, keep_imp: False, keep_age: False}


async def test_archived_not_in_retrieval(pool, gw) -> None:
    """验收 1 用例三：归档集不进配额检索（04 §7.3；T-MEM-01 消费口径）。"""
    very_old = T0 - dt.timedelta(days=ARCHIVE_AGE_DAYS + 3)
    mid = await store.insert_memory(pool, gw, agent_id="A19", sim_time=very_old, kind="event", content="被遗忘的片段", importance=2, rng_seed=1)
    await store.insert_memory(pool, gw, agent_id="A19", sim_time=T0, kind="event", content="新片段", importance=5, rng_seed=2)
    hyg = MemoryHygiene(pool, gw)
    await hyg.archive_old(sim_now=T0)
    rows = await quota_retrieve(pool, gw, agent_id="A19", query_text="片段", sim_now=T0)
    assert mid not in {r["id"] for r in rows}, "归档后不进三桶"


async def test_reindex_triggers(pool, gw) -> None:
    """索引重建：force / 行数阈 / 召回抽检 <85% 三触发；无触发返回 False。"""
    hyg = MemoryHygiene(pool, gw)
    assert await hyg.maybe_reindex() is False
    assert await hyg.maybe_reindex(recall_hit_rate=0.5) is True, "召回抽检退化触发提前重建"
    assert await hyg.maybe_reindex(force=True) is True, "周常/运维强制重建"


async def test_batch_hook_mount(pool, gw) -> None:
    """摘要合并经 T-TIME-03 回调注册表挂载（唯一挂载点）：注册名 memory.merge，钩子内执行合并。"""
    old = T0 - dt.timedelta(days=MERGE_AGE_DAYS + 1)
    for i in range(2):
        await store.insert_memory(pool, gw, agent_id="A18", sim_time=old + dt.timedelta(hours=i), kind="event",
                                  content=f"钩子合并片段{i}", importance=2, rng_seed=i)
    hyg = MemoryHygiene(pool, gw)
    clear_batch_hooks()
    try:
        hyg.register_batch_hook()
        assert "memory.merge" in registered_hooks()
        clock_stub = types.SimpleNamespace(now_sim=lambda: T0)
        await run_batch_hooks(BatchContext(sim_hours=8.0, agent_ids=("A18",), clock=clock_stub))
        assert await pool.fetchval("SELECT count(*) FROM memories WHERE agent_id='A18' AND archived") == 2
    finally:
        clear_batch_hooks()
