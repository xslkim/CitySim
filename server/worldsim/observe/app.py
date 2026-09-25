"""obs-api FastAPI 应用骨架（05 T-WEB-02；03 §5.1 响应包/错误约定、§8.1 访问日志、§8.3 鉴权）。

- 响应包统一 `{ok, data, meta:{watermark_tick}}`；错误 `{ok:false, error:{code,message}}`。
- 鉴权依赖 `require_token`：`/api/*` 一律要求有效 token（`?token=` 或 `Authorization: Bearer`，
  03 §8.3）；通过即写访问日志（token/时间/端点，不含事件内容，03 §8.1）。WS `/ws` 的 hello 帧
  鉴权归 `ws.py`（T-WEB-07，同样写访问日志）。
- 路由组：`rest_snapshot`/`rest_agents`（T-WEB-03）、`snapshot_merge`（T-WEB-04）、
  `rest_events`（T-WEB-05）、`rest_ripple`/`rest_relations`/`rest_health`（T-WEB-06）、
  `ws`（T-WEB-07）随任务挂载。
- 启动：`cd server && uv run uvicorn worldsim.observe.app:app --port 8080`
  （端口 env `WSIM_OBS_PORT` 默认 8080，03 §8.1）。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .access_log import record_access, usage_by_day
from .auth import TokenStore, extract_token
from .db import create_pool, watermark_tick


class ApiError(Exception):
    """协议错误（03 §5.1 错误包）；status 默认 400。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def error_body(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


async def ok_envelope(pool: Any, data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    m = {"watermark_tick": await watermark_tick(pool)}
    if meta:
        m.update(meta)
    return {"ok": True, "data": data, "meta": m}


def get_pool(request: Request) -> Any:
    return request.app.state.pool


def get_token_store(request: Request) -> TokenStore:
    return request.app.state.token_store


async def require_token(request: Request) -> dict:
    """鉴权依赖（03 §8.3）：失败 401；通过写访问日志并返回 token 行。"""
    token = extract_token(request.query_params.get("token"), request.headers.get("authorization"))
    row = request.app.state.token_store.verify(token or "")
    if row is None:
        raise ApiError("unauthorized", "无效或缺失 token（03 §8.3）", status=401)
    record_access(row["token"], endpoint=request.url.path, db_path=request.app.state.token_db_path)
    return row


def create_app(pool: Any = None, token_db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="worldsim-obs-api", docs_url=None, redoc_url=None)
    app.state.pool = pool
    app.state.token_db_path = token_db_path
    app.state.token_store = TokenStore(token_db_path)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message))

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content=error_body("internal", str(exc)))

    @app.get("/api/usage")
    async def api_usage(
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None),
        _token_row: dict = Depends(require_token),
        _pool: Any = Depends(get_pool),
    ) -> dict[str, Any]:
        """门禁④度量（03 §5.1）：访问日志 token×自然日聚合（管理鉴权 = 有效 token，M4 开发期口径）。"""
        return await ok_envelope(_pool, usage_by_day(from_, to, db_path=app.state.token_db_path))

    # 随任务挂载的 REST/WS 路由（T-WEB-03~07；模块未交付时静默缺省）
    for modname in ("rest_snapshot", "rest_agents", "rest_events", "rest_ripple",
                    "rest_relations", "rest_health", "ws"):
        with contextlib.suppress(ImportError):
            mod = __import__(f"worldsim.observe.{modname}", fromlist=["router"])
            app.include_router(mod.router)

    return app


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:  # pragma: no cover - uvicorn 启动路径
    if app.state.pool is None:
        app.state.pool = await create_pool()
    yield
    if app.state.pool is not None:
        await app.state.pool.close()
        app.state.pool = None


app = create_app()
app.router.lifespan_context = _lifespan
