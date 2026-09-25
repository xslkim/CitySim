#!/usr/bin/env python
"""审计手动入口（08 T-AUD-01；00 §2 布局登记脚本名）。

用法（cwd = server/，00 §1 A14）：
  uv run python scripts/audit_run.py [--dsn ...] [--only <id>] [--sim-now <ISO>]
  uv run python scripts/audit_run.py --self-test      # 样例立法门禁：逐样例注入临时库断言必报红
exit code：0 = 全绿 / 1 = 有红或样例失效 / 2 = 参数或环境错误。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import asyncpg

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from worldsim.audit.daily import (  # noqa: E402
    REGISTRY, REPORT_DIR, get_item, run_daily_audit, run_item,
)
from worldsim.time_engine.clock import LOCAL_TZ  # noqa: E402

PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"


def _sim_now_from_anchor(value: dict) -> dt.datetime:
    """now_sim() 由 TIME 口径提供（08 D10）：anchor_sim + (wall_now − anchor_wall) × ratio。"""
    anchor_sim = dt.datetime.fromisoformat(value["anchor_sim"])
    anchor_wall = dt.datetime.fromisoformat(value["anchor_wall"])
    ratio = float(value.get("ratio", 1.0))
    if value.get("paused_at"):
        return anchor_sim + (dt.datetime.fromisoformat(value["paused_at"]) - anchor_wall) * ratio
    return anchor_sim + (dt.datetime.now(LOCAL_TZ) - anchor_wall) * ratio


async def _resolve_sim_now(pool, override: str | None) -> dt.datetime:
    if override:
        return dt.datetime.fromisoformat(override).astimezone(LOCAL_TZ)
    row = await pool.fetchrow("SELECT value FROM world_state WHERE key='clock.anchor'")
    if row is None:
        return dt.datetime.now(LOCAL_TZ)
    import json

    value = row["value"]
    return _sim_now_from_anchor(json.loads(value) if isinstance(value, str) else dict(value))


def _psql_file(path: Path, db: str) -> None:
    subprocess.run([os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db,
                    "-v", "ON_ERROR_STOP=1", "-f", str(path)],
                   check=True, capture_output=True, text=True)


async def _self_test() -> int:
    """样例立法门禁（04 §10.1 头部）：逐样例注入独立临时库 → 对应审计必报红、其余项不受影响。"""
    ddl = SERVER_ROOT / "ddl"
    failures: list[str] = []
    for item in REGISTRY:
        if not item.sample_path.is_file():
            failures.append(f"{item.id}: 缺样例文件 {item.sample_path}")
            continue
        db = f"worldsim_audit_st_{item.id[:2]}"
        subprocess.run([os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", "postgres",
                        "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)",
                        "-c", f"CREATE DATABASE {db}"],
                       check=True, capture_output=True, text=True)
        try:
            for sql in ("CREATE SCHEMA IF NOT EXISTS partman",
                        "CREATE EXTENSION IF NOT EXISTS vector",
                        "CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman"):
                subprocess.run([os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db,
                                "-c", sql], check=True, capture_output=True, text=True)
            _psql_file(ddl / "schema_v1.sql", db)
            _psql_file(ddl / "seed_8.sql", db)
            dsn = f"postgresql:///{db}?host={SOCKET_DIR}"
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
            try:
                sim_now = await _resolve_sim_now(pool, None)
                # 干净库前提：本项应绿
                clean = await run_item(pool, item, sim_now=sim_now, sim_day=sim_now.date())
                if not clean.ok:
                    failures.append(f"{item.id}: 干净库未过（样例前提失败）: {clean.detail[:2]}")
                    continue
                # 注入样例 → 本项必报红
                _psql_file(item.sample_path, db)
                red = await run_item(pool, item, sim_now=sim_now, sim_day=sim_now.date())
                if red.ok:
                    failures.append(f"{item.id}: 样例未触发报红（死检查，04 §10.1 样例立法）")
                    continue
                # 其余项不受影响
                for other in REGISTRY:
                    if other.id == item.id:
                        continue
                    res = await run_item(pool, other, sim_now=sim_now, sim_day=sim_now.date())
                    if not res.ok:
                        failures.append(f"{item.id}: 样例污染了 {other.id}: {res.detail[:1]}")
            finally:
                await pool.close()
        finally:
            subprocess.run([os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", "postgres",
                            "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)"],
                           check=True, capture_output=True, text=True)
    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    if failures:
        return 1
    print(f"OK: {len(REGISTRY)} 项审计样例全部触发报红且互不污染")
    return 0


async def _run(args) -> int:
    dsn = args.dsn or os.environ.get("WSIM_PG_DSN")
    if not dsn:
        print("缺主库 DSN：--dsn 或 env WSIM_PG_DSN", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        sim_now = await _resolve_sim_now(pool, args.sim_now)
        report = await run_daily_audit(pool, sim_now=sim_now, only=args.only)
        for r in report.items:
            print(f"{'PASS' if r.ok else 'RED '} {r.id} {r.name} 违规 {r.violations}")
        print(f"日报落盘：{REPORT_DIR / (report.sim_day + '.json')}")
        return 1 if report.red else 0
    finally:
        await pool.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="WorldSim 每日审计（04 §10.1）")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--only", default=None, help="只跑单项（注册表 id）")
    ap.add_argument("--sim-now", default=None, help="覆盖 sim_now（ISO；缺省从 clock.anchor 推算）")
    ap.add_argument("--self-test", action="store_true", help="样例立法门禁（04 §10.1 头部）")
    args = ap.parse_args()
    if args.self_test:
        return asyncio.run(_self_test())
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
