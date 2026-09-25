"""T-SYN-06 副本库 DDL 验收（05 §3.1~§3.8 + §4.1 逐表清单断言；05 §5 授权面）。

验收 2（逐表列名/类型/约束/索引与 05 §3 SQL 块清单一致）、3（全库无 text_raw/content/embedding 列；
memory_projection 恰八列）、4（obs_ro 清单制只读 8 项 / derived_task 不授权）、5（摄入账号可 DELETE
当日重灌）、6（events PK=seq 幂等位点 + events_caused_by 部分索引）。
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db, _psql

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_replica_ddl_test"

# 05 §3 SQL 块逐字清单（手写对拍）：表 → [(列名, 信息模式数据类型)]
EXPECTED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "events": [
        ("seq", "bigint"), ("tick", "bigint"), ("sim_time", "timestamp with time zone"),
        ("wall_time", "timestamp with time zone"), ("type", "text"), ("source", "text"),
        ("trigger", "text"), ("arc_id", "text"), ("location_id", "text"),
        ("actors", "ARRAY"), ("rng_seed", "bigint"), ("payload", "jsonb"),
        ("visibility", "text"), ("ui", "jsonb"), ("schema_version", "smallint"),
    ],
    "memory_projection": [
        ("memory_id", "bigint"), ("agent_id", "text"), ("sim_time", "timestamp with time zone"),
        ("kind", "text"), ("content_display", "text"), ("importance", "smallint"),
        ("source_event_seq", "bigint"), ("is_witness", "boolean"),
    ],
    "relation_change_log": [
        ("id", "bigint"), ("event_seq", "bigint"), ("a_id", "text"), ("b_id", "text"),
        ("delta_affinity", "smallint"), ("delta_tension", "smallint"),
        ("labels_added", "jsonb"), ("labels_removed", "jsonb"),
        ("sim_time", "timestamp with time zone"), ("sim_day", "date"),
    ],
    "relation_daily": [
        ("sim_day", "date"), ("a_id", "text"), ("b_id", "text"),
        ("affinity", "smallint"), ("tension", "smallint"), ("labels", "ARRAY"),
    ],
    "health_daily": [
        ("sim_day", "date"), ("a_grade_gap_days", "numeric"), ("type_entropy", "numeric"),
        ("gini", "numeric"), ("ngram_dup", "numeric"), ("relation_week_change", "numeric"),
        ("high_tension_ratio", "numeric"), ("active_conflict_edges", "integer"),
        ("intervention_rate", "numeric"), ("cost_micro_cny", "bigint"),
        ("computed_at", "timestamp with time zone"),
    ],
    "world_state_snapshot": [
        ("sim_day", "date"), ("state", "jsonb"), ("digest", "text"),
        ("created_at", "timestamp with time zone"),
    ],
    "ripple_edge": [
        ("id", "bigint"), ("root_event_seq", "bigint"), ("dst_event_seq", "bigint"),
        ("src_event_seq", "bigint"), ("hop", "smallint"), ("teller_id", "text"),
        ("listener_id", "text"), ("distortion", "numeric"),
        ("sim_time", "timestamp with time zone"), ("sim_day", "date"),
    ],
    "event_grade_view": [
        ("seq", "bigint"), ("grade", "text"), ("revised_by_seq", "bigint"),
        ("reason", "text"), ("updated_at", "timestamp with time zone"),
    ],
    "derived_task": [
        ("id", "bigint"), ("task_type", "text"), ("dedupe_key", "text"), ("sim_day", "date"),
        ("status", "text"), ("attempts", "integer"), ("run_at", "timestamp with time zone"),
        ("done_at", "timestamp with time zone"), ("last_error", "text"),
    ],
}

EXPECTED_INDEXES = {
    "events_simday_idx", "events_type_time", "events_actors_gin", "events_loc_time",
    "events_caused_by", "memproj_agent_time", "memproj_src_event", "memproj_reflection",
    "rcl_pair_time", "rcl_event", "rcl_simday", "ripple_root", "ripple_simday",
    "derived_task_pending",
}


@pytest_asyncio.fixture
async def pool():
    # 角色为实例级对象：幂等确保存在（授权断言依赖）
    _psql("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='worldsim_ingest') THEN "
          "CREATE ROLE worldsim_ingest LOGIN; END IF; END $$")
    _psql("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='obs_ro') THEN "
          "CREATE ROLE obs_ro LOGIN; END IF; END $$")
    dsn = _build_db(DB_NAME, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def test_tables_match_05(pool) -> None:
    """验收 2：9 张表逐列（名+类型）与 05 §3/§4.1 SQL 块清单一致；约束与索引抽查。"""
    rows = await pool.fetch(
        "SELECT table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_schema='public' ORDER BY table_name, ordinal_position")
    got: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        got.setdefault(r["table_name"], []).append((r["column_name"], r["data_type"]))
    assert sorted(got) == sorted(EXPECTED_COLUMNS)  # 恰 9 张表
    for table, cols in EXPECTED_COLUMNS.items():
        assert got[table] == cols, f"{table} 列集漂移: {got[table]}"

    constraints = await pool.fetch(
        "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint"
        " WHERE connamespace='public'::regnamespace AND contype IN ('c','u','p')")
    defs = {r["conname"]: r["def"] for r in rows} if False else {r["conname"]: r["def"] for r in constraints}
    assert any("UNIQUE (event_seq, a_id, b_id)" in d for d in defs.values())          # 05 §3.3
    assert any("UNIQUE (root_event_seq, dst_event_seq)" in d for d in defs.values())  # 05 §3.7
    assert any("CHECK" in d and "hop" in d and "1" in d and "4" in d for d in defs.values())  # ripple hop 1..4
    assert any("grade" in d and "'A'" in d and "'B'" in d and "'C'" in d for d in defs.values())
    assert any("status" in d and "pending" in d and "failed" in d for d in defs.values())

    idx = {r["indexname"] for r in await pool.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname='public'")}
    assert EXPECTED_INDEXES <= idx


async def test_no_raw_channels(pool) -> None:
    """验收 3：全库扫描无 text_raw/content/embedding 列；memory_projection 恰 05 §3.2 八列。"""
    bad = await pool.fetch(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema='public' AND column_name IN ('text_raw','content','embedding')")
    assert bad == []
    cols = [r["column_name"] for r in await pool.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema='public' AND table_name='memory_projection' ORDER BY ordinal_position")]
    assert cols == [c for c, _ in EXPECTED_COLUMNS["memory_projection"]]


async def test_obs_ro_readonly_grants(pool) -> None:
    """验收 4：obs_ro 可 SELECT 清单 8 项（逐名核对，清单制）；INSERT/UPDATE/DELETE 与 derived_task 全拒。"""
    readable = ["events", "memory_projection", "relation_change_log", "relation_daily",
                "health_daily", "world_state_snapshot", "ripple_edge", "event_grade_view"]
    grants = {(r["table_name"], r["privilege_type"]) for r in await pool.fetch(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
        " WHERE table_schema='public' AND grantee='obs_ro'")}
    assert {t for t, p in grants if p == "SELECT"} == set(readable)  # 清单制：恰 8 项，逐名核对
    assert not {t for t, p in grants if p in ("INSERT", "UPDATE", "DELETE")}
    assert not any(t == "derived_task" for t, _ in grants)
    # 行为级证据：SET ROLE obs_ro 直跑
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE obs_ro")
            assert await conn.fetchval("SELECT count(*) FROM events") == 0
            with pytest.raises(Exception):
                await conn.fetchval("SELECT count(*) FROM derived_task")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE obs_ro")
            with pytest.raises(Exception):
                await conn.execute(
                    "INSERT INTO events (seq,tick,sim_time,wall_time,type,source,trigger,payload,visibility)"
                    " VALUES (1,1,now(),now(),'agent.move','system','system','{}','internal')")


async def test_ingest_role_can_delete_for_reproject(pool) -> None:
    """验收 5：摄入账号可 DELETE 当日事件（重灌依赖，04 §9.2）。"""
    await pool.execute(
        "INSERT INTO events (seq,tick,sim_time,wall_time,type,source,trigger,payload,visibility)"
        " VALUES (1,1,'2026-10-12 09:00+08',now(),'agent.move','system','system','{}','internal')")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE worldsim_ingest")
            n = await conn.fetchval(
                "WITH d AS (DELETE FROM events WHERE (sim_time AT TIME ZONE 'Asia/Shanghai')::date"
                " = DATE '2026-10-12' RETURNING seq) SELECT count(*) FROM d")
            assert n == 1
    assert await pool.fetchval("SELECT count(*) FROM events") == 0


async def test_events_pk_seq_and_partial_index(pool) -> None:
    """验收 6：events PK 在 seq（幂等位点）；events_caused_by 部分索引（WHERE payload ? 'caused_by'）。"""
    pk = await pool.fetchval(
        "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i"
        " WHERE i.indrelid='events'::regclass AND i.indisprimary")
    assert "(seq)" in pk
    partial = await pool.fetchval(
        "SELECT pg_get_indexdef(i.indexrelid) FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid"
        " WHERE c.relname='events_caused_by'")
    assert "caused_by" in partial and "WHERE" in partial.upper()
