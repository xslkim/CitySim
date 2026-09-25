"""T-WEB-06 涟漪/关系/健康接口组测试（03 §3.4 五段、03 §5.1、01 §9 阈值下发）。"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.observe.app import create_app
from worldsim.observe.auth import TokenStore
from worldsim.observe.rest_health import METRIC_COLUMN, THRESHOLDS_PATH

DB_NAME = "worldsim_obs_rest_ripple_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
T0 = dt.datetime.now(LOCAL_TZ).replace(hour=10, minute=0, second=0, microsecond=0) + dt.timedelta(days=8)


@pytest_asyncio.fixture
async def fx() -> Any:
    """构造完整涟漪链：e1 argue(public,A) → m1 投影 → e2 gossip(cites=m1) → followup e3(promoted,caused_by=e1)
    + e_rel relation.changed(changes[].cause=e1)；relation_daily 两行；health_daily 一行。"""
    dsn = _build_db(
        DB_NAME,
        DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql",
        DDL_DIR / "obs_views_v1.sql", DDL_DIR / "obs_derived_v1.sql",
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    conn = await pool.acquire()

    async def ev(tick: int, etype: str, vis: str, payload: dict, *, ui: dict | None = None,
                 actors: list[str] | None = None, t: dt.datetime | None = None) -> int:
        return await conn.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, payload, visibility, ui)
            VALUES ($1, $2, $3, 'system', 'system', $4, $5::jsonb, $6, $7::jsonb) RETURNING seq
            """,
            tick, t or T0, etype, actors or [], json.dumps(payload, ensure_ascii=False), vis,
            json.dumps(ui) if ui else None,
        )

    e1 = await ev(1, "dialogue.argue", "public",
                  {"participants": ["A01", "A03"], "reason_hint": "r", "lines": [], "witnesses": []},
                  ui={"grade": "A"}, actors=["A01", "A03"])
    m1 = await conn.fetchval(
        "INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance,"
        " source_event_seq, is_witness)"
        " VALUES ('A02', $1, 'projection', 'RAW', '目击：林晚和叶蓁吵起来了', 8, $2, TRUE) RETURNING id",
        T0 + dt.timedelta(minutes=1), e1,
    )
    e2 = await ev(2, "dialogue.gossip", "public",
                  {"teller": "A02", "listener": "A04", "about": "A01", "cites": [str(m1)],
                   "lines": [], "text_display": "听说林晚和叶蓁吵起来了"},
                  actors=["A02", "A04"], t=T0 + dt.timedelta(hours=1))
    e3 = await ev(3, "agent.promoted", "internal",
                  {"from_tier": "secondary", "to_tier": "star", "reason": "event_driven",
                   "caused_by": str(e1)},
                  actors=["A03"], t=T0 + dt.timedelta(hours=2))
    e_rel = await ev(4, "relation.changed", "internal",
                     {"changes": [{"a_id": "A01", "b_id": "A03", "delta_affinity": -4,
                                   "delta_tension": 8, "cause": str(e1)}]},
                     t=T0 + dt.timedelta(hours=2))
    day = T0.date()
    await conn.execute(
        "INSERT INTO obs.relation_daily (sim_day, a_id, b_id, affinity, tension, labels) VALUES"
        " ($1, 'A01', 'A03', -20, 48, '{冷战}'), ($1, 'A03', 'A01', -18, 40, '{}'),"
        " ($1, 'A01', 'A02', 0, 0, '{}')",
        day,
    )
    await conn.execute(
        "INSERT INTO obs.ripple_edge (root_event_seq, dst_event_seq, src_event_seq, hop,"
        " teller_id, listener_id, distortion, sim_time, sim_day)"
        f" VALUES ({e1}, {e2}, {e1}, 1, 'A02', 'A04', 0.25, $1, $2)",
        T0 + dt.timedelta(hours=1), day,
    )
    await conn.execute(
        "INSERT INTO health_daily (sim_day, a_grade_gap_days, type_entropy, gini, ngram_dup,"
        " relation_week_change, high_tension_ratio, active_conflict_edges, stars_without_conflict,"
        " intervention_rate, cost_micro_cny)"
        " VALUES ($1, 0.8, 2.8, 0.52, 0.09, 0.12, 0.094, 14, 0, 0.083, 0)",
        day,
    )
    await pool.release(conn)

    token_db = str(Path(tempfile.mkdtemp()) / "tokens.db")
    app = create_app(pool=pool, token_db_path=token_db)
    token = TokenStore(token_db).issue("tester")
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c, {"e1": e1, "e2": e2, "e3": e3, "e_rel": e_rel, "day": day}
    await pool.close()
    _drop_db(DB_NAME)


