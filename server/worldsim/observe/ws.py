"""obs-api WS 通道 `/ws`（05 T-WEB-07；03 §5.2 全帧形态、00 §4 红线 2 seq 续传）。

- hello（token + 可选 last_seq）→ welcome（watermark_tick/server_time/session_id）；
  token 无效 → 关闭（4401）；hello 通过写访问日志（03 §8.1，仅 token/端点，不含事件内容）。
- subscribe channels：events（filter types/actors/locations/grades，空=全量；grades 匹配最新生效
  grade = obs.event_grade_view，05 §3.8）/world_state（state_diff 500ms 合帧）/health（每分钟一次）。
- 新事件感知 = 轮询主库 500ms（工程默认，对齐 03 §6.3 合帧节拍，05 文档 D6①）；
  event 帧逐条推；state_diff 从事件流派生（state_diff.py）。
- resume：缺口 ≤ 内存环缓冲（容量 10,000，03 §5.2）直接补推接 live；超缓冲 →
  `{op:"resync_required"}`（客户端走 /api/snapshot 重建，03 §5.2）。
- 心跳：客户端 30s ping、服务端 pong；90s 无客户端帧断开（03 §5.2）；重连退避在客户端。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections import deque
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .access_log import record_access
from .db import watermark_tick
from .rest_events import registered_types
from .serde import serialize_event
from .state_diff import derive_state_diff

router = APIRouter()

POLL_INTERVAL_S = 0.5        # 轮询/合帧节拍（D6①，对齐 03 §6.3）
RING_CAPACITY = 10_000       # 环缓冲容量（03 §5.2；实测回填复核 05 §5）
HEALTH_INTERVAL_S = 60.0     # health 频道每分钟一次（03 §5.2）
HEARTBEAT_TIMEOUT_S = 90.0   # 90s 无客户端帧断开（03 §5.2）
BATCH_LIMIT = 500            # 单批推送上限

# 进程级环缓冲（app 实例共享；喂入方 = 各连接轮询循环去重写入）
RING: deque[dict[str, Any]] = deque(maxlen=RING_CAPACITY)
_RING_LOCK = asyncio.Lock()


def _matches_filter(ev: dict[str, Any], filt: dict[str, Any] | None) -> bool:
    """events 频道过滤（空=全量；grades 在 SQL 侧已按 event_grade_view 过滤，此处兜底）。"""
    if not filt:
        return True
    types = filt.get("types") or []
    actors = filt.get("actors") or []
    locations = filt.get("locations") or []
    if types and ev["type"] not in types:
        return False
    if locations and (ev.get("payload") or {}).get("location_id") not in locations:
        return False
    if actors:
        ev_actors = (ev.get("payload") or {}).get("participants") or []
        # actors 数组列在 serialize 后不外显；由调用方附带 _actors 内部键（不出站键，发送前剥除）
        ev_actors = ev.get("_actors") or ev_actors
        if not set(ev_actors) & set(actors):
            return False
    grades = filt.get("grades") or []
    if grades and ev.get("_grade") not in grades:
        return False
    return True


def _strip_internal(ev: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ev.items() if not k.startswith("_")}


async def _fetch_events_after(pool: Any, last_seq: int, limit: int = BATCH_LIMIT) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT e.*, g.grade AS eff_grade FROM obs.events e
        LEFT JOIN obs.event_grade_view g ON g.seq = e.seq
        WHERE e.seq > $1 ORDER BY e.seq LIMIT $2
        """,
        last_seq, limit,
    )
    out = []
    for r in rows:
        ev = serialize_event(r)
        ev["_actors"] = list(r["actors"] or [])
        ev["_grade"] = r["eff_grade"]
        out.append(ev)
    return out


async def _feed_ring(events: list[dict[str, Any]]) -> None:
    async with _RING_LOCK:
        for ev in events:
            if not RING or ev["seq"] > RING[-1]["seq"]:
                RING.append(ev)


