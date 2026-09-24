#!/usr/bin/env python3
"""邀约接受率统计（02 T-REL-05；01 §3.5 观测口径 / 02 文档偏差表 D6 定名）。

口径（D6，接受无独立注册事件类型）：
- **接受** = `social.appointment.created`（接受即约定成立，system 发起）；
- **送达** = `social.invite`；
- **第 1 轮 `social.invite.counter` 剔除**（不计拒绝，01 §5.2；拒绝仅计 `social.refuse`）；
- 接受率 = 接受 / 送达，7 日滑动窗口（01 §3.5：落 40~60% 区间外才动手调参，先验见 06 §3）。

执行：`cd server && uv run python scripts/invite_stats.py [--dsn $WSIM_PG_DSN] [--days 14]`
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 允许直接脚本执行时 import worldsim

from worldsim.main import _load_root_env  # noqa: E402  # 根 .env 兜底（00 §2 附表唯一清单）

_DAILY_SQL = """
SELECT date(sim_time AT TIME ZONE 'Asia/Shanghai') AS sim_day,
  count(*) FILTER (WHERE type='social.invite')                                  AS delivered,
  count(*) FILTER (WHERE type='social.appointment.created')                     AS accepted,
  count(*) FILTER (WHERE type='social.refuse')                                  AS refused,
  count(*) FILTER (WHERE type='social.invite.counter' AND payload->>'round' = '1') AS counters_r1_excluded,
  count(*) FILTER (WHERE type='social.invite.counter' AND payload->>'round' = '2') AS counters_r2,
  count(*) FILTER (WHERE type='social.appointment.stood_up')                    AS stood_up
FROM events
WHERE type IN ('social.invite','social.invite.counter','social.refuse',
               'social.appointment.created','social.appointment.stood_up')
  AND sim_time > now() - ($1 || ' days')::interval
GROUP BY 1 ORDER BY 1;
"""


async def _run(dsn: str, days: int) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_DAILY_SQL, str(days))
    finally:
        await conn.close()
    print(f"邀约接受率（口径 02 D6：接受=social.appointment.created、送达=social.invite、第 1 轮 counter 剔除）")
    print(f"{'sim_day':<12}{'送达':>6}{'接受':>6}{'拒绝':>6}{'改期R1*':>9}{'改期R2':>8}{'爽约':>6}{'接受率':>9}")
    window: list[tuple[int, int]] = []
    for r in rows:
        delivered, accepted = int(r["delivered"]), int(r["accepted"])
        window.append((delivered, accepted))
        rate = f"{accepted / delivered * 100:.1f}%" if delivered else "—"
        print(f"{r['sim_day']!s:<12}{delivered:>6}{accepted:>6}{int(r['refused']):>6}"
              f"{int(r['counters_r1_excluded']):>9}{int(r['counters_r2']):>8}{int(r['stood_up']):>6}{rate:>9}")
    tail = window[-7:]
    d, a = sum(x for x, _ in tail), sum(y for _, y in tail)
    if d:
        print(f"\n7 日滑动窗（最近 {len(tail)} 个有数据日）：接受率 {a / d * 100:.1f}%（观测区间见 01 §3.5）")
    else:
        print("\n7 日滑动窗：无送达数据")
    return 0


def main() -> int:
    _load_root_env()
    p = argparse.ArgumentParser(description="邀约接受率统计（01 §3.5 / 02 D6）")
    p.add_argument("--dsn", default=os.environ.get("WSIM_PG_DSN", ""))
    p.add_argument("--days", type=int, default=14)
    args = p.parse_args()
    if not args.dsn:
        print("缺主库 DSN：--dsn 或 env WSIM_PG_DSN", file=sys.stderr)
        return 2
    return asyncio.run(_run(args.dsn, args.days))


if __name__ == "__main__":
    sys.exit(main())