@pytest.mark.asyncio
async def test_ripple_five_sections(fx: Any) -> None:
    """构造 gossip 链五段全非空（03 §3.4）。"""
    c, ids = fx
    data = (await c.get(f"/api/ripple/{ids['e1']}")).json()["data"]
    assert data["projections"] and data["projections"][0]["is_witness"] is True
    assert data["chain"] and data["chain"][0]["hop"] == 1 and data["chain"][0]["distortion"] == 0.25
    assert [f["seq"] for f in data["followups"]] == [ids["e3"]]  # caused_by 裸 seq 链
    assert [r["event_seq"] for r in data["relation_changes"]] == [ids["e_rel"]]  # changes[].cause 链
    assert data["stats"]["covered_agents"] == 2 and data["stats"]["hops"] == 1
    assert data["stats"]["followup_count"] == 1


@pytest.mark.asyncio
async def test_ripple_e_prefix_stripped(fx: Any) -> None:
    """URL e<seq> 前缀剥离（03 §3.4）；非法形态 422。"""
    c, ids = fx
    r = await c.get(f"/api/ripple/e{ids['e1']}")
    assert r.status_code == 200 and r.json()["data"]["source_seq"] == ids["e1"]
    assert (await c.get("/api/ripple/abc")).status_code == 422
    assert (await c.get("/api/ripple/999999")).status_code == 404


@pytest.mark.asyncio
async def test_ripple_today_top5(fx: Any) -> None:
    """今日热涟漪：当前模拟日 grade='A'（视图口径）事件入榜，含 stats。"""
    c, ids = fx
    data = (await c.get("/api/ripple/today")).json()["data"]
    assert [it["event"]["seq"] for it in data["items"]] == [ids["e1"]]
    assert data["items"][0]["stats"]["hops"] == 1


@pytest.mark.asyncio
async def test_relations_snapshots_and_pair(fx: Any) -> None:
    """snapshots 稀疏编码（零边剔除）；pair 双向时序 + 变更流水 + 关键事件标记。"""
    c, ids = fx
    data = (await c.get("/api/relations/snapshots")).json()["data"]
    (day_item,) = data["items"]
    assert {e["a"] + e["b"] for e in day_item["edges"]} == {"A01A03", "A03A01"}  # 零边 A01→A02 剔除
    pair = (await c.get(f"/api/relations/pair?a=A01&b=A03")).json()["data"]
    assert pair["series"]["forward"][0]["affinity"] == -20
    assert pair["series"]["backward"][0]["tension"] == 40
    assert pair["changes"][0]["event_seq"] == ids["e_rel"]
    assert pair["key_events"] and pair["key_events"][0]["seq"] == ids["e_rel"]
    assert (await c.get("/api/relations/pair?a=A01&b=A01")).status_code == 422
    assert (await c.get("/api/relations/pair?a=A01&b=ag07")).status_code == 422
    agent_edges = (await c.get("/api/relations?agent=A01")).json()["data"]["items"]
    assert agent_edges[0]["a"] == "A01" and agent_edges[0]["b"] == "A03"  # |aff| 降序


@pytest.mark.asyncio
async def test_health_thresholds_from_config(fx: Any) -> None:
    """响应阈值与 health_thresholds.yaml 逐字一致（改 yaml 重读生效）；六块齐全。"""
    c, _ = fx
    data = (await c.get("/api/health")).json()["data"]
    specs = {s["metric"]: s for s in yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8"))["metrics"]}
    assert len(data["metrics"]) == len(specs) == 7
    for m in data["metrics"]:
        spec = specs[m["key"]]
        assert m["thresholds"]["healthy"] == spec.get("healthy")
        assert m["thresholds"]["warning"] == spec.get("warning")
        assert m["thresholds"]["alarm"] == spec.get("alarm")
        assert m["color"] in ("green", "yellow", "red", None)
        assert m["advice"]
    by_key = {m["key"]: m for m in data["metrics"]}
    # 勾稽：值 = health_daily 列（0.52 gini 落在 0.45~0.6 预警带 → yellow）
    assert by_key["appearance_gini"]["value"] == 0.52
    assert by_key["appearance_gini"]["color"] == "yellow"
    assert by_key["a_grade_event_interval_days"]["color"] == "green"  # 0.8 ≤ 1
    rt = data["runtime"]
    assert set(rt) == {"intervention_rate", "intervention_rate_cap", "cost_micro_cny",
                       "cost_limit_micro_cny", "cost_breaker", "replica_lag_s", "replica_lag_ticks"}
    assert rt["intervention_rate"] == 0.083 and rt["replica_lag_s"] == 0
    assert set(data["today_partial"]) == {"events_today", "a_grade_today", "intervention_rate_today"}
