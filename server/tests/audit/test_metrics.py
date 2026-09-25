"""T-AUD-08 健康度七指标验收（08 文档 T-AUD-08 验收 1~4；阈值读 health_thresholds.yaml，
测试不写字面量；口径 04 §10.2 / 有效 grade 04 §6.6 / 冲突边方向 01 §9）。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from tests.audit.conftest import SIM_NOW, close_pool, make_pool
from worldsim.audit import metrics as M

_DB_COUNTER = 0


async def _pool():
    global _DB_COUNTER
    _DB_COUNTER += 1
    return await make_pool(f"worldsim_audit_metrics{_DB_COUNTER}")


async def _insert(pool, type_, sim_time, *, grade=None, actors=(), payload=None,
                  trigger="autonomous", source="world", visibility="internal") -> int:
    ui = json.dumps({"grade": grade}) if grade else None
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload, ui)
        VALUES (0, $1, $2, $3, $4, $5::text[], $6, $7::jsonb, $8::jsonb) RETURNING seq
        """, sim_time, type_, source, trigger, list(actors), visibility,
        json.dumps(payload or {}), ui)


# ---- 各指标构造数据集 → 已知输出 -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_grade_gap_effective_grade() -> None:
    """A 级间隔 + test_grade_revise_overrides_initial：初值 A+revise B 后不再是 A（有效 grade 口径）。"""
    pool = await _pool()
    try:
        t0 = SIM_NOW - dt.timedelta(days=3)
        a1 = await _insert(pool, "dialogue.chat", t0, grade="A")
        a2 = await _insert(pool, "dialogue.chat", t0 + dt.timedelta(days=1), grade="A")
        a3 = await _insert(pool, "dialogue.chat", t0 + dt.timedelta(days=3), grade="A")
        gap = await M.a_grade_gap_days(pool, SIM_NOW)
        assert gap == pytest.approx(1.5)  # (1 + 2) / 2
        # a2 被复核下调为 B → 有效 A 级只剩 a1/a3 → 间隔 3 日
        await _insert(pool, "director.grade_revise", SIM_NOW, trigger="director", source="director",
                      visibility="public",
                      payload={"target_seq": str(a2), "new_grade": "B", "reason": "复核下调"})
        assert await M.a_grade_gap_days(pool, SIM_NOW) == pytest.approx(3.0)
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_type_entropy() -> None:
    """两类各半 → 熵 = 1.0 bit（当日）。"""
    pool = await _pool()
    try:
        for _ in range(4):
            await _insert(pool, "agent.think", SIM_NOW - dt.timedelta(hours=1))
            await _insert(pool, "agent.rest", SIM_NOW - dt.timedelta(hours=1))
        assert await M.type_entropy(pool, SIM_NOW) == pytest.approx(1.0)
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_gini() -> None:
    """均等分布基尼 ≈0；全集中一人 → 基尼趋高。"""
    assert M.gini([10, 10, 10, 10]) == pytest.approx(0.0)
    assert M.gini([40, 0, 0, 0]) == pytest.approx(0.75)
    assert M.gini([]) == 0.0
    pool = await _pool()
    try:
        for _ in range(4):
            await _insert(pool, "agent.think", SIM_NOW, actors=("A01",))
        await _insert(pool, "agent.think", SIM_NOW, actors=("A02",))
        assert await M.appearance_gini(pool, SIM_NOW) == pytest.approx(M.gini([4, 1]))
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_ngram_dup() -> None:
    """两句完全相同台词 → 3-gram 重复占比 > 0；全不同 → 0。"""
    pool = await _pool()
    try:
        lines = [{"speaker": "A01", "text": "今天天气不错"}]
        await _insert(pool, "dialogue.chat", SIM_NOW, payload={"lines": lines, "participants": ["A01", "A02"]})
        await _insert(pool, "dialogue.chat", SIM_NOW, payload={"lines": lines, "participants": ["A01", "A02"]})
        dup = await M.ngram_dup(pool, SIM_NOW)
        assert 0 < dup <= 1
        pool2 = await _pool()
        try:
            await _insert(pool2, "dialogue.chat", SIM_NOW,
                          payload={"lines": [{"speaker": "A01", "text": "甲乙丙丁戊"}], "participants": ["A01"]})
            await _insert(pool2, "dialogue.chat", SIM_NOW,
                          payload={"lines": [{"speaker": "A02", "text": "己庚辛壬癸"}], "participants": ["A02"]})
            assert await M.ngram_dup(pool2, SIM_NOW) == 0.0
        finally:
            await close_pool(pool2)
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_relation_week_change() -> None:
    """两条活跃边其一 |Δaffinity|≥3 → 周变化率 0.5；标签增删亦计入。"""
    pool = await _pool()
    try:
        changes = [
            {"a_id": "A01", "b_id": "A02", "delta_affinity": 4, "delta_tension": 0, "cause": "1"},
            {"a_id": "A03", "b_id": "A04", "delta_affinity": 1, "delta_tension": 0, "cause": "1"},
        ]
        await _insert(pool, "relation.changed", SIM_NOW, source="system", trigger="system",
                      payload={"changes": changes})
        assert await M.relation_week_change(pool, SIM_NOW) == pytest.approx(0.5)
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_high_tension_ratio() -> None:
    """4 条边 1 条 tension>60 → 占比 0.25。"""
    pool = await _pool()
    try:
        await pool.execute("UPDATE relations SET tension=70 WHERE a_id='A01' AND b_id='A02'")
        await pool.execute("UPDATE relations SET tension=10 WHERE NOT (a_id='A01' AND b_id='A02')")
        # seed_8 共 12 条边
        n = await pool.fetchval("SELECT count(*) FROM relations")
        assert await M.high_tension_ratio(pool) == pytest.approx(1 / n, abs=1e-3)
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_active_conflict_edges_direction() -> None:
    """test_gossip_edge_direction_teller_about：gossip 计 teller→about 边（不取 teller→listener）。"""
    pool = await _pool()
    try:
        await pool.execute("UPDATE relations SET tension=0")  # 清空 tension 边，只留行为边
        await _insert(pool, "dialogue.gossip", SIM_NOW, actors=("A05","A03"),
                      payload={"teller": "A05", "listener": "A03", "about": "A04", "cites": [], "lines": []},
                      visibility="public", source="agent:A05")
        total, star_zero, _ = await M.active_conflict_edges(pool, SIM_NOW)
        assert total == 1
        edge = await M.active_conflict_edges(pool, SIM_NOW)
        # 边方向 = teller(A05)→about(A04)：明星覆盖按参与计
        assert "A05" not in edge[2] and "A04" not in edge[2]
        assert "A03" in edge[2], "listener 方向不得计冲突边（01 §9 写死）"
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_intervention_and_cost() -> None:
    """干预率复用 T-DIR-03 同口径；日结成本 = SUM/COUNT(DISTINCT date) 且写 world_state。"""
    pool = await _pool()
    try:
        await _insert(pool, "director.intervene", SIM_NOW, trigger="director", source="director",
                      visibility="public", payload={"level": "L1", "reason": "x"})
        await _insert(pool, "agent.think", SIM_NOW)
        assert await M.intervention_rate(pool, SIM_NOW) == pytest.approx(0.5)
        await pool.execute(
            """
            INSERT INTO llm_calls (sim_time, task_type, provider, model, prompt_tokens,
                                   completion_tokens, cost_micro_cny, latency_ms, status)
            VALUES ($1, 'dialogue', 'mock', 'mock', 10, 10, 2000, 5, 'ok')
            """, SIM_NOW)
        m = await M.write_health_daily(pool, SIM_NOW)
        assert m["cost_micro_cny"] == 2000
        v = await pool.fetchval("SELECT value FROM world_state WHERE key='health.cost_daily'")
        assert int(json.loads(v) if isinstance(v, str) else v) == 2000
        # health_daily 行落库且各列非空（验收 2 口径）
        # 本 scratch 库未灌 health_daily_v1.sql → 用 DDL 灌后验证
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_health_daily_ddl_row() -> None:
    """health_daily DDL 独立增量文件可用；写入行各列非空（08 T-AUD-08 验收 2 形态）。"""
    from tests.conftest import DDL_DIR, _build_db, _drop_db

    name = "worldsim_audit_metrics_ddl"
    dsn = _build_db(name, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql")
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        await _insert(pool, "agent.think", SIM_NOW, grade="C")
        await M.write_health_daily(pool, SIM_NOW)
        row = await pool.fetchrow("SELECT * FROM health_daily ORDER BY sim_day DESC LIMIT 1")
        assert row is not None
        for col in ("a_grade_gap_days", "type_entropy", "gini", "ngram_dup", "relation_week_change",
                    "high_tension_ratio", "active_conflict_edges", "intervention_rate", "cost_micro_cny"):
            assert row[col] is not None, col
    finally:
        await pool.close()
        _drop_db(name)


def test_classify_reads_config() -> None:
    """阈值判定从 health_thresholds.yaml 读值断言（不写字面量）；metrics.py 无字面阈值。"""
    cfg = M.load_thresholds()
    spec = next(m for m in cfg["metrics"] if m["metric"] == "event_type_entropy_bits")
    healthy_min = float(spec["healthy"]["min"])
    assert M.classify("event_type_entropy_bits", healthy_min + 0.5, cfg) == "healthy"
    assert M.classify("event_type_entropy_bits", healthy_min - 0.01, cfg) in ("warning", "alarm")
    alarm_max = float(spec["alarm"]["max"])
    assert M.classify("event_type_entropy_bits", alarm_max - 0.5, cfg) == "alarm"
    # 关系周变化率双带
    rs = next(m for m in cfg["metrics"] if m["metric"] == "relation_graph_weekly_change_ratio")
    lo, hi = float(rs["healthy"]["min"]), float(rs["healthy"]["max"])
    assert M.classify("relation_graph_weekly_change_ratio", (lo + hi) / 2, cfg) == "healthy"
    assert M.classify("relation_graph_weekly_change_ratio", lo / 2, cfg) == "alarm"
    # 冲突边三档
    cs = next(m for m in cfg["metrics"] if m["metric"] == "active_conflict_edges")
    assert M.classify("active_conflict_edges", (int(cs["healthy"]["min_total"]), 0), cfg) == "healthy"
    assert M.classify("active_conflict_edges", (int(cs["alarm"]["max_total"]) - 1, 0), cfg) == "alarm"
    # 字面阈值守卫（08 验收 4）
    src = (Path(__file__).resolve().parents[2] / "worldsim" / "audit" / "metrics.py").read_text(encoding="utf-8")
    assert not re.search(r"\b2\.4\b|\b0\.45\b|\b0\.08\b|\b0\.15\b", src), "metrics.py 不得含字面阈值"
