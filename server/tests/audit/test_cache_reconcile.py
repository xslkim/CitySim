"""T-AUD-07 审计⑥ 缓存对账验收（08 文档 T-AUD-07 验收 1~2）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.audit.conftest import SIM_NOW, _psql_file, close_pool, make_pool
from worldsim.audit.daily import get_item, run_item, sample_agents

SERVER_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_clean_passes() -> None:
    pool = await make_pool("worldsim_audit_06a")
    try:
        res = await run_item(pool, get_item("06"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert res.ok, res.detail
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_violation_sample_reports_red() -> None:
    """样例（篡改全员 needs 缓存）→ 被抽 5 人必在其中 → 必报红。"""
    pool = await make_pool("worldsim_audit_06b")
    try:
        from worldsim.audit.daily import ensure_anchors
        await ensure_anchors(pool)  # 锚点先固化
        _psql_file(get_item("06").sample_path, "worldsim_audit_06b")
        res = await run_item(pool, get_item("06"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert not res.ok and any(d["kind"] == "cache_mismatch" for d in res.detail)
    finally:
        await close_pool(pool)


def test_sample_is_deterministic() -> None:
    """同库同日抽样名单一致（固定种子可复现；不同日名单允许不同）。"""
    ids = [f"A{i:02d}" for i in range(1, 9)]
    a = sample_agents(ids, SIM_NOW.date())
    b = sample_agents(ids, SIM_NOW.date())
    assert a == b and len(a) == 5
    import datetime as dt

    c = sample_agents(ids, SIM_NOW.date() + dt.timedelta(days=1))
    assert len(c) == 5  # 名单写进日报即可复现核对


@pytest.mark.asyncio
async def test_cli_only_cache_reconcile() -> None:
    """audit_run.py --only cache_reconcile：干净库 exit 0 / 样例库 exit 1。"""
    name = "worldsim_audit_06c"
    pool = await make_pool(name)
    try:
        dsn = f"postgresql:///{name}?host=/tmp"
        cmd = [sys.executable, str(SERVER_ROOT / "scripts" / "audit_run.py"),
               "--dsn", dsn, "--only", "cache_reconcile", "--sim-now", SIM_NOW.isoformat()]
        r0 = subprocess.run(cmd, capture_output=True, text=True, cwd=SERVER_ROOT)
        assert r0.returncode == 0, r0.stderr
        _psql_file(get_item("06").sample_path, name)
        r1 = subprocess.run(cmd, capture_output=True, text=True, cwd=SERVER_ROOT)
        assert r1.returncode == 1
    finally:
        await close_pool(pool)
