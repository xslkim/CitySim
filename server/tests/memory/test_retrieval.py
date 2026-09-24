"""T-MEM-01 配额制记忆检索验收。

口径：02 文档 T-MEM-01 验收 1（三桶配额各自封顶 / 跨桶去重 / 归档排除 / 排序 bucket 优先 +
test_quota_buckets 30 条记忆返回 ≤ N+M+K 且 recent/important 桶命中数不超配额）。
配额 N/M/K=10/5/10 从 models.yaml thresholds.retrieval_quota 读值（04 §7.1；00 §7 DoD 6）。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.llm_gateway import LLMGateway
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.memory import store
from worldsim.memory.retrieval import quota_retrieve
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=LOCAL_TZ)
RET_AGENT_IDS = ["A16", "A17"]
QUOTA = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "models.yaml").read_text(encoding="utf-8"))["thresholds"]["retrieval_quota"]


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in RET_AGENT_IDS:
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
            await conn.execute("DELETE FROM memories WHERE agent_id = ANY($1)", RET_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", RET_AGENT_IDS)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def gateway():
    return LLMGateway(None, {}, providers={"mock": MockProvider()}, default_provider="mock")


async def _seed_30(pool, gateway) -> dict[str, int | list[int]]:
    """30 条记忆（确定性布局）：

    - 10 条逐模拟小时（最新 → recent 桶，低重要性 1~3）；
    - 5 条 5 模拟天前（近 7 日内、非最新 10 条、高重要性 8~10 → important 桶确定命中）；
    - 14 条 8~22 模拟天前（7 日窗外、importance 10 → 只进 semantic）；
    - 1 条 30 分钟前 importance 10（同时在 recent 与 important → 去重用例）。
    """
    out: dict[str, int | list[int]] = {"mid": [], "older": []}
    for i in range(10):
        await store.insert_memory(
            pool, gateway, agent_id="A16", sim_time=T0 - dt.timedelta(hours=i + 1),
            kind="event", content=f"逐时记忆{i}", importance=1 + (i % 3), rng_seed=i,
        )
    overlap = await store.insert_memory(
        pool, gateway, agent_id="A16", sim_time=T0 - dt.timedelta(minutes=30),
        kind="event", content="既新又重要的重叠记忆", importance=10, rng_seed=99,
    )
    out["overlap"] = overlap
    for i, imp in enumerate((9, 9, 10, 10, 8)):
        mid = await store.insert_memory(
            pool, gateway, agent_id="A16", sim_time=T0 - dt.timedelta(days=5, hours=i + 1),
            kind="event", content=f"近段高重要{i}", importance=imp, rng_seed=50 + i,
        )
        out["mid"].append(mid)
    for i in range(14):
        oid = await store.insert_memory(
            pool, gateway, agent_id="A16", sim_time=T0 - dt.timedelta(days=8 + i),
            kind="event", content=f"旧记忆{i}", importance=10, rng_seed=100 + i,
        )
        out["older"].append(oid)
    await store.insert_memory(
        pool, gateway, agent_id="A17", sim_time=T0, kind="event", content="别人的记忆", importance=10, rng_seed=0,
    )
    return out


async def test_quota_buckets(pool, gateway) -> None:
    """验收 2 指定用例：30 条记忆，返回 ≤ N+M+K 且 recent/important 桶命中数不超配额。"""
    await _seed_30(pool, gateway)
    n_quota, m_quota, k_quota = QUOTA["N"], QUOTA["M"], QUOTA["K"]
    rows = await quota_retrieve(pool, gateway, agent_id="A16", query_text="厨房 闲聊", sim_now=T0, quota=QUOTA)
    assert len(rows) <= n_quota + m_quota + k_quota, "总量封顶 N+M+K"
    by_bucket: dict[int, list] = {1: [], 2: [], 3: []}
    for r in rows:
        by_bucket[r["bucket"]].append(r)
    assert len(by_bucket[1]) <= n_quota, "recent 桶 ≤ N"
    assert len(by_bucket[2]) <= m_quota, "important 桶 ≤ M"
    assert len(by_bucket[3]) <= k_quota, "semantic 桶 ≤ K"
    # recent CTE = 10 条（顶满），important CTE = 5 条（含重叠记忆），重叠去重后合计恰 14 条
    assert len(by_bucket[1]) + len(by_bucket[2]) == n_quota + m_quota - 1
    assert all(r["agent_id"] != "A17" for r in rows) if rows else True
    important_rows = by_bucket[2]
    assert all(r["sim_time"] > T0 - dt.timedelta(days=7) for r in important_rows), "important 限近 7 模拟日"
    assert all(r["importance"] >= 8 for r in important_rows), "近 7 日内按 importance DESC 取"


async def test_cross_bucket_dedup(pool, gateway) -> None:
    """跨桶去重：同一条记忆同时满足 recent 与 important 时只出现一次。"""
    ids = await _seed_30(pool, gateway)
    rows = await quota_retrieve(pool, gateway, agent_id="A16", query_text="测试", sim_now=T0, quota=QUOTA)
    got = [r["id"] for r in rows]
    assert len(got) == len(set(got)), "DISTINCT ON (id) 合并去重"
    assert got.count(ids["overlap"]) == 1, "既新又重要的记忆只出现一次"


async def test_archived_excluded(pool, gateway) -> None:
    """归档排除（04 §7.3）：archived=true 的记忆即使最新/高重要性/语义近也不进检索。"""
    ids = await _seed_30(pool, gateway)
    await pool.execute("UPDATE memories SET archived=true WHERE id=$1", ids["overlap"])
    rows = await quota_retrieve(pool, gateway, agent_id="A16", query_text="测试", sim_now=T0, quota=QUOTA)
    assert ids["overlap"] not in {r["id"] for r in rows}
    assert all(r["content"] != "别人的记忆" for r in rows)


async def test_bucket_ordering(pool, gateway) -> None:
    """排序（bucket 优先）：结果按 bucket 升序、桶内 sim_time 降序。"""
    await _seed_30(pool, gateway)
    rows = await quota_retrieve(pool, gateway, agent_id="A16", query_text="测试", sim_now=T0, quota=QUOTA)
    keys = [(r["bucket"],) for r in rows]
    assert keys == sorted(keys), "bucket 升序"
    for bucket in (1, 2, 3):
        times = [r["sim_time"] for r in rows if r["bucket"] == bucket]
        assert times == sorted(times, reverse=True), f"bucket={bucket} 内 sim_time 降序"


async def test_quota_tunable(pool, gateway) -> None:
    """配额可调（04 §7.1）：缩小 N/M/K 后各桶随之封顶。"""
    await _seed_30(pool, gateway)
    small = {"N": 3, "M": 2, "K": 4}
    rows = await quota_retrieve(pool, gateway, agent_id="A16", query_text="测试", sim_now=T0, quota=small)
    by_bucket: dict[int, int] = {}
    for r in rows:
        by_bucket[r["bucket"]] = by_bucket.get(r["bucket"], 0) + 1
    assert by_bucket.get(1, 0) <= small["N"] and by_bucket.get(2, 0) <= small["M"] and by_bucket.get(3, 0) <= small["K"]
    assert len(rows) <= sum(small.values())
