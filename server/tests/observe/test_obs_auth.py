"""T-WEB-02 obs-api 骨架/token 鉴权/访问日志测试（03 §5.1/§8.1/§8.3）。

scratch 库 = schema_v1 + seed_8 + health_daily_v1 + obs_views_v1 + obs_derived_v1（M0~M4 全层）；
token/访问日志库 = tmp_path SQLite（obs-api 自管，不入仓）。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.observe.access_log import record_access, usage_by_day
from worldsim.observe.app import create_app
from worldsim.observe.auth import TOKEN_CAPACITY, TokenStore

DB_NAME = "worldsim_obs_auth_test"


@pytest.fixture(scope="module")
def obs_dsn() -> Any:
    dsn = _build_db(
        DB_NAME,
        DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql",
        DDL_DIR / "obs_views_v1.sql", DDL_DIR / "obs_derived_v1.sql",
    )
    try:
        yield dsn
    finally:
        _drop_db(DB_NAME)


@pytest_asyncio.fixture
async def client(obs_dsn: str, tmp_path: Path) -> Any:
    import httpx

    pool = await asyncpg.create_pool(obs_dsn, min_size=1, max_size=2)
    token_db = str(tmp_path / "tokens.db")
    app = create_app(pool=pool, token_db_path=token_db)
    store = TokenStore(token_db)
    token = store.issue("tester")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, token, token_db
    await pool.close()


@pytest.mark.asyncio
async def test_token_capacity_10(tmp_path: Path) -> None:
    """token 表固定 10 行容量（03 §8.3）：第 11 次签发拒发。"""
    store = TokenStore(str(tmp_path / "t.db"))
    for i in range(TOKEN_CAPACITY):
        store.issue(f"u{i}")
    with pytest.raises(Exception):
        store.issue("overflow")
    assert len([r for r in store.list() if not r["disabled"]]) == TOKEN_CAPACITY


@pytest.mark.asyncio
async def test_disabled_token_rejected(client: Any) -> None:
    """吊销后 401（03 §8.3 disabled 字段语义）。"""
    c, token, token_db = client
    r = await c.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["ok"] is True
    TokenStore(token_db).revoke(token)
    r = await c.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json()["error"]["code"]


@pytest.mark.asyncio
async def test_query_token_accepted(client: Any) -> None:
    """`?token=` 携带方式（03 §8.3 两种方式之一）。"""
    c, token, _ = client
    r = await c.get(f"/api/usage?token={token}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_no_token_401(client: Any) -> None:
    """验收 1 等价：无 token → 401 且 error.code 非空。"""
    c, _, _ = client
    r = await c.get("/api/usage")
    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False and body["error"]["code"]


@pytest.mark.asyncio
async def test_envelope_and_watermark(client: Any) -> None:
    """验收 2 等价：响应包 {ok,data,meta.watermark_tick} 且 watermark 为整数（03 §5.1）。"""
    c, token, _ = client
    r = await c.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    assert body["ok"] is True and isinstance(body["meta"]["watermark_tick"], int)


@pytest.mark.asyncio
async def test_access_log_usage_aggregation(client: Any) -> None:
    """验收 4：同 token 同日两次请求 → 聚合行 opens=2、first/last_seen_at 正确；/api/usage 返回该行。"""
    c, token, token_db = client
    auth = {"Authorization": f"Bearer {token}"}
    await c.get("/api/usage", headers=auth)
    await c.get("/api/usage", headers=auth)
    rows = usage_by_day(None, None, db_path=token_db)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    (row,) = [r for r in rows if r["token"] == token and r["day"] == today]
    assert row["opens"] == 2
    assert row["first_seen_at"] <= row["last_seen_at"]
    r = await c.get(f"/api/usage?from={today}&to={today}", headers=auth)
    assert any(x["token"] == token and x["opens"] >= 2 for x in r.json()["data"])
