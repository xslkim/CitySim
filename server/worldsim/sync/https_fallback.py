"""HTTPS 回退通道（07 T-SYN-04；04 §9.1 三端点，WS 不可用时走批量接口）。

- base URL = `WSIM_CLOUD_INGEST_HTTPS`（工程默认 http://127.0.0.1:9100，与摄入 API 同进程，07 D2）。
- `POST /v1/events:batch {from_seq, events[]}`（单批 ≤500）→ `acked_upto` 推进 sync_state；
  `/v1/memories:batch` → `mem_acked_upto`；`/v1/snapshot` → `snap_acked_upto`。
- 409 `{expected_seq}` → 从 expected_seq 重发（04 §9.1）；重灌帧带 mode=reproject（07 D4）。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import httpx

from .outbox import (
    fetch_events_after,
    fetch_memory_projections_after,
    read_sync_state,
    update_sync_state,
)
from .snapshot import DEFAULT_SNAPSHOT_DIR, load_snapshot_frame_from_disk, pending_snapshot_days

log = logging.getLogger(__name__)

DEFAULT_HTTPS_BASE = "http://127.0.0.1:9100"  # 07 D2


class HttpsFallback:
    """WS 断线时的批量回退发送器（与 SyncClient 共享 outbox 读取器与 sync_state）。"""

    def __init__(self, pool: Any, *, base_url: str | None = None, token: str | None = None,
                 client: httpx.AsyncClient | None = None,
                 snapshot_dir: str | None = None, batch: int = 500) -> None:
        self.pool = pool
        self.base_url = (base_url or os.environ.get("WSIM_CLOUD_INGEST_HTTPS")
                         or DEFAULT_HTTPS_BASE).rstrip("/")
        self.token = token if token is not None else os.environ.get("WSIM_INGEST_TOKEN", "")
        self.batch = batch
        self.snapshot_dir = snapshot_dir
        self._client = client

    async def _post(self, client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = await client.post(f"{self.base_url}{path}", json=body,
                              headers={"Authorization": f"Bearer {self.token}"},
                              timeout=30)
        if r.status_code == 409:
            expected = int(r.json()["expected_seq"])
            log.warning("HTTPS 409 expected_seq=%s → 重发（04 §9.1）", expected)
            raise OutOfOrder409(expected)
        r.raise_for_status()
        return r.json()

    async def sync_once(self) -> dict[str, int]:
        """一轮：events 逐批（≤500）→ memories → 快照断档；位点随响应推进。"""
        from .ws_client import encode_frame  # 复用同一帧序列化（datetime → ISO）
        import json as _json

        def loads_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return _json.loads(encode_frame({"x": rows}))["x"]

        sent = {"events": 0, "memories": 0, "snapshots": 0}
        async with (self._client or httpx.AsyncClient()) as client:
            state = await read_sync_state(self.pool)
            cursor = int(state["last_acked_seq"])
            while True:
                rows = await fetch_events_after(self.pool, cursor, limit=self.batch)
                if not rows:
                    break
                try:
                    resp = await self._post(client, "/v1/events:batch",
                                            {"from_seq": rows[0]["seq"],
                                             "events": loads_rows(rows)})
                except OutOfOrder409 as e:
                    cursor = e.expected - 1
                    continue
                cursor = int(resp["acked_upto"])
                await update_sync_state(self.pool, upto=cursor)
                sent["events"] += len(rows)
            mems = await fetch_memory_projections_after(
                self.pool, int(state["last_acked_memory_id"]), limit=self.batch)
            if mems:
                resp = await self._post(client, "/v1/memories:batch",
                                        {"from_id": mems[0]["memory_id"],
                                         "memories": loads_rows(mems)})
                await update_sync_state(self.pool, mem_upto=int(resp["mem_acked_upto"]))
                sent["memories"] = len(mems)
            state = await read_sync_state(self.pool)
            for day in pending_snapshot_days(state["last_acked_snapshot_day"],
                                             out_dir=self.snapshot_dir or DEFAULT_SNAPSHOT_DIR):
                frame = load_snapshot_frame_from_disk(
                    day, out_dir=self.snapshot_dir or DEFAULT_SNAPSHOT_DIR)
                if frame is None:
                    continue
                resp = await self._post(client, "/v1/snapshot",
                                        {"sim_day": frame["sim_day"], "digest": frame["digest"],
                                         "state": frame["state"]})
                await update_sync_state(self.pool,
                                        snap_upto=dt.date.fromisoformat(resp["snap_acked_upto"]))
                sent["snapshots"] += 1
        log.info("HTTPS 回退一轮：%s", sent)
        return sent


class OutOfOrder409(Exception):
    def __init__(self, expected: int) -> None:
        super().__init__(f"409 expected_seq={expected}")
        self.expected = expected
