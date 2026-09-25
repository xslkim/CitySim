"""worldsim-ingest：摄入 API 应用工厂（07 T-SYN-05；04 §1.4 单元，开发期单进程 00 §1 A5）。

- WS 主通道 `/v1/ingest/ws`（ws.py）+ 三 HTTPS 回退端点与 `/v1/health`（routes.py）；
  `/v1/digest` 由 T-SYN-09 `ingest/digest.py` 挂载。
- DSN = `WSIM_REPLICA_PG_DSN`（副本库，07 D2）；绑定 = `WSIM_INGEST_BIND`（默认 127.0.0.1:9100）。
- 启动：`cd server && uv run uvicorn worldsim.ingest:app --host 127.0.0.1 --port 9100`
  （起停段编排归 08 T-OPS-05 `start_local.sh` 增补段，R1 §A.9）。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
from fastapi import FastAPI

from .auth import expected_token_hash
from .batch import BatchBuffer

DEFAULT_INGEST_BIND = "127.0.0.1:9100"  # 07 D2 工程默认


def replica_dsn() -> str:
    dsn = os.environ.get("WSIM_REPLICA_PG_DSN") or ""
    if not dsn:
        raise RuntimeError("摄入 API 缺 WSIM_REPLICA_PG_DSN（07 D2，00 §2 附表）")
    return dsn


def create_app(pool: Any = None, *, token_hash: str | None = None) -> FastAPI:
    app = FastAPI(title="worldsim-ingest", docs_url=None, redoc_url=None)
    app.state.pool = pool
    app.state.token_hash = token_hash  # None = lifespan 启动时从 env 解析（import 期不读 env）
    app.state.batch_buffer = BatchBuffer()
    from . import routes, ws
    app.include_router(routes.router)
    app.include_router(ws.router)
    with contextlib.suppress(ImportError):
        from . import digest  # T-SYN-09 挂载 /v1/digest（未交付时静默缺省）
        if hasattr(digest, "router"):
            app.include_router(digest.router)
    return app


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:  # pragma: no cover - uvicorn 启动路径
    if app.state.pool is None:
        app.state.pool = await asyncpg.create_pool(replica_dsn(), min_size=1, max_size=8)
    if app.state.token_hash is None:
        app.state.token_hash = expected_token_hash()
    yield
    if app.state.pool is not None:
        await app.state.pool.close()
        app.state.pool = None


app = create_app()  # uvicorn worldsim.ingest:app
app.router.lifespan_context = _lifespan
