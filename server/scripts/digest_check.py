#!/usr/bin/env python3
"""手动 digest 校验入口（07 T-SYN-09；T-SYN-11 演练调用；04 §9.2 全窗口逐日比对）。

用法：`cd server && uv run python scripts/digest_check.py`（DSN 读环境变量
WSIM_PG_DSN / WSIM_REPLICA_PG_DSN，不打印）。窗口截止 T−2（T = 主库最大模拟日）。
逐日打印 PASS/FAIL/SKIP；任一 FAIL 退出码 1。比对结果写副本 digest_log（D5）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys

import asyncpg

from worldsim.ingest.digest import compare_day, replica_day_digest
from worldsim.sync.digest import LOCAL_TZ, build_push_body, in_compare_window


async def main() -> int:
    main_dsn = os.environ.get("WSIM_PG_DSN")
    replica_dsn = os.environ.get("WSIM_REPLICA_PG_DSN")
    if not main_dsn or not replica_dsn:
        print("缺 WSIM_PG_DSN / WSIM_REPLICA_PG_DSN", file=sys.stderr)
        return 2
    mp = await asyncpg.connect(main_dsn)
    rp = await asyncpg.connect(replica_dsn)
    try:
        days = sorted({r["sim_time"].astimezone(LOCAL_TZ).date() for r in
                       await mp.fetch("SELECT sim_time FROM events")})
        if not days:
            print("主库无事件，无可比对日")
            return 0
        today = days[-1]
        fails = 0
        for day in days:
            if not in_compare_window(day, today):
                print(f"{day}: SKIP（T−1/T 窗口外不比对，05 §2.2）")
                continue
            local = await build_push_body(mp, day)
            remote = await replica_day_digest(rp, day)
            diffs = compare_day(local, remote)
            status = "FAIL" if diffs else "PASS"
            fails += bool(diffs)
            print(f"{day}: {status}" + (f" 字段={diffs}" if diffs else ""))
            import json as _json
            try:
                await rp.execute(
                    "INSERT INTO digest_log (sim_day, status, detail) VALUES ($1,$2,$3::jsonb)",
                    day, "mismatch" if diffs else "ok",
                    _json.dumps({"diffs": diffs, "via": "digest_check.py"}, ensure_ascii=False))
            except asyncpg.UndefinedTableError:
                pass  # digest_log 未建（replica_init.sh 未重跑）时仅打印
        return 1 if fails else 0
    finally:
        await mp.close()
        await rp.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
