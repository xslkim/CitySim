"""T-DIR-05 编剧 K3 复核与 director.grade_revise 验收（04 文档 T-DIR-05 验收 1~5）。

mock/桩 K3 先行（真接入随 M2 路由切换）；每日上调上限读 world.yaml director.revise 段。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import FakeClock
from worldsim.llm_gateway import ChainExhausted, LLMGateway
from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.director.intervene import intervention_rate_7d
from worldsim.world_agent.director.review import ReviewEngine, effective_grade

_DB = "worldsim_dir_review"
DAY = dt.date(2028, 5, 15)
NOW = dt.datetime(2028, 5, 16, 0, 30, tzinfo=LOCAL_TZ)  # 凌晨 batch 段后（D-19）


class ScriptedProvider:
    """脚本化 K3 桩：返回预设 revises JSON（chat 协议）。"""

    name = "scripted"

    def __init__(self, revises: list[dict] | None = None, *, fail: bool = False) -> None:
        self._revises = revises or []
        self._fail = fail
        self.calls = 0

    async def chat(self, task_type, messages, gen_params=None, *, seed=None) -> ChatResult:
        self.calls += 1
        if self._fail:
            raise ChainExhausted("director", "queue_retry")
        text = json.dumps({"note": "终审", "revises": self._revises}, ensure_ascii=False)
        return ChatResult(text=text, prompt_tokens=1, completion_tokens=1, latency_ms=1,
                          request_id="scripted", provider=self.name, model="scripted-k3")


def _p(row) -> dict:
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])


@pytest.fixture(scope="module")
def rv_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


async def _setup(dsn, revises=None, *, fail=False, registry=None, call_factor=None):
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    clock = FakeClock(NOW)
    provider = ScriptedProvider(revises, fail=fail)
    gw = LLMGateway(pool, {}, providers={"scripted": provider, "mock": MockProvider()},
                    default_provider="scripted")
    from worldsim.world_agent.config import load_world_config

    revise_cfg = load_world_config()["director"]["revise"]
    eng = ReviewEngine(pool, gw, clock, revise_cfg=revise_cfg, registry=registry,
                       call_factor=call_factor)
    return eng, pool, clock, provider


async def _insert_graded(pool, grade: str, *, day: dt.date = DAY, n: int = 1) -> list[int]:
    seqs = []
    for _ in range(n):
        seqs.append(await pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload, ui)
            VALUES (0, $1, 'dialogue.chat', 'agent:A01', 'autonomous', 'public', '{}'::jsonb, $2::jsonb)
            RETURNING seq
            """, dt.datetime.combine(day, dt.time(20, 0), tzinfo=LOCAL_TZ),
            json.dumps({"grade": grade})))
    return seqs


@pytest.mark.asyncio
async def test_revise_event_payload(rv_dsn) -> None:
    """验收 1：mock K3 输出 → 事件三键逐字、target_seq 匹配 ^[0-9]+$（非 'e<seq>' 形态）。"""
    seqs = await _insert_graded_async(rv_dsn)
    target = seqs[0]
    eng, pool, clock, provider = await _setup(rv_dsn, [{"target_seq": str(target), "new_grade": "A",
                                                        "reason": "张力完整"}])
    try:
        stats = await eng.run_daily_review(DAY)
        assert stats["ups"] == 1 and provider.calls == 1
        row = await pool.fetchrow(
            "SELECT type, source, trigger, payload FROM events WHERE type='director.grade_revise' ORDER BY seq DESC LIMIT 1")
        assert (row["type"], row["source"], row["trigger"]) == ("director.grade_revise", "director", "director")
        p = _p(row)
        assert set(p.keys()) == {"target_seq", "new_grade", "reason"}, "三键逐字 06 §1.2"
        import re

        assert re.match(r"^[0-9]+$", p["target_seq"]) and not p["target_seq"].startswith("e")
        assert p["target_seq"] == str(target) and p["new_grade"] == "A"
        # 同落 interventions 行（event_seq 互指）
        ev_seq = await pool.fetchval(
            "SELECT seq FROM events WHERE type='director.grade_revise' ORDER BY seq DESC LIMIT 1")
        assert await pool.fetchval("SELECT count(*) FROM interventions WHERE event_seq=$1", ev_seq) == 1
    finally:
        await pool.close()


