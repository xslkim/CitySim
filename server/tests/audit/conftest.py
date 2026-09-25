"""tests/audit 共享支撑（08 T-AUD-01 交付 `server/tests/audit/conftest.py`）。

私有 scratch 库惯例同 tests/adjudicator；审计样例注入走 psql（样例 SQL 即唯一违规构造）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import asyncpg

from tests.conftest import DDL_DIR, PG_BIN, SOCKET_DIR, _build_db, _drop_db, _psql_file
from worldsim.time_engine.clock import LOCAL_TZ

SIM_NOW = dt.datetime(2028, 3, 13, 12, 0, tzinfo=LOCAL_TZ)  # 审计基准时点（周一中午）

__all__ = ["DDL_DIR", "PG_BIN", "SOCKET_DIR", "SIM_NOW", "make_pool", "close_pool"]


_POOL_DB: dict[int, str] = {}


async def make_pool(name: str) -> Any:
    """函数级独立 scratch 库（schema_v1 + seed_8 + health_daily_v1 增量），close_pool 配对 drop。"""
    dsn = _build_db(name, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    _POOL_DB[id(pool)] = name
    return pool


async def anchor_now(pool: Any) -> dt.datetime:
    """样例 SQL 的 sim_time 锚点（clock.anchor 的 anchor_sim）+1h——样例与本审计 sim_now 同窗口径。"""
    import json

    row = await pool.fetchrow("SELECT value FROM world_state WHERE key='clock.anchor'")
    v = row["value"]
    v = json.loads(v) if isinstance(v, str) else dict(v)
    return dt.datetime.fromisoformat(v["anchor_sim"]).astimezone(LOCAL_TZ) + dt.timedelta(hours=1)


async def close_pool(pool: Any) -> None:
    name = _POOL_DB.pop(id(pool))
    await pool.close()
    _drop_db(name)
