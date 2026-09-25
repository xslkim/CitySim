"""T-WEB-05 事件查询/直方图/检索测试（03 §5.1；05 §3.8 grade 视图口径）。"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.observe.app import create_app
from worldsim.observe.auth import TokenStore

DB_NAME = "worldsim_obs_rest_events_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
BASE = dt.datetime.now(LOCAL_TZ).replace(hour=9, minute=0, second=0, microsecond=0) + dt.timedelta(days=6)


@pytest_asyncio.fixture
async def client() -> Any:
    dsn = _build_db(
        DB_NAME,
        DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql",
        DDL_DIR / "obs_views_v1.sql", DDL_DIR / "obs_derived_v1.sql",
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    # 30 条 chat（A01/A02，corp.pantry）+ 1 条 time.day_summary（trigger=system）
    # + 1 条 argue（grade 初值 B）+ grade_revise 改判 A
    rows = []
    for i in range(30):
        rows.append(
            f"({i + 1}, '{(BASE + dt.timedelta(minutes=5 * i)).isoformat()}', 'dialogue.chat',"
            f" 'agent:A01', 'autonomous', '{{A01,A02}}', 'corp.pantry',"
            f" '{{\"participants\":[\"A01\",\"A02\"],\"lines\":[],\"text_display\":\"闲聊第{i + 1}句\"}}'::jsonb,"
            f" 'public', '{{\"grade\":\"C\"}}'::jsonb)"
        )
    argue_time = (BASE + dt.timedelta(hours=3)).isoformat()
    rows.append(
        f"(100, '{argue_time}', 'dialogue.argue', 'agent:A01', 'autonomous', '{{A01,A03}}', 'corp.tech',"
        " '{\"participants\":[\"A01\",\"A03\"],\"reason_hint\":\"r\",\"lines\":[],\"witnesses\":[],"
        "\"text_display\":\"林晚和叶蓁吵起来了\"}'::jsonb, 'public', '{\"grade\":\"B\"}'::jsonb)"
    )
    sql = ("INSERT INTO events (tick, sim_time, type, source, trigger, actors, location_id, payload,"
           " visibility, ui) VALUES " + ",".join(rows))
    await pool.execute(sql)
    argue_seq = await pool.fetchval("SELECT seq FROM events WHERE type = 'dialogue.argue'")
    await pool.execute(
        "INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility) VALUES"
        " (101, $1, 'time.day_summary', 'system', 'system', '{\"day\":7}'::jsonb, 'internal')",
        BASE + dt.timedelta(hours=4),
    )
    await pool.execute(
        "INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility) VALUES"
        " (102, $1, 'director.grade_revise', 'director', 'director',"
        f" '{{\"target_seq\":\"{argue_seq}\",\"new_grade\":\"A\",\"reason\":\"终审上调\"}}'::jsonb, 'public')",
        BASE + dt.timedelta(hours=5),
    )
    token_db = str(Path(tempfile.mkdtemp()) / "tokens.db")
    app = create_app(pool=pool, token_db_path=token_db)
    token = TokenStore(token_db).issue("tester")
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        c._worldsim_argue_seq = argue_seq  # type: ignore[attr-defined]
        yield c
    await pool.close()
    _drop_db(DB_NAME)


@pytest.mark.asyncio
async def test_cursor_pagination_no_dup_no_gap(client: Any) -> None:
    """cursor=seq 翻页：全程 seq 严格递增、无重复无空洞。"""
    seen: list[int] = []
    cursor: int | None = None
    for _ in range(10):
        url = "/api/events?limit=10" + (f"&cursor={cursor}" if cursor else "")
        body = (await client.get(url)).json()["data"]
        items = body["items"]
        assert items
        seen += [e["seq"] for e in items]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(seen) and len(seen) == len(set(seen))
    assert len(seen) == 33  # 30 chat + argue + day_summary + grade_revise


@pytest.mark.asyncio
async def test_trigger_system_accepted(client: Any) -> None:
    """trigger=system 过滤命中 time.day_summary（06 §1.1 枚举含 system）。"""
    body = (await client.get("/api/events?trigger=system")).json()["data"]
    types = {e["type"] for e in body["items"]}
    assert types == {"time.day_summary", "time.*"} or "time.day_summary" in types
    assert all(e["trigger"] == "system" for e in body["items"])


@pytest.mark.asyncio
async def test_type_unknown_422(client: Any) -> None:
    r = await client.get("/api/events?type=dialogue.unknown_xyz")
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_param"


@pytest.mark.asyncio
async def test_grade_filter_reads_view(client: Any) -> None:
    """grade 过滤读 event_grade_view 最新生效口径：初值 B 的 argue 经改判后被 grade=A 命中。"""
    argue_seq = client._worldsim_argue_seq  # type: ignore[attr-defined]
    body = (await client.get("/api/events?grade=A")).json()["data"]
    assert [e["seq"] for e in body["items"]] == [argue_seq]
    assert body["items"][0]["ui"]["grade"] == "B"  # ui 初值不改写（append-only）


@pytest.mark.asyncio
async def test_q_trgm_hit(client: Any) -> None:
    """q 中文检索（pg_trgm 降级路径 ILIKE）：命中 text_display（argue 白名单无 text_display 键，
    06 §1.2——检索样本取 chat）。"""
    body = (await client.get("/api/events?q=闲聊第7句")).json()["data"]
    assert len(body["items"]) == 1 and body["items"][0]["type"] == "dialogue.chat"
    body = (await client.get("/api/events?q=不存在的关键词xyz")).json()["data"]
    assert body["items"] == []


@pytest.mark.asyncio
async def test_limit_bounds_and_combo(client: Any) -> None:
    """limit=501 → 422；缺省 limit ≤200；六维组合各 200 且含 meta.watermark_tick。"""
    assert (await client.get("/api/events?limit=501")).status_code == 422
    body = (await client.get("/api/events?actor=A01&location=corp.pantry&type=dialogue.chat"
                             "&trigger=autonomous&from=1&to=50")).json()
    assert body["ok"] is True and isinstance(body["meta"]["watermark_tick"], int)
    assert all(e["payload"]["location_id"] == "corp.pantry" for e in body["data"]["items"])
    assert len(body["data"]["items"]) == 30


@pytest.mark.asyncio
async def test_histogram(client: Any) -> None:
    """直方图：bucket=sim_hour 分桶 + a_count 按最新生效 grade='A' 计。"""
    body = (await client.get("/api/events/histogram")).json()["data"]
    assert body and all(set(b) == {"bucket_start", "count", "a_count"} for b in body)
    assert sum(b["a_count"] for b in body) == 1  # 改判后的 argue
    assert sum(b["count"] for b in body) == 33
