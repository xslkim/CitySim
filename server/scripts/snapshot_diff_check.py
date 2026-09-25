#!/usr/bin/env python3
"""快照合并 diff 自检（05 T-WEB-04；03 §4.2 正确性自检进 CI 常跑 + 事件落库完整性回归）。

逐日比对：`/api/snapshot?tick=` 合并语义（`snapshot_merge.merged_state_at_tick`，
≤次日快照时点的最近快照 + 其间事件增量）vs 次日 `obs.world_state_snapshot` 的字段级 diff：
- 比对字段 = 归约覆盖的 UI 字段（03 §4.2 边界 3）：agents[].position / needs / mood；
- 任一字段不一致 → 逐条打印并非零退出；全部一致退出 0。

用法（00 §1 A14）：cd server && uv run python scripts/snapshot_diff_check.py [--dsn ...]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from typing import Any

import asyncpg

from worldsim.observe.snapshot_merge import merged_state_at_tick

COMPARE_FIELDS = ("position", "needs", "mood")  # 03 §4.2 边界 3 归约覆盖字段


async def diff_all_days(pool: Any) -> list[str]:
    snaps = await pool.fetch(
        "SELECT sim_day, state::text AS state FROM obs.world_state_snapshot ORDER BY sim_day"
    )
    problems: list[str] = []
    for i in range(1, len(snaps)):
        nxt = snaps[i]
        nxt_state = json.loads(nxt["state"])
        nxt_time = dt.datetime.fromisoformat((nxt_state.get("sim") or {}).get("sim_time"))
        tick = await pool.fetchval(
            "SELECT max(tick) FROM obs.events WHERE sim_time <= $1", nxt_time,
        )
        if tick is None:
            continue
        merged, _ = await merged_state_at_tick(pool, int(tick))
        merged_agents = {a["id"]: a for a in merged.get("agents") or []}
        for a in nxt_state.get("agents") or []:
            m = merged_agents.get(a["id"])
            if m is None:
                problems.append(f"{nxt['sim_day']} {a['id']}: 合并结果缺角色")
                continue
            for f in COMPARE_FIELDS:
                if m.get(f) != a.get(f):
                    problems.append(
                        f"{nxt['sim_day']} {a['id']}.{f}: merged={m.get(f)!r} != snapshot={a.get(f)!r}"
                    )
    return problems


async def amain(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="快照合并 vs 下一份快照字段级 diff（03 §4.2 自检）")
    ap.add_argument("--dsn", default=os.environ.get("WSIM_OBS_PG_DSN") or os.environ.get("WSIM_PG_DSN", ""))
    args = ap.parse_args(argv)
    if not args.dsn:
        print("FAIL: 缺 DSN（--dsn 或 WSIM_OBS_PG_DSN/WSIM_PG_DSN）", file=sys.stderr)
        return 1
    pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=2)
    try:
        problems = await diff_all_days(pool)
    finally:
        await pool.close()
    if problems:
        print(f"FAIL: {len(problems)} 处不一致（事件落库完整性回归，03 §4.2）")
        for p in problems[:50]:
            print(f"  {p}")
        return 1
    print("OK: 快照合并与逐日快照字段级一致")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