async def _insert_graded_async(dsn) -> list[int]:
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        return await _insert_graded(pool, "B", n=3) + await _insert_graded(pool, "A", n=1)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_daily_up_cap(rv_dsn, caplog) -> None:
    """验收 2：超量上调建议 → 当日落库上调数 ≤ 配置上限，其余 WARN 留痕。"""
    day = dt.date(2028, 6, 20)
    import asyncpg

    pool0 = await asyncpg.create_pool(rv_dsn, min_size=1, max_size=1)
    seqs = await _insert_graded(pool0, "B", day=day, n=5)
    await pool0.close()
    revises = [{"target_seq": str(s), "new_grade": "A", "reason": "冲"} for s in seqs]
    eng, pool, clock, _ = await _setup(rv_dsn, revises)
    try:
        from worldsim.world_agent.config import load_world_config

        cap = int(load_world_config()["director"]["revise"]["daily_up_cap"])
        with caplog.at_level("WARNING"):
            stats = await eng.run_daily_review(day)
        assert stats["ups"] == cap
        n_up = await pool.fetchval(
            """SELECT count(*) FROM events e WHERE type='director.grade_revise' AND payload->>'new_grade'='A'
               AND EXISTS (SELECT 1 FROM events t WHERE t.seq = (e.payload->>'target_seq')::bigint
                           AND t.sim_time::date = $1)""", day)
        assert n_up <= cap
        assert stats["warned"] >= 1 and "上限" in caplog.text
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_no_update_on_events(rv_dsn) -> None:
    """验收 3：复核后原事件行 ui.grade 不变（append-only，04 §5.2）。"""
    day = dt.date(2028, 7, 22)
    import asyncpg

    pool0 = await asyncpg.create_pool(rv_dsn, min_size=1, max_size=1)
    (target,) = await _insert_graded(pool0, "B", day=day)
    await pool0.close()
    eng, pool, clock, _ = await _setup(rv_dsn, [{"target_seq": str(target), "new_grade": "A", "reason": "x"}])
    try:
        await eng.run_daily_review(day)
        assert await pool.fetchval("SELECT ui->>'grade' FROM events WHERE seq=$1", target) == "B"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_effective_grade(rv_dsn) -> None:
    """验收 4：有效 grade = 初值 ⊕ 最新复核（04 §6.6）：A+revise B → B；B+revise A → A；未复核 → 初值。"""
    day = dt.date(2028, 8, 25)
    import asyncpg

    pool0 = await asyncpg.create_pool(rv_dsn, min_size=1, max_size=1)
    (b_seq,) = await _insert_graded(pool0, "B", day=day)
    (a_seq,) = await _insert_graded(pool0, "A", day=day)
    (c_seq,) = await _insert_graded(pool0, "C", day=day)  # C 不在候选集
    await pool0.close()
    revises = [{"target_seq": str(b_seq), "new_grade": "A", "reason": "上调"},
               {"target_seq": str(a_seq), "new_grade": "B", "reason": "下调"}]
    eng, pool, clock, _ = await _setup(rv_dsn, revises)
    try:
        stats = await eng.run_daily_review(day)
        assert stats["ups"] == 1 and stats["downs"] == 1
        assert await effective_grade(pool, b_seq) == "A"
        assert await effective_grade(pool, a_seq) == "B"
        assert await effective_grade(pool, c_seq) == "C", "未复核 → 初值"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_intervention_accounting(rv_dsn) -> None:
    """验收 5：revise 事件计入 intervention_rate_7d()（trigger='director'，06 §1.1 红线 13）。"""
    day = dt.date(2028, 9, 26)
    import asyncpg

    pool0 = await asyncpg.create_pool(rv_dsn, min_size=1, max_size=1)
    (target,) = await _insert_graded(pool0, "B", day=day)
    await pool0.close()
    eng, pool, clock, _ = await _setup(rv_dsn, [{"target_seq": str(target), "new_grade": "A", "reason": "x"}])
    try:
        d0 = await pool.fetchval(
            "SELECT count(*) FROM events WHERE trigger='director' AND sim_time > $1",
            NOW - dt.timedelta(days=7))
        await eng.run_daily_review(day)
        d1 = await pool.fetchval(
            "SELECT count(*) FROM events WHERE trigger='director' AND sim_time > $1",
            NOW - dt.timedelta(days=7))
        assert d1 - d0 == 1
        rate = await intervention_rate_7d(pool, NOW)
        assert rate > 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_k3_failure_queued(rv_dsn) -> None:
    """K3 失败（ChainExhausted queue_retry，04 §8.1）：当日跳过 + WARN + review.pending 排队标记。"""
    day = dt.date(2028, 10, 28)
    import asyncpg

    pool0 = await asyncpg.create_pool(rv_dsn, min_size=1, max_size=1)
    await _insert_graded(pool0, "B", day=day)
    await pool0.close()
    eng, pool, clock, provider = await _setup(rv_dsn, fail=True)
    try:
        seq0 = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        stats = await eng.run_daily_review(day)
        assert stats["pending"] and stats["ups"] == 0
        pending = await eng._get("review.pending")
        assert any(p["day"] == day.isoformat() for p in pending)
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='director.grade_revise' AND seq > $1", seq0) == 0
    finally:
        await pool.close()
