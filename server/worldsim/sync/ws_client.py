"""sync ws_client（07 T-SYN-03/04；04 §1.3 全帧协议 + 恢复追平/乱序重发/HTTPS 回退）。

- 连接：`WSIM_CLOUD_INGEST_URL`（开发期 loopback ws://127.0.0.1:9100/v1/ingest/ws，07 D2）；
  upgrade 带 `Authorization: Bearer $WSIM_INGEST_TOKEN`；首帧 hello{token,from_seq,kernel_ver}。
- 攒批：events **200 条或 2s**（04 §1.3）；memories 投影同连随批；快照帧随日界/断档补发。
- ACK：`ack{upto,mem_upto,snap_upto}` 三流位点同帧回执 → UPDATE sync_state（04 §1.3）；
  ACK 语义 = 副本库事务已提交（摄入侧保证）。
- 断网态：发送超时 5s 标记 down；指数退避重连 1s→60s 封顶 ±20% 抖动；**内核不阻塞**
  （sync 独立协程，事件主库自然积压，崩溃后从 sync_state 续传）。
- 恢复态（T-SYN-04）：hello_ack.max_seq < last_acked → 按 max_seq 重发；追平模式 500 条/批、
  窗口 8 批流水化；快照断档按 sim_day 升序补发；nack/409 {expected_seq} → 从 expected_seq 重发。
- 帧落盘：`WSIM_SYNC_FRAME_DUMP=<path>` 出站帧追加 JSONL（07 D2，必过项①抓包证据源）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets

from .outbox import (
    fetch_events_after,
    fetch_memory_projections_after,
    read_sync_state,
    update_sync_state,
)
from .snapshot import DEFAULT_SNAPSHOT_DIR, load_snapshot_frame_from_disk, pending_snapshot_days

log = logging.getLogger(__name__)

KERNEL_VER = "0.6.0"
SCHEMA_VERSION = 1

BATCH_SIZE = 200            # 04 §1.3：200 条
BATCH_WINDOW_S = 2.0        #           或 2s
CATCHUP_BATCH = 500         # 追平 500 条/批
CATCHUP_WINDOW = 8          #       窗口 8 批流水
SEND_TIMEOUT_S = 5.0        # 发送超时标记 down
BACKOFF_BASE_S = 1.0        # 退避 1s→60s 封顶 ±20% 抖动
BACKOFF_MAX_S = 60.0

DEFAULT_INGEST_URL = "ws://127.0.0.1:9100/v1/ingest/ws"  # 07 D2 开发期 loopback


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    raise TypeError(f"不可序列化: {type(obj)}")


def encode_frame(frame: dict[str, Any]) -> str:
    return json.dumps(frame, ensure_ascii=False, default=_jsonable)


class SyncClient:
    """出站同步协程（注入 sleep/monotonic/rng 以便假时钟测试）。"""

    def __init__(
        self, pool: Any, *,
        url: str | None = None, token: str | None = None,
        frame_dump: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        batch_size: int = BATCH_SIZE, batch_window_s: float = BATCH_WINDOW_S,
        catchup_batch: int = CATCHUP_BATCH, catchup_window: int = CATCHUP_WINDOW,
        send_timeout_s: float = SEND_TIMEOUT_S,
        snapshot_dir: str | Path | None = None,
        connect: Callable[..., Any] = websockets.connect,
    ) -> None:
        self.pool = pool
        self.url = url or os.environ.get("WSIM_CLOUD_INGEST_URL") or DEFAULT_INGEST_URL
        self.token = token if token is not None else os.environ.get("WSIM_INGEST_TOKEN", "")
        self.frame_dump = frame_dump if frame_dump is not None else os.environ.get(
            "WSIM_SYNC_FRAME_DUMP") or None
        self.sleep = sleep
        self.monotonic = monotonic
        self.rng = rng or random.Random(0)
        self.batch_size = batch_size
        self.batch_window_s = batch_window_s
        self.catchup_batch = catchup_batch
        self.catchup_window = catchup_window
        self.send_timeout_s = send_timeout_s
        self.snapshot_dir = snapshot_dir
        self._connect = connect
        self.down = False
        self.backoff_s = BACKOFF_BASE_S
        self._ws: Any = None
        self._cursor_seq = 0          # 发送游标（≥ 持久位点；nack/409 回退只动游标）
        self._pending_events: list[dict[str, Any]] = []   # 攒批缓冲（跨轮询周期保持，04 §1.3 2s 窗口）
        self._pending_since: float | None = None

    # ---- 帧 IO ----------------------------------------------------------------

    def _dump_frame(self, frame: dict[str, Any]) -> None:
        if self.frame_dump:
            with open(self.frame_dump, "a", encoding="utf-8") as f:
                f.write(encode_frame(frame) + "\n")

    async def _send(self, frame: dict[str, Any]) -> None:
        frame.setdefault("schema_version", SCHEMA_VERSION)
        self._dump_frame(frame)
        await asyncio.wait_for(self._ws.send(encode_frame(frame)), timeout=self.send_timeout_s)

    async def _recv(self, timeout: float | None = None) -> dict[str, Any]:
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout or self.send_timeout_s)
        return json.loads(raw)

    # ---- 连接 / 恢复 ------------------------------------------------------------

    async def connect(self) -> dict[str, Any]:
        """建连 + hello/hello_ack；返回 hello_ack（恢复态判定依据）。"""
        state = await read_sync_state(self.pool)
        self._cursor_seq = max(self._cursor_seq, int(state["last_acked_seq"]))
        self._ws = await self._connect(
            self.url, additional_headers={"Authorization": f"Bearer {self.token}"})
        await self._send({"frame": "hello", "token": self.token, "from_seq": self._cursor_seq,
                          "kernel_ver": KERNEL_VER})
        ack = await self._recv()
        if ack.get("frame") == "nack":
            raise ConnectionError(f"hello 被拒：{ack.get('reason')}")
        self.down = False
        self.backoff_s = BACKOFF_BASE_S
        # 恢复态：副本缺数据 → 按 max_seq 重发（04 §1.3）
        cloud_max = int(ack.get("max_seq") or 0)
        if cloud_max < self._cursor_seq:
            log.warning("副本 max_seq=%s < 本机位点=%s → 重发（04 §1.3 恢复态）",
                        cloud_max, self._cursor_seq)
            self._cursor_seq = cloud_max
        return ack

    def next_backoff(self) -> float:
        """指数退避 1s→60s 封顶 ±20% 抖动（04 §1.3）。"""
        delay = self.backoff_s * (1 + self.rng.uniform(-0.2, 0.2))
        self.backoff_s = min(self.backoff_s * 2, BACKOFF_MAX_S)
        return delay

    async def _close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 - 关闭路径尽力而为
                pass
            self._ws = None

    # ---- ACK / NACK -------------------------------------------------------------

    async def _apply_ack(self, ack: dict[str, Any]) -> None:
        upto, mem_upto, snap = ack.get("upto"), ack.get("mem_upto"), ack.get("snap_upto")
        if upto is not None:
            self._cursor_seq = max(self._cursor_seq, int(upto))
        await update_sync_state(
            self.pool,
            upto=int(upto) if upto is not None else None,
            mem_upto=int(mem_upto) if mem_upto is not None else None,
            snap_upto=dt.date.fromisoformat(snap) if snap else None)

    # ---- 发送侧 ------------------------------------------------------------------

    async def send_events_batch(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """发一帧 batch 并等 ack（返回 ack 帧；nack 由调用方处理）。"""
        frame = {"frame": "batch", "from": events[0]["seq"], "to": events[-1]["seq"],
                 "events": events}
        await self._send(frame)
        log.info("sync batch from=%s to=%s n=%d", frame["from"], frame["to"], len(events))
        return await self._recv()

    async def send_mem_batch(self, memories: list[dict[str, Any]]) -> dict[str, Any]:
        frame = {"frame": "mem_batch", "from": memories[0]["memory_id"],
                 "to": memories[-1]["memory_id"], "memories": memories}
        await self._send(frame)
        log.info("sync mem_batch from=%s to=%s n=%d", frame["from"], frame["to"], len(memories))
        return await self._recv()

    async def send_snapshot_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        await self._send(frame)
        log.info("sync snapshot sim_day=%s", frame.get("sim_day"))
        return await self._recv()

    async def send_snapshots_pending(self) -> int:
        """快照断档按 sim_day 升序补发（04 §1.3 恢复态）；返回补发帧数。"""
        state = await read_sync_state(self.pool)
        days = pending_snapshot_days(state["last_acked_snapshot_day"],
                                     out_dir=self.snapshot_dir or DEFAULT_SNAPSHOT_DIR)
        sent = 0
        for day in days:
            frame = load_snapshot_frame_from_disk(
                day, out_dir=self.snapshot_dir or DEFAULT_SNAPSHOT_DIR)
            if frame is None:
                continue
            ack = await self.send_snapshot_frame(frame)
            if ack.get("frame") == "ack":
                await self._apply_ack(ack)
                sent += 1
        return sent

    async def sync_once(self) -> dict[str, int]:
        """一个轮询周期：正常态攒批（200 条或 2s）/ 追平模式（500×8 流水）+ memories + ack 处理。

        返回 {'events': n_sent, 'memories': n_sent}。断网/异常向上抛（run_forever 捕获进退避）。
        """
        state = await read_sync_state(self.pool)
        backlog = await self.pool.fetchval("SELECT count(*) FROM events WHERE seq > $1",
                                           self._cursor_seq)
        sent_e = sent_m = 0
        if backlog and backlog > self.catchup_batch:  # 积压超 500 进追平模式（04 §1.3）
            sent_e = await self._catchup(int(backlog))
        else:
            sent_e = await self._normal_events()
        mem_state = await read_sync_state(self.pool)
        mems = await fetch_memory_projections_after(
            self.pool, int(mem_state["last_acked_memory_id"]), limit=self.batch_size)
        if mems:
            ack = await self.send_mem_batch(mems)
            await self._handle_reply(ack)
            if ack.get("frame") == "ack":
                sent_m = len(mems)
        return {"events": sent_e, "memories": sent_m}

    async def _normal_events(self) -> int:
        """正常态：攒批 200 条立即发；不足 200 在 2s 窗口到点即发（窗口自首批入行起算）。"""
        pending = self._pending_events
        sent = 0
        while True:
            # 读取位点 = max(ack 游标, 攒批缓冲末行)：已入缓冲未 ack 的行不重复拉取
            fetch_after = pending[-1]["seq"] if pending else self._cursor_seq
            rows = await fetch_events_after(self.pool, fetch_after,
                                            limit=self.batch_size - len(pending))
            if rows:
                pending.extend(rows)
                if self._pending_since is None:
                    self._pending_since = self.monotonic()
            if not pending:
                self._pending_since = None
                return sent
            full = len(pending) >= self.batch_size
            expired = (self._pending_since is not None
                       and (self.monotonic() - self._pending_since) >= self.batch_window_s)
            if full or expired:
                batch = pending[:self.batch_size]
                del pending[:self.batch_size]
                ack = await self.send_events_batch(batch)
                await self._handle_reply(ack)
                if ack.get("frame") != "ack":
                    return sent
                sent += len(batch)
                self._pending_since = self.monotonic() if pending else None
            else:
                return sent  # 未满且窗口未到点 → 等下一周期

    async def _catchup(self, backlog: int) -> int:
        """追平模式：500 条/批、窗口 8 批流水化（04 §1.3/§9.2）。"""
        sent = 0
        in_flight: list[tuple[int, int]] = []  # (from, to) 未 ack 批
        while sent < backlog or in_flight:
            while sent < backlog and len(in_flight) < self.catchup_window:
                rows = await fetch_events_after(self.pool, self._cursor_seq,
                                                limit=self.catchup_batch)
                if not rows:
                    break
                frame = {"frame": "batch", "from": rows[0]["seq"], "to": rows[-1]["seq"],
                         "events": rows}
                await self._send(frame)
                in_flight.append((rows[0]["seq"], rows[-1]["seq"]))
                self._cursor_seq = rows[-1]["seq"]  # 乐观推进；nack 回退
                sent += len(rows)
            if not in_flight:
                break
            reply = await self._recv()
            if reply.get("frame") == "ack" and reply.get("upto") is not None:
                upto = int(reply["upto"])
                in_flight = [b for b in in_flight if b[1] > upto]
                await self._apply_ack(reply)
            else:
                await self._handle_reply(reply)
                if reply.get("frame") == "nack":
                    return sent - sum(b[1] - b[0] + 1 for b in in_flight)
        return sent

    async def _handle_reply(self, reply: dict[str, Any]) -> None:
        """ack → 推进 sync_state；nack{from|expected_seq} → WARN 记日志 + 游标回退重发（04 §9.1）。"""
        if reply.get("frame") == "ack":
            await self._apply_ack(reply)
        elif reply.get("frame") == "nack":
            rewind = reply.get("expected_seq") or reply.get("from") or 0
            if reply.get("reason") == "schema":
                log.error("摄入 nack（schema 类，整批）：%s", reply)
            else:
                log.warning("摄入 nack：%s → 从 %s 重发", reply, rewind)
            self._cursor_seq = min(self._cursor_seq, max(0, int(rewind) - 1))

    # ---- 主循环 ------------------------------------------------------------------

    async def run_forever(self, *, poll_s: float = 0.5, https_fallback: Any = None) -> None:
        """连接 → sync_once 循环；断网退避重连；WS 长期不可用走 HTTPS 回退（T-SYN-04）。"""
        ws_failures = 0
        while True:
            try:
                await self.connect()
                ws_failures = 0
                await self.send_snapshots_pending()
                while True:
                    await self.sync_once()
                    await self.sleep(poll_s)
            except (ConnectionError, OSError, asyncio.TimeoutError,
                    websockets.exceptions.WebSocketException) as e:
                if not self.down:
                    log.warning("sync 通道 down（%s）→ 退避重连；内核不阻塞（04 §1.3）", e)
                self.down = True
                await self._close()
                ws_failures += 1
                if https_fallback is not None and ws_failures >= 2:
                    try:
                        await https_fallback.sync_once()
                        continue
                    except Exception as fe:  # noqa: BLE001 - 回退也失败则退避
                        log.warning("HTTPS 回退亦失败（%s）", fe)
                await self.sleep(self.next_backoff())
