#!/usr/bin/env python3
"""canonical.py — events 流 digest 对账 CLI（02 T-ADJ-09；05 §2.2 公式、04 §9.2）。

唯一实现归 `worldsim/snapshot/canonical.py`（本脚本只是其 CLI 壳；07 T-SYN-01/09 消费方同 import）。

用法（00 §1 A14，cwd = server/）：

    uv run python scripts/canonical.py --sim-day 1            # 打印该模拟日 {count, sum_seq, digest}
    uv run python scripts/canonical.py --sim-day 1 --expect sha256:…   # 对账：不一致退出码 1

模拟日界 = 本地时区 date(sim_time)（与 llm_calls_simday D12-b 口径一致）。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

import asyncpg

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from worldsim.main import _load_root_env  # noqa: E402
from worldsim.snapshot.canonical import LOCAL_TZ, digest_events  # noqa: E402


async def _main(args: argparse.Namespace) -> int:
    _load_root_env()
    dsn = args.dsn or os.environ.get("WSIM_PG_DSN")
    if not dsn:
        print("缺主库 DSN：--dsn 或 env WSIM_PG_DSN（根 .env）", file=sys.stderr)
        return 2
    # --sim-day N：以 world_state clock.anchor 的 tick0_sim 为第 1 模拟日界
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        anchor_raw = await pool.fetchval("SELECT value FROM world_state WHERE key='clock.anchor'")
        anchor = json.loads(anchor_raw) if isinstance(anchor_raw, str) else dict(anchor_raw or {})
        tick0 = dt.datetime.fromisoformat(anchor.get("tick0_sim") or anchor["anchor_sim"]).astimezone(LOCAL_TZ)
        sim_day = (tick0 + dt.timedelta(days=args.sim_day - 1)).date()
        report = await digest_events(pool, sim_day)
    finally:
        await pool.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.expect is not None:
        if report["digest"] != args.expect:
            print(f"FAIL：digest 不符（expect {args.expect}，actual {report['digest']}）", file=sys.stderr)
            return 1
        print("OK：digest 一致")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="canonical.py", description="events 流 canonical digest 对账（02 T-ADJ-09）")
    p.add_argument("--sim-day", type=int, required=True, help="模拟日序号（1 起，以 clock.anchor tick0_sim 为界）")
    p.add_argument("--expect", default=None, help="期望 digest（sha256:…）；不一致退出码 1")
    p.add_argument("--dsn", default=None, help="主库 DSN（缺省 env WSIM_PG_DSN，根 .env 兜底）")
    return asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
