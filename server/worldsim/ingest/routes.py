"""摄入 API HTTPS 端点（07 T-SYN-05；04 §9.1 端点表）：

- `POST /v1/events:batch`   body `{from_seq, events[]}`（单批 ≤500）→ `{acked_upto}`
- `POST /v1/memories:batch` body `{from_id, memories[]}` → `{mem_acked_upto}`
- `POST /v1/snapshot`       body `{sim_day, digest, state}` → `{snap_acked_upto}`
- `POST /v1/state:latest`   body `{tick, sim_time, digest, state}` → `{latest_acked}`（R1 #1 当日滚动）
- `GET  /v1/health`         副本库 max(seq) / 摄入延迟 / 最近 digest 校验结果（digest_log 归 T-SYN-09 D5）

`/v1/digest` 归 T-SYN-09（ingest/digest.py），不在本模块。
鉴权：全端点 `Authorization: Bearer`（哈希比对，auth.py）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .auth import bearer_token, token_ok
from .batch import BatchBuffer, BatchReject, ingest_memories, ingest_snapshot, ingest_state_latest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


async def require_ingest_token(request: Request) -> None:
    token = bearer_token(request.headers.get("authorization"))
    if not token_ok(token, expected_hash=request.app.state.token_hash):
        return _unauthorized()


def _unauthorized() -> None:
    from fastapi import HTTPException
    raise HTTPException(status_code=401, detail="无效或缺失 Bearer token（04 §9.1）")


def _buffer(request: Request) -> BatchBuffer:
    return request.app.state.batch_buffer


@router.post("/events:batch")
async def events_batch(request: Request, body: dict[str, Any],
                       _auth: None = Depends(require_ingest_token)) -> JSONResponse:
    from_seq = int(body.get("from_seq", 0))
    events = body.get("events") or []
    if len(events) > 500:
        return JSONResponse(status_code=400,
                            content={"error": "单批 >500 条（04 §9.1）", "reason": "schema"})
    try:
        acked, reset = await _buffer(request).submit(
            request.app.state.pool, from_seq, events, mode=body.get("mode"))
    except BatchReject as e:
        log.error("events:batch 整批拒绝（%s）from=%s", e, from_seq)
        content: dict[str, Any] = {"error": str(e), "reason": e.reason}
        if e.expected_seq is not None:
            content["expected_seq"] = e.expected_seq
        return JSONResponse(status_code=e.status, content=content)
    log.info("sync batch acked_upto=%s（https 回退，from=%s n=%d）", acked, from_seq, len(events))
    content = {"acked_upto": acked}
    if reset:
        content["reset"] = True  # R3 #2：副本已重建，本机游标回退续传
    return JSONResponse(content=content)


@router.post("/memories:batch")
async def memories_batch(request: Request, body: dict[str, Any],
                         _auth: None = Depends(require_ingest_token)) -> JSONResponse:
    from_id = int(body.get("from_id", 0))
    memories = body.get("memories") or []
    try:
        acked = await ingest_memories(request.app.state.pool, from_id, memories)
    except BatchReject as e:
        log.error("memories:batch 整批拒绝（%s）from_id=%s", e, from_id)
        content = {"error": str(e), "reason": e.reason}
        if e.expected_seq is not None:
            content["expected_seq"] = e.expected_seq
        return JSONResponse(status_code=e.status, content=content)
    log.info("sync mem_batch mem_acked_upto=%s（https 回退，from_id=%s n=%d）",
             acked, from_id, len(memories))
    return JSONResponse(content={"mem_acked_upto": acked})


@router.post("/snapshot")
async def snapshot(request: Request, body: dict[str, Any],
                   _auth: None = Depends(require_ingest_token)) -> JSONResponse:
    try:
        day = await ingest_snapshot(request.app.state.pool, body.get("sim_day"),
                                    str(body.get("digest") or ""), body.get("state") or {})
    except BatchReject as e:
        log.error("snapshot 拒收（%s）sim_day=%s", e, body.get("sim_day"))
        return JSONResponse(status_code=e.status, content={"error": str(e), "reason": e.reason})
    except (TypeError, ValueError) as e:
        return JSONResponse(status_code=400, content={"error": str(e), "reason": "schema"})
    log.info("sync snapshot sim_day=%s 落库", day)
    return JSONResponse(content={"snap_acked_upto": day})


@router.post("/state:latest")
async def state_latest(request: Request, body: dict[str, Any],
                       _auth: None = Depends(require_ingest_token)) -> JSONResponse:
    """当日滚动 latest 帧（R1 #1；WS 主通道同语义）：digest 复核 + 单行 upsert。"""
    try:
        await ingest_state_latest(request.app.state.pool, body.get("tick"), body.get("sim_time"),
                                  str(body.get("digest") or ""), body.get("state") or {})
    except BatchReject as e:
        log.error("state:latest 拒收（%s）tick=%s", e, body.get("tick"))
        return JSONResponse(status_code=e.status, content={"error": str(e), "reason": e.reason})
    except (TypeError, ValueError) as e:
        return JSONResponse(status_code=400, content={"error": str(e), "reason": "schema"})
    log.info("sync state_latest tick=%s 落库（https 回退）", body.get("tick"))
    return JSONResponse(content={"latest_acked": True})


@router.get("/health")
async def health(request: Request, _auth: None = Depends(require_ingest_token)) -> dict[str, Any]:
    """04 §9.1：副本库 max(seq) / 摄入延迟（秒）/ 最近 digest 校验结果（digest_log，T-SYN-09 D5）。"""
    pool = request.app.state.pool
    max_seq = int(await pool.fetchval("SELECT COALESCE(max(seq), 0) FROM events"))
    lag = await pool.fetchval("SELECT EXTRACT(EPOCH FROM (now() - max(wall_time))) FROM events")
    last_digest = None
    if await pool.fetchval("SELECT to_regclass('public.digest_log')") is not None:
        row = await pool.fetchrow(
            "SELECT sim_day, status, checked_at FROM digest_log ORDER BY checked_at DESC LIMIT 1")
        if row:
            last_digest = {"sim_day": row["sim_day"].isoformat(), "status": row["status"],
                           "checked_at": row["checked_at"].isoformat()}
    return {"max_seq": max_seq,
            "ingest_lag_seconds": round(float(lag), 3) if lag is not None else None,
            "last_digest": last_digest}
