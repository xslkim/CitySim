"""摄入 API WS 主通道（07 T-SYN-05；04 §1.3 帧协议全帧族）。

帧族：hello/hello_ack、batch、mem_batch、snapshot、ack、nack、ping/pong。
- 鉴权：upgrade `Authorization: Bearer` 优先，hello 帧 token 兜底（04 §1.3 帧样例两形态并存）。
- **ACK 语义 = 副本库事务已提交**：ack 帧仅在摄入事务提交后发送（04 §1.3；验收 8 故障注入路径）。
- hello_ack 回 `{max_seq, mem_max, snap_max}`（恢复态判定依据；mem/snap 两值为工程增补，
  04 §1.3 只钉 max_seq，增补键向后兼容）。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import bearer_token, token_ok
from .batch import BatchReject, ingest_memories, ingest_snapshot, ingest_state_latest

log = logging.getLogger(__name__)

router = APIRouter()


async def _send(ws: WebSocket, frame: dict[str, Any]) -> None:
    await ws.send_json(frame)


@router.websocket("/v1/ingest/ws")
async def ingest_ws(ws: WebSocket) -> None:
    expected_hash = ws.app.state.token_hash
    header_ok = token_ok(bearer_token(ws.headers.get("authorization")), expected_hash=expected_hash)
    await ws.accept()
    authed = header_ok
    try:
        while True:
            msg = await ws.receive_json()
            frame = msg.get("frame")
            if frame == "ping":
                await _send(ws, {"frame": "pong", "schema_version": 1})
                continue
            if frame == "hello":
                if not authed and token_ok(str(msg.get("token") or ""), expected_hash=expected_hash):
                    authed = True
                if not authed:
                    await _send(ws, {"frame": "nack", "from": 0, "reason": "auth"})
                    await ws.close(code=4401)
                    return
                pool = ws.app.state.pool
                snap = await pool.fetchval("SELECT max(sim_day) FROM world_state_snapshot")
                await _send(ws, {
                    "frame": "hello_ack", "schema_version": 1,
                    "max_seq": int(await pool.fetchval("SELECT COALESCE(max(seq),0) FROM events")),
                    "mem_max": int(await pool.fetchval(
                        "SELECT COALESCE(max(memory_id),0) FROM memory_projection")),
                    "snap_max": snap.isoformat() if isinstance(snap, dt.date) else None,
                })
                continue
            if not authed:
                await _send(ws, {"frame": "nack", "from": 0, "reason": "auth"})
                continue
            await _handle_data_frame(ws, msg)
    except WebSocketDisconnect:
        return


async def _handle_data_frame(ws: WebSocket, msg: dict[str, Any]) -> None:
    """batch / mem_batch / snapshot：事务提交成功才发 ack（三流位点同帧回执）。"""
    pool = ws.app.state.pool
    frame = msg.get("frame")
    try:
        if frame == "batch":
            events = msg.get("events") or []
            acked, reset = await ws.app.state.batch_buffer.submit(
                pool, int(msg.get("from", 0)), events, mode=msg.get("mode"))
            log.info("sync batch ack upto=%s（ws，from=%s n=%d）", acked, msg.get("from"),
                     len(events))
            ack: dict[str, Any] = {"frame": "ack", "upto": acked, "mem_upto": None,
                                   "snap_upto": None}
            if reset:
                ack["reset"] = True  # R3 #2：副本已重建，本机游标回退续传
                log.error("sync batch 触发时间线分叉重建 → ack 带 reset（upto=%s）", acked)
            await _send(ws, ack)
        elif frame == "mem_batch":
            mems = msg.get("memories") or []
            acked = await ingest_memories(pool, int(msg.get("from", 0)), mems)
            log.info("sync mem_batch ack mem_upto=%s（ws，n=%d）", acked, len(mems))
            await _send(ws, {"frame": "ack", "upto": None, "mem_upto": acked, "snap_upto": None})
        elif frame == "snapshot":
            day = await ingest_snapshot(pool, msg.get("sim_day"), str(msg.get("digest") or ""),
                                        msg.get("state") or {})
            log.info("sync snapshot ack snap_upto=%s（ws）", day)
            await _send(ws, {"frame": "ack", "upto": None, "mem_upto": None, "snap_upto": day})
        elif frame == "state_latest":
            await ingest_state_latest(pool, msg.get("tick"), msg.get("sim_time"),
                                      str(msg.get("digest") or ""), msg.get("state") or {})
            log.info("sync state_latest ack（ws，tick=%s）", msg.get("tick"))
            await _send(ws, {"frame": "ack", "upto": None, "mem_upto": None, "snap_upto": None})
        else:
            await _send(ws, {"frame": "nack", "from": msg.get("from", 0),
                             "reason": f"unknown_frame:{frame}"})
    except BatchReject as e:
        # schema 类 nack 记 ERROR（04 §9.1）；乱序 409 记 WARN 由本机从重发位点恢复
        (log.error if e.reason in ("schema", "digest") else log.warning)(
            "摄入 nack（%s）：%s", e.reason, e)
        nack: dict[str, Any] = {"frame": "nack", "from": msg.get("from", 0), "reason": e.reason}
        if e.expected_seq is not None:
            nack["expected_seq"] = e.expected_seq
        await _send(ws, nack)
