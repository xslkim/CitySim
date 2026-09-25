"""T-WEB-01 obs 派生视图层测试（05 §3.3~§3.8 列形；00 §1 A5/A12）。

私有 scratch 库：schema_v1 + seed_8 + health_daily_v1（08 T-AUD-08）+ obs_views_v1（M0）+ obs_derived_v1（本任务）。
列形 diff（验收 4）= information_schema 列清单逐字比对（M0 层两视图一并覆盖）。
"""

from __future__ import annotations

import datetime as dt
import gzip
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db, _psql_file

SERVER_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVER_ROOT.parent
DB_NAME = "worldsim_obs_derived_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))

# obs_refresh.py 以脚本形态交付（scripts/ 非包），按路径加载
_spec = importlib.util.spec_from_file_location("obs_refresh", SERVER_ROOT / "scripts" / "obs_refresh.py")
obs_refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(obs_refresh)

HOP_MAX = int(yaml.safe_load((SERVER_ROOT / "config" / "relations.yaml").read_text(encoding="utf-8"))["gossip"]["hop_max"])

EXPECTED_COLUMNS = {
    # M0 层（05 §3.1/§3.2 列清单逐字）
    "events": ["seq", "tick", "sim_time", "wall_time", "type", "source", "trigger", "arc_id",
               "location_id", "actors", "rng_seed", "payload", "visibility", "ui", "schema_version"],
    "memory_projection": ["memory_id", "agent_id", "sim_time", "kind", "content_display",
                          "importance", "source_event_seq", "is_witness"],
    # M4 层（05 §3.3/§3.8/§3.6/§3.4/§3.7 列清单逐字）
    "relation_change_log": ["id", "event_seq", "a_id", "b_id", "delta_affinity", "delta_tension",
                            "labels_added", "labels_removed", "sim_time", "sim_day"],
    "event_grade_view": ["seq", "grade", "revised_by_seq", "reason", "updated_at"],
    "world_state_snapshot": ["sim_day", "state", "digest", "created_at"],
    "relation_daily": ["sim_day", "a_id", "b_id", "affinity", "tension", "labels"],
    "ripple_edge": ["id", "root_event_seq", "dst_event_seq", "src_event_seq", "hop",
                    "teller_id", "listener_id", "distortion", "sim_time", "sim_day"],
}


@pytest.fixture(scope="module")
def obs_dsn() -> Any:
    dsn = _build_db(
        DB_NAME,
        DDL_DIR / "schema_v1.sql",
        DDL_DIR / "seed_8.sql",
        DDL_DIR / "health_daily_v1.sql",
        DDL_DIR / "obs_views_v1.sql",
        DDL_DIR / "obs_derived_v1.sql",
    )
    # 幂等重跑（验收 1：可重复执行）
    _psql_file(DDL_DIR / "obs_derived_v1.sql", DB_NAME)
    try:
        yield dsn
    finally:
        _drop_db(DB_NAME)


