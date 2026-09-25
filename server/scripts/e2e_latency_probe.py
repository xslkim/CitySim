#!/usr/bin/env python3
"""端到端延迟探针（05 T-WEB-20；00 §5 M4 出口：p95 ≤5s，03 §9.2 口径）。

口径：事件落库 wall_time → WS 收帧时刻的差值（毫秒）。
- 连接 `/ws`（hello+subscribe events 全量）持续收集 event 帧，记录 `recv_wall − data.wall_time`
  （wall_time = 内核落库时刻，事件出站白名单列 05 §3.1；序列化层不下发 → 探针直读 DB 对齐 seq 取 wall_time）。
- 跑满 `--duration-s`（默认覆盖 1 模拟日试跑窗口）或 `--min-samples` 后输出 p50/p95/max 与判定。

用法（00 §1 A14）：
  cd server && uv run python scripts/e2e_latency_probe.py --token dev_xxx [--duration-s 60]
退出码：p95 ≤ 5000ms → 0；否则 1（00 §5 M4 出口线）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import datetime as dt

import asyncpg
import websockets

P95_LIMIT_MS = 5000  # M4 出口线（00 §5；03 §9 口径）


async def run_probe(url: str, token: str, dsn: str, duration_s: float, min_samples: int) -> dict:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2,
                                     server_settings={"search_path": "obs"})
    latencies: list[float] = []
    try:
        async with websockets.connect(f"{url}?token={token}") as ws:
            await ws.send(json.dumps({"op": "hello", "token": token, "client": "probe/1.0.0"}))
            welcome = json.loads(await ws.recv())
            assert welcome["op"] == "welcome", welcome
            await ws.send(json.dumps({"op": "subscribe", "channels": [
                {"name": "events", "filter": {"types": [], "actors": [], "locations": [], "grades": []}}]}))

            async def pinger() -> None:  # 03 §5.2：客户端 30s ping（看门狗 90s 无帧断开）
                while True:
                    await asyncio.sleep(25)
                    try:
                        await ws.send(json.dumps({"op": "ping"}))
                    except Exception:
                        return

            ping_task = asyncio.create_task(pinger())
            deadline = asyncio.get_event_loop().time() + duration_s
            try:
                while asyncio.get_event_loop().time() < deadline and len(latencies) < 10_000:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        break
                    recv_wall = dt.datetime.now(dt.timezone.utc)
                    frame = json.loads(raw)
                    if frame.get("op") != "event":
                        continue
                    seq = frame["seq"]
                    wall = await pool.fetchval("SELECT wall_time FROM obs.events WHERE seq = $1", seq)
                    if wall is None:
                        continue
                    latencies.append((recv_wall - wall).total_seconds() * 1000)
            finally:
                ping_task.cancel()
    finally:
        await pool.close()
    latencies.sort()
    n = len(latencies)
    return {
        "samples": n,
        "p50_ms": round(statistics.median(latencies), 1) if n else None,
        "p95_ms": round(latencies[min(n - 1, int(n * 0.95))], 1) if n else None,
        "max_ms": round(latencies[-1], 1) if n else None,
        "pass": bool(n) and latencies[min(n - 1, int(n * 0.95))] <= P95_LIMIT_MS,
    }


async def amain(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="端到端延迟探针（事件落库 wall_time → WS 收帧，p95 ≤5s）")
    ap.add_argument("--url", default=f"ws://127.0.0.1:{os.environ.get('WSIM_OBS_PORT', '8080')}/ws")
    ap.add_argument("--token", default=os.environ.get("WSIM_OBS_PROBE_TOKEN", ""))
    ap.add_argument("--dsn", default=os.environ.get("WSIM_OBS_PG_DSN") or os.environ.get("WSIM_PG_DSN", ""))
    ap.add_argument("--duration-s", type=float, default=60)
    ap.add_argument("--min-samples", type=int, default=20)
    args = ap.parse_args(argv)
    if not args.token or not args.dsn:
        print("FAIL: 缺 --token 或 DSN", file=sys.stderr)
        return 1
    r = await run_probe(args.url, args.token, args.dsn, args.duration_s, args.min_samples)
    print(json.dumps(r, ensure_ascii=False))
    if not r["pass"]:
        print(f"FAIL: p95={r['p95_ms']}ms > {P95_LIMIT_MS}ms（或样本不足 {r['samples']}）", file=sys.stderr)
        return 1
    print(f"OK: p95={r['p95_ms']}ms ≤ {P95_LIMIT_MS}ms（n={r['samples']}）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
