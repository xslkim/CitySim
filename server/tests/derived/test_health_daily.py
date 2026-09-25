"""T-SYN-08 refresh_health_daily 验收（04 §10.2 口径副本复算 + 01 §9 结构指标 + R1 §A.4 对拍）。"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.ingest.derived import (
    materialize_event_grade,
    project_relation_change,
    refresh_health_daily,
)

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_derived_health_test"
MAIN_DB = "worldsim_derived_health_main_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
D = dt.date(2026, 10, 13)            # 定稿日
SIM_NOW = dt.datetime(2026, 10, 14, 0, 0, tzinfo=LOCAL_TZ)  # 窗口右端


def T(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=LOCAL_TZ)


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def _ins(pool: Any, seq: int, type_: str, sim_time: dt.datetime, *, actors: list[str],
               payload: dict, ui: dict | None = None, trigger: str = "autonomous",
               visibility: str = "internal") -> None:
    await pool.execute(
        """
        INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, actors,
                            payload, visibility, ui)
        VALUES ($1,$1,$2,$2,$3,'system',$4,$5,$6::jsonb,$7,$8::jsonb)
        """, seq, sim_time, type_, trigger, actors, json.dumps(payload, ensure_ascii=False),
        visibility, json.dumps(ui) if ui else None)


async def _seed_metric_dataset(pool: Any, *, replica: bool) -> list[int]:
    """对拍共用数据集（主/副本同形事件流）；副本侧追加 relation_change_log/event_grade_view 物化。"""
    # seq1/seq2：两条 A 级 chat 间隔 12h；seq5 把 seq1 改判 B → 有效 A 只剩 seq2
    await _ins(pool, 1, "dialogue.chat", T(D - dt.timedelta(days=2), 8), actors=["A01", "A02"],
               payload={"participants": ["A01", "A02"], "lines": [
                   {"speaker": "A01", "text": "aaaa", "at_offset_s": 0},
                   {"speaker": "A02", "text": "aaaa", "at_offset_s": 3}], "text_display": "闲聊"},
               ui={"grade": "A"}, visibility="public")
    await _ins(pool, 2, "dialogue.chat", T(D - dt.timedelta(days=2), 20), actors=["A01", "A02"],
               payload={"participants": ["A01", "A02"], "lines": [], "text_display": "再聊"},
               ui={"grade": "A"}, visibility="public")
    await _ins(pool, 3, "agent.move", T(D, 9), actors=["A01"],
               payload={"from": "a", "to": "b", "sim_cost_min": 1})
    await _ins(pool, 4, "relation.changed", T(D, 10), actors=["A01", "A02"], trigger="system",
               payload={"changes": [{"a_id": "A01", "b_id": "A02", "delta_affinity": 4,
                                     "delta_tension": 0, "cause": "chat"}]})
    await _ins(pool, 5, "director.grade_revise", T(D, 11), actors=[], trigger="director",
               payload={"target_seq": "1", "new_grade": "B", "reason": "复核下调"},
               visibility="public")
    if replica:
        await project_relation_change.project(pool, 4)
        for s in (1, 2, 5):
            await materialize_event_grade.materialize(pool, s)
    return [1, 2, 3, 4, 5]


async def test_five_metrics(pool) -> None:
    """验收 4：五指标逐列与手算一致；A 级判定读 event_grade_view（改判后口径随之变化）。"""
    await _seed_metric_dataset(pool, replica=True)
    m = await refresh_health_daily.compute_day(pool, D)
    # A 级间隔：seq1 改判 B → 有效 A 仅 seq2 → 不足两条按窗长劣化 7.0（与主库同口径）
    assert m["a_grade_gap_days"] == 7.0
    # 类型熵：D-2 {chat:2} → 0；D {move,relation.changed,grade_revise} 各 1 → log2(3)；均值
    assert m["type_entropy"] == round((0 + math.log2(3)) / 2, 4)
    # 出场基尼：A01×4、A02×3（含 relation.changed actors）→ 1/(2×7)=0.0714
    assert m["gini"] == round(1 / 14, 4)
    # 3-gram：两行 "aaaa" → gram('aaa')×4，dup=3/4=0.75
    assert m["ngram_dup"] == 0.75
    # 关系周变化率：1 活跃边 |Δ|=4≥3 → 1.0
    assert m["relation_week_change"] == 1.0
    # 改判前口径对照：物化前（无 grade_revise）两条 A → 间隔 0.5 日
    await pool.execute("DELETE FROM event_grade_view")
    await materialize_event_grade.materialize(pool, 1)
    await materialize_event_grade.materialize(pool, 2)
    m2 = await refresh_health_daily.compute_day(pool, D)
    assert m2["a_grade_gap_days"] == 0.5


async def test_structural_columns(pool) -> None:
    """验收 5：high_tension_ratio/active_conflict_edges 与 01 §9 口径手算一致；gossip 取 teller→about。"""
    # 定稿矩阵（D 日 relation_daily）：3 边，tension 70/40/10
    for a, b, t in (("A01", "A02", 70), ("A02", "A03", 40), ("A03", "A04", 10)):
        await pool.execute(
            "INSERT INTO relation_daily (sim_day, a_id, b_id, affinity, tension) VALUES ($1,$2,$3,0,$4)",
            D, a, b, t)
    # 近 7 日冲突流水：gossip teller=A04 about=A01（计 A04→A01）；refuse from=A05 to=A06
    await _ins(pool, 1, "dialogue.gossip", T(D, 8), actors=["A04", "A05"],
               payload={"teller": "A04", "listener": "A05", "about": "A01", "cites": [],
                        "lines": [], "text_display": "g"}, visibility="public")
    await _ins(pool, 2, "social.refuse", T(D, 9), actors=["A05", "A06"],
               payload={"from": "A05", "to": "A06", "request_ref": "9", "politeness": 0})
    await _ins(pool, 3, "dialogue.gossip", T(D, 10), actors=["A05", "A06"],
               payload={"teller": "A05", "listener": "A06", "about": "A02", "cites": [],
                        "lines": []}, visibility="public")
    ratio, edges = await refresh_health_daily.structural_metrics(pool, D, SIM_NOW)
    assert ratio == round(1 / 3, 4)          # tension>60 仅 (A01,A02)
    # 冲突边：tension≥30 → (A01,A02),(A02,A03)；+ gossip A04→A01、A05→A02；+ refuse A05→A06
    assert edges == 5


async def test_cost_from_snapshot(pool) -> None:
    """验收 6：快照含 health.cost_daily_micro_cny 时落列；缺席时 NULL 不报错。"""
    await pool.execute(
        "INSERT INTO world_state_snapshot (sim_day, state, digest) VALUES ($1,$2::jsonb,'sha256:x')",
        D, json.dumps({"sim": {"day": D.isoformat()}, "relations": [], "agents": [],
                       "health": {"cost_daily_micro_cny": 123456}}))
    assert await refresh_health_daily.cost_from_snapshot(pool, D) == 123456
    m = await refresh_health_daily.refresh(pool, D)
    assert m["cost_micro_cny"] == 123456
    row = await pool.fetchrow("SELECT cost_micro_cny FROM health_daily WHERE sim_day=$1", D)
    assert int(row["cost_micro_cny"]) == 123456
    other = D - dt.timedelta(days=1)  # 无快照日
    m2 = await refresh_health_daily.refresh(pool, other)
    assert m2["cost_micro_cny"] is None


async def test_thresholds_not_stored(pool) -> None:
    """验收 7：副本全库无阈值常量；代码 grep 无 01 §9 字面阈值数字。"""
    with open(Path(__file__).resolve().parents[2] / "config" / "health_thresholds.yaml",
              encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    literals = set()
    for metric in cfg["metrics"]:
        for band in (metric.get("healthy"), metric.get("warning"), metric.get("alarm")):
            if isinstance(band, dict):
                for v in json.loads(json.dumps(band)).values():
                    vals = [v] if not isinstance(v, dict) else list(v.values())
                    # 只查小数字面量（T-AUD-08 验收 4 同款口径：grep 2.4/0.45 形态）；
                    # 整数阈值（如计数）在代码中无区分度，不纳入
                    literals |= {str(x) for x in vals
                                 if isinstance(x, float) or (isinstance(x, str) and "." in x)}
    src = (Path(__file__).resolve().parents[2] / "worldsim" / "ingest" / "derived" /
           "refresh_health_daily.py").read_text(encoding="utf-8")
    hits = [lit for lit in literals if re.search(rf"(?<![\d.]){re.escape(lit)}(?![\d.])", src)]
    assert hits == [], f"阈值字面量出现在副本聚合代码中: {hits}"
    # DDL 侧无阈值列（列集恰 05 §3.5 十列）
    cols = {r["column_name"] for r in await pool.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema='public' AND table_name='health_daily'")}
    assert not any("threshold" in c or "warn" in c or "alarm" in c for c in cols)


async def test_parity_with_08_metrics(pool) -> None:
    """验收 9（R1 §A.4 对拍）：同一构造数据集喂 08 metrics.py（主库）与本模块（副本）五指标一致。"""
    from worldsim.audit import metrics as m8

    main_dsn = _build_db(MAIN_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    main_pool = await asyncpg.create_pool(main_dsn, min_size=1, max_size=3)
    try:
        # 主库侧同形事件（seq 自增，捕获后与副本对齐 grade_revise target）
        seq_map: dict[int, int] = {}

        async def mins(seq: int, type_: str, sim_time: dt.datetime, *, actors: list[str],
                       payload: dict, ui: dict | None = None, trigger: str = "autonomous",
                       visibility: str = "internal") -> None:
            payload = dict(payload)
            if type_ == "director.grade_revise":
                payload["target_seq"] = str(seq_map[int(payload["target_seq"])])
            real = await main_pool.fetchval(
                """
                INSERT INTO events (tick, sim_time, type, source, trigger, actors,
                                    payload, visibility, ui)
                VALUES ($1,$2,$3,'system',$4,$5,$6::jsonb,$7,$8::jsonb) RETURNING seq
                """, seq, sim_time, type_, trigger, actors,
                json.dumps(payload, ensure_ascii=False), visibility,
                json.dumps(ui) if ui else None)
            seq_map[seq] = real

        await mins(1, "dialogue.chat", T(D - dt.timedelta(days=2), 8), actors=["A01", "A02"],
                   payload={"participants": ["A01", "A02"], "lines": [
                       {"speaker": "A01", "text": "aaaa", "at_offset_s": 0},
                       {"speaker": "A02", "text": "aaaa", "at_offset_s": 3}],
                            "text_display": "闲聊"}, ui={"grade": "A"}, visibility="public")
        await mins(2, "dialogue.chat", T(D - dt.timedelta(days=2), 20), actors=["A01", "A02"],
                   payload={"participants": ["A01", "A02"], "lines": [], "text_display": "再聊"},
                   ui={"grade": "A"}, visibility="public")
        await mins(3, "agent.move", T(D, 9), actors=["A01"],
                   payload={"from": "a", "to": "b", "sim_cost_min": 1})
        await mins(4, "relation.changed", T(D, 10), actors=["A01", "A02"], trigger="system",
                   payload={"changes": [{"a_id": "A01", "b_id": "A02", "delta_affinity": 4,
                                         "delta_tension": 0, "cause": "chat"}]})
        await mins(5, "director.grade_revise", T(D, 11), actors=[], trigger="director",
                   payload={"target_seq": "1", "new_grade": "B", "reason": "复核下调"},
                   visibility="public")

        await _seed_metric_dataset(pool, replica=True)
        replica_m = await refresh_health_daily.compute_day(pool, D)

        main_vals = {
            "a_grade_gap_days": await m8.a_grade_gap_days(main_pool, SIM_NOW),
            "type_entropy": await m8.type_entropy(main_pool, SIM_NOW),
            "gini": await m8.appearance_gini(main_pool, SIM_NOW),
            "ngram_dup": await m8.ngram_dup(main_pool, SIM_NOW),
            "relation_week_change": await m8.relation_week_change(main_pool, SIM_NOW),
        }
        for k, v in main_vals.items():
            assert replica_m[k] == pytest.approx(v, abs=1e-4), \
                f"{k}: 副本={replica_m[k]} 主库={v}（对拍不一致）"
    finally:
        await main_pool.close()
        _drop_db(MAIN_DB)


async def test_sweep_idempotent(pool) -> None:
    """验收 8：兜底调度幂等——手动触发两次 refresh 内容不变。"""
    await _seed_metric_dataset(pool, replica=True)
    await refresh_health_daily.refresh(pool, D)
    first = dict(await pool.fetchrow("SELECT * FROM health_daily WHERE sim_day=$1", D))
    await refresh_health_daily.refresh(pool, D)
    second = dict(await pool.fetchrow("SELECT * FROM health_daily WHERE sim_day=$1", D))
    first.pop("computed_at")
    second.pop("computed_at")
    assert first == second