async def _insert_event(
    conn: asyncpg.Connection, tick: int, etype: str, visibility: str, payload: str,
    *, ui: str | None = None, sim_time: dt.datetime | None = None, actors: list[str] | None = None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, payload, visibility, ui)
        VALUES ($1, $2, $3, 'system', 'system', $4, $5::jsonb, $6, $7::jsonb)
        RETURNING seq
        """,
        tick, sim_time or dt.datetime.now(LOCAL_TZ), etype, actors or [], payload, visibility, ui,
    )


@pytest.mark.asyncio
async def test_relation_change_log_expand(obs_dsn: str) -> None:
    """relation.changed 展开列形按 05 §3.3（含 labels_added/removed，sim_day 本地时区聚合）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        seq = await _insert_event(
            conn, 9501, "relation.changed", "internal",
            '{"changes":[{"a_id":"A01","b_id":"A02","delta_affinity":-4,"delta_tension":8,'
            '"labels_added":["冷战"],"labels_removed":[],"cause":"1089"},'
            '{"a_id":"A02","b_id":"A01","delta_affinity":-4,"delta_tension":8,"cause":"1089"}]}',
        )
        rows = await conn.fetch(
            "SELECT * FROM obs.relation_change_log WHERE event_seq = $1 ORDER BY a_id, b_id", seq,
        )
        assert len(rows) == 2
        r = rows[0]
        assert (r["a_id"], r["b_id"]) == ("A01", "A02")
        assert r["delta_affinity"] == -4 and r["delta_tension"] == 8
        assert json.loads(r["labels_added"]) == ["冷战"]
        assert r["sim_day"] == r["sim_time"].astimezone(LOCAL_TZ).date()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_grade_view_revise_override(obs_dsn: str) -> None:
    """初值 B + 改判 A → 视图 grade='A' 且 events 行零 UPDATE（append-only 触发器拒绝）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        seq = await _insert_event(
            conn, 9502, "dialogue.argue", "public",
            '{"participants":["A01","A02"],"reason_hint":"x","lines":[],"witnesses":[]}',
            ui='{"grade":"B"}', actors=["A01", "A02"],
        )
        assert await conn.fetchval("SELECT grade FROM obs.event_grade_view WHERE seq = $1", seq) == "B"
        revise_seq = await _insert_event(
            conn, 9503, "director.grade_revise", "public",
            json.dumps({"target_seq": str(seq), "new_grade": "A", "reason": "终审上调"}),
        )
        row = await conn.fetchrow("SELECT * FROM obs.event_grade_view WHERE seq = $1", seq)
        assert row["grade"] == "A" and row["revised_by_seq"] == revise_seq and row["reason"] == "终审上调"
        before = await conn.fetchval("SELECT ui::text FROM events WHERE seq = $1", seq)
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute("UPDATE events SET ui = '{\"grade\":\"A\"}' WHERE seq = $1", seq)
        assert await conn.fetchval("SELECT ui::text FROM events WHERE seq = $1", seq) == before  # 零 UPDATE
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_health_daily_view_passthrough(obs_dsn: str) -> None:
    """obs.health_daily 与主库 health_daily 表逐行一致（透传，非自算；唯一权威 = 08 T-AUD-08）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        day = dt.date(2026, 9, 25)
        await conn.execute(
            """
            INSERT INTO health_daily (sim_day, a_grade_gap_days, type_entropy, gini, ngram_dup,
                relation_week_change, high_tension_ratio, active_conflict_edges, stars_without_conflict,
                intervention_rate, cost_micro_cny)
            VALUES ($1, 0.8, 2.8, 0.52, 0.09, 0.12, 0.094, 14, 0, 0.083, 0)
            ON CONFLICT (sim_day) DO NOTHING
            """,
            day,
        )
        main_row = dict(await conn.fetchrow("SELECT * FROM health_daily WHERE sim_day = $1", day))
        obs_row = dict(await conn.fetchrow("SELECT * FROM obs.health_daily WHERE sim_day = $1", day))
        assert obs_row == main_row
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_obs_ro_readonly(obs_dsn: str) -> None:
    """obs_ro：M4 层全部对象可 SELECT；UPDATE/DELETE/DDL 全被拒（05 §5 DDL/DML 权限为零）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        await conn.execute("SET ROLE obs_ro")
        for obj in ("events", "memory_projection", "relation_change_log", "event_grade_view",
                    "health_daily", "world_state_snapshot", "relation_daily", "ripple_edge"):
            await conn.fetchval(f"SELECT count(*) FROM obs.{obj}")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM obs.relation_daily")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE obs.world_state_snapshot SET digest = 'x'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("INSERT INTO obs.ripple_edge (root_event_seq, dst_event_seq, src_event_seq, hop,"
                               " teller_id, listener_id, sim_time, sim_day) VALUES (1,2,1,1,'A01','A02',now(),'2026-09-25')")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE obs.pwn (id int)")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_column_shape_diff_empty(obs_dsn: str) -> None:
    """验收 4：information_schema 列清单与 05 §3.1~§3.8 逐字 diff 为空（M0 两视图一并覆盖）；
    obs.health_daily 列集 = 主库 health_daily 表列集（透传口径，05 §3.5 列集为其子集——偏差登记）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        for obj, expected in EXPECTED_COLUMNS.items():
            cols = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'obs' AND table_name = $1 ORDER BY ordinal_position
                """,
                obj,
            )
            assert [c["column_name"] for c in cols] == expected, obj
        main_cols = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'health_daily' ORDER BY ordinal_position
            """
        )
        obs_cols = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'obs' AND table_name = 'health_daily' ORDER BY ordinal_position
            """
        )
        assert [c["column_name"] for c in obs_cols] == [c["column_name"] for c in main_cols]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_obs_refresh_three_tables(obs_dsn: str, tmp_path: Path) -> None:
    """验收 3：obs_refresh 三表行数 >0；ripple_edge.hop ∈ [1, hop_max]（上限读 relations.yaml 镜像）。

    构造链：chat(e1, public) → A03 记忆投影(m1, source=e1) → gossip1(e2, cites=[m1]) →
    A04 记忆投影(m2, source=e2) → gossip2(e3, cites=[m2])；另 m3 = 纯反思（无 source_event_seq）被 e3 同时引用（P2-9 跳过）。
    """
    day1 = dt.date.today() + dt.timedelta(days=2)  # 避开本模块其他用例的当日 relation.changed（同库共享）
    day2 = day1 + dt.timedelta(days=1)
    t1 = dt.datetime.combine(day1, dt.time(10, 0), tzinfo=LOCAL_TZ)
    t2 = dt.datetime.combine(day2, dt.time(0, 30), tzinfo=LOCAL_TZ)
    conn = await asyncpg.connect(obs_dsn)
    try:
        # 快照白名单文件（两日；day1 作首日基线）
        for day, t in ((day1, t1), (day2, t2)):
            state = {
                "sim": {"day": day.isoformat(), "sim_time": t.isoformat(), "compression_ratio": 1.0},
                "agents": [], "relations": [
                    {"a": "A01", "b": "A02", "affinity": 10, "tension": 5, "labels": ["同事"], "one_line": None},
                ],
                "economy": {"stocks": []}, "health": {"cost_daily_micro_cny": None}, "announcements": [],
            }
            with gzip.open(tmp_path / f"snapshot_{day.isoformat()}.whitelist.json.gz", "wt", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        e1 = await _insert_event(
            conn, 9601, "dialogue.chat", "public",
            '{"participants":["A01","A02"],"mode":"small","topic_ids":[],"lines":[],"witnesses":[],'
            '"text_display":"林晚和周叙在茶水间聊起新项目"}',
            sim_time=t1, actors=["A01", "A02"],
        )
        # 记忆（content_display 非空 = 已出行口径）；append-only 下事件行不可改，gossip 事件在记忆之后落库
        m1 = await conn.fetchval(
            "INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance, source_event_seq)"
            " VALUES ('A03', $1, 'projection', 'RAW', '听说林晚和周叙聊了新项目', 6, $2) RETURNING id",
            t1 + dt.timedelta(hours=1), e1,
        )
        e2 = await _insert_event(
            conn, 9602, "dialogue.gossip", "public",
            json.dumps({"teller": "A03", "listener": "A04", "about": "A01", "cites": [str(m1)],
                        "lines": [], "text_display": "听说林晚和周叙聊了新项目"}, ensure_ascii=False),
            sim_time=t1 + dt.timedelta(hours=2), actors=["A03", "A04"],
        )
        m2 = await conn.fetchval(
            "INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance, source_event_seq)"
            " VALUES ('A04', $1, 'projection', 'RAW', 'A03 说林晚和周叙聊新项目', 5, $2) RETURNING id",
            t1 + dt.timedelta(hours=3), e2,
        )
        m3 = await conn.fetchval(
            "INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance)"
            " VALUES ('A04', $1, 'reflection', 'RAW', '我反思了一下八卦本身', 7) RETURNING id",
            t1 + dt.timedelta(hours=4),
        )
        e3 = await _insert_event(
            conn, 9603, "dialogue.gossip", "public",
            json.dumps({"teller": "A04", "listener": "A05", "about": "A01", "cites": [str(m2), str(m3)],
                        "lines": [], "text_display": "听说林晚和周叙在茶水间吵了一架"}, ensure_ascii=False),
            sim_time=t2, actors=["A04", "A05"],
        )
        # relation.changed 两日各一条
        await _insert_event(
            conn, 9611, "relation.changed", "internal",
            '{"changes":[{"a_id":"A01","b_id":"A02","delta_affinity":3,"delta_tension":-1,"cause":"1"}]}',
            sim_time=t1,
        )
        await _insert_event(
            conn, 9612, "relation.changed", "internal",
            '{"changes":[{"a_id":"A01","b_id":"A02","delta_affinity":-2,"delta_tension":4,"cause":"2"}]}',
            sim_time=t2,
        )

        pool = await asyncpg.create_pool(obs_dsn, min_size=1, max_size=3)
        try:
            await obs_refresh.refresh_days(pool, [day1, day2], tmp_path, HOP_MAX)
        finally:
            await pool.close()

        assert await conn.fetchval("SELECT count(*) FROM obs.world_state_snapshot WHERE sim_day = ANY($1::date[])",
                                   [day1, day2]) == 2
        rel1 = await conn.fetchval(
            "SELECT affinity FROM obs.relation_daily WHERE sim_day=$1 AND a_id='A01' AND b_id='A02'", day1,
        )
        rel2 = await conn.fetchval(
            "SELECT affinity FROM obs.relation_daily WHERE sim_day=$1 AND a_id='A01' AND b_id='A02'", day2,
        )
        assert rel1 == 10 + 3  # 基线 10 + 当日 +3
        assert rel2 == 13 - 2  # 递推 13 + 当日 -2（05 §3.4）
        assert await conn.fetchval(
            "SELECT labels FROM obs.relation_daily WHERE sim_day=$1 AND a_id='A01' AND b_id='A02'", day2,
        ) == ["同事"]  # labels 取当日快照矩阵
        edges = await conn.fetch(
            "SELECT root_event_seq, dst_event_seq, hop, distortion FROM obs.ripple_edge ORDER BY hop",
        )
        assert len(edges) > 0
        assert all(1 <= e["hop"] <= HOP_MAX for e in edges)
        by_dst = {e["dst_event_seq"]: e for e in edges}
        assert by_dst[e2]["root_event_seq"] == e1 and by_dst[e2]["hop"] == 1
        assert by_dst[e3]["root_event_seq"] == e1 and by_dst[e3]["hop"] == 2  # 递归 hop+1
        assert 0.0 < float(by_dst[e3]["distortion"]) <= 1.0  # 归一化（分母 max(len)，D3）
        # P2-9：纯反思 m3（无 source_event_seq）不产生额外行
        assert await conn.fetchval("SELECT count(*) FROM obs.ripple_edge WHERE dst_event_seq = $1", e3) == 1
        # 幂等：重跑行数不变
        pool = await asyncpg.create_pool(obs_dsn, min_size=1, max_size=3)
        try:
            await obs_refresh.refresh_days(pool, [day1, day2], tmp_path, HOP_MAX)
        finally:
            await pool.close()
        assert await conn.fetchval("SELECT count(*) FROM obs.ripple_edge") == len(edges)
    finally:
        await conn.close()
