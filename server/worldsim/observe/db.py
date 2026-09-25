"""obs-api DB 层（05 T-WEB-02）：只读连接池 + watermark。

- DSN 取 `WSIM_OBS_PG_DSN`，缺省回退 `WSIM_PG_DSN`（00 §2 附表 D5）；连接 `search_path=obs`
  （M4 读主库 obs schema 白名单视图层；M6 仅换 DSN 指副本库，00 §1 A5）。
- 只读守卫：连接带 `default_transaction_read_only=on`——obs-api 一切数据来自白名单视图
  （00 §4 红线 12），写操作在驱动层即拒绝（角色层 obs_ro 零 DML 由 DDL 授权保证）。
- `watermark_tick` = `SELECT max(tick) FROM obs.events`（M4 语义 = 主库最大 tick，05 文档 D4；
  M6 切副本后自然获得副本延迟语义）。
"""

from __future__ import annotations

import os
from typing import Any

import asyncpg

DEFAULT_OBS_PORT = 8080  # 03 §8.1（obs-api 端口工程默认）


def obs_dsn() -> str:
    dsn = os.environ.get("WSIM_OBS_PG_DSN") or os.environ.get("WSIM_PG_DSN") or ""
    if not dsn:
        raise RuntimeError("obs-api 缺 DSN（WSIM_OBS_PG_DSN / WSIM_PG_DSN，00 §2 附表）")
    return dsn


def obs_port() -> int:
    return int(os.environ.get("WSIM_OBS_PORT", DEFAULT_OBS_PORT))


async def create_pool(dsn: str | None = None) -> Any:
    return await asyncpg.create_pool(
        dsn or obs_dsn(),
        min_size=1,
        max_size=8,
        server_settings={
            "search_path": "obs",
            "default_transaction_read_only": "on",
        },
    )


async def watermark_tick(pool: Any) -> int:
    """响应包 meta.watermark_tick（03 §5.1）；空库 = 0。"""
    v = await pool.fetchval("SELECT max(tick) FROM obs.events")
    return int(v or 0)