async def _resume(ws: WebSocket, pool: Any, last_seq: int, ring_capacity: int) -> tuple[bool, int]:
    """resume-from-seq（03 §5.2）：缺口 ≤ 环缓冲容量 → 直接补推接 live；超容量 → resync_required
    （客户端走 /api/snapshot 重建）。返回（可否接 live, 补推后的新 last_seq——防 live 段重推）。"""
    db_max = int(await pool.fetchval("SELECT max(seq) FROM obs.events") or 0)
    if db_max - last_seq > ring_capacity:
        await ws.send_text(json.dumps({"op": "resync_required"}, ensure_ascii=False))
        return False, last_seq
    events = await _fetch_events_after(pool, last_seq, limit=ring_capacity)
    for ev in events:
        await ws.send_text(json.dumps(
            {"op": "event", "seq": ev["seq"], "data": _strip_internal(ev)}, ensure_ascii=False))
    return True, max(last_seq, db_max)


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    app = websocket.app
    pool = app.state.pool
    # 工程节拍默认值（测试经 app.state 覆盖；生产值 = 03 §5.2/§6.3 口径）
    poll_interval = float(getattr(app.state, "ws_poll_interval", POLL_INTERVAL_S))
    heartbeat_timeout = float(getattr(app.state, "ws_heartbeat_timeout", HEARTBEAT_TIMEOUT_S))
    health_interval = float(getattr(app.state, "ws_health_interval", HEALTH_INTERVAL_S))
    ring_capacity = int(getattr(app.state, "ws_ring_capacity", RING_CAPACITY))
    last_rx = dt.datetime.now().timestamp()
    subscribed: dict[str, dict[str, Any] | None] = {}
    last_seq = 0
    greeted = False

    async def poll_loop() -> None:
        nonlocal last_seq
        last_health = 0.0
        while True:
            await asyncio.sleep(poll_interval)
            try:
                events = await _fetch_events_after(pool, last_seq)
                if events:
                    await _feed_ring(events)
                    for ev in events:
                        last_seq = max(last_seq, ev["seq"])
                        if "events" in subscribed and _matches_filter(ev, subscribed["events"]):
                            await websocket.send_text(json.dumps(
                                {"op": "event", "seq": ev["seq"], "data": _strip_internal(ev)},
                                ensure_ascii=False))
                    if "world_state" in subscribed:
                        diff = derive_state_diff(events, events[-1]["tick"])
                        if diff:
                            await websocket.send_text(json.dumps(
                                {"op": "state_diff", "seq": events[-1]["seq"], "data": diff},
                                ensure_ascii=False))
                if "health" in subscribed and dt.datetime.now().timestamp() - last_health >= health_interval:
                    from .rest_health import health_payload
                    await websocket.send_text(json.dumps(
                        {"op": "health", "seq": last_seq, "data": await health_payload(pool)},
                        ensure_ascii=False, default=str))
                    last_health = dt.datetime.now().timestamp()
            except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                raise
            except Exception:  # 单笔推送失败不杀轮询循环
                continue

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(min(5.0, heartbeat_timeout / 2 or 5.0))
            if dt.datetime.now().timestamp() - last_rx > heartbeat_timeout:
                await websocket.close(code=4408)  # 90s 无心跳断开（03 §5.2）
                return

    poll_task: asyncio.Task | None = None
    watchdog_task = asyncio.create_task(watchdog())
    try:
        while True:
            raw = await websocket.receive_text()
            last_rx = dt.datetime.now().timestamp()
            frame = json.loads(raw)
            op = frame.get("op")
            if op == "hello":
                token_row = app.state.token_store.verify(str(frame.get("token") or ""))
                if token_row is None:
                    await websocket.close(code=4401)  # token 错误的 hello → 连接关闭（验收 3）
                    return
                record_access(token_row["token"], endpoint="/ws",
                              db_path=app.state.token_db_path)
                greeted = True
                req_last = frame.get("last_seq")
                if req_last is not None:
                    last_seq = int(req_last)
                    ok, last_seq = await _resume(websocket, pool, last_seq, ring_capacity)
                    if not ok:
                        continue  # resync_required 已下发，等客户端重建后重新 hello/subscribe
                else:
                    last_seq = await pool.fetchval("SELECT max(seq) FROM obs.events") or 0
                    last_seq = int(last_seq)
                await websocket.send_text(json.dumps({
                    "op": "welcome",
                    "watermark_tick": await watermark_tick(pool),
                    "server_time": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "session_id": f"s_{uuid.uuid4().hex[:12]}",
                }, ensure_ascii=False))
            elif op == "subscribe":
                if not greeted:
                    await websocket.close(code=4400)
                    return
                for ch in frame.get("channels") or []:
                    name = ch.get("name")
                    if name not in ("events", "world_state", "health"):
                        continue
                    filt = ch.get("filter") if name == "events" else None
                    if filt and filt.get("types"):
                        filt = {**filt,
                                "types": [t for t in filt["types"] if t in registered_types()]}
                    subscribed[name] = filt
                if poll_task is None:
                    poll_task = asyncio.create_task(poll_loop())
            elif op == "ping":
                await websocket.send_text(json.dumps(
                    {"op": "pong", "t": frame.get("t")}, ensure_ascii=False))
            else:
                await websocket.send_text(json.dumps(
                    {"op": "error", "error": {"code": "bad_op", "message": f"未知 op：{op!r}"}},
                    ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        watchdog_task.cancel()
        if poll_task is not None:
            poll_task.cancel()
