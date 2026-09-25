"""T-AUD-02 审计① 余额守恒验收（08 文档 T-AUD-02 验收 1~3）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.audit.conftest import SIM_NOW, _psql_file, close_pool, make_pool
from worldsim.audit.daily import get_item, load_economy_types, run_item

SERVER_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_clean_seed_passes() -> None:
    pool = await make_pool("worldsim_audit_01a")
    try:
        res = await run_item(pool, get_item("01"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert res.ok, res.detail
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_violation_sample_reports_red() -> None:
    pool = await make_pool("worldsim_audit_01b")
    try:
        from worldsim.audit.daily import ensure_anchors
        await ensure_anchors(pool)  # 锚点须先于样例固化（生产口径=审计从第 0 天起跑，08 D9/D14）
        _psql_file(get_item("01").sample_path, "worldsim_audit_01b")
        res = await run_item(pool, get_item("01"), sim_now=SIM_NOW, sim_day=SIM_NOW.date())
        assert not res.ok and res.violations == 1
        # 样例差额 = 8800（give_gift 一档，事件有结算额而余额未动）
        assert res.detail[0]["actual"] - res.detail[0]["expected"] == 8800
    finally:
        await close_pool(pool)


def test_economy_list_matches_06_marks() -> None:
    """回归：event_types.yaml 的 economy 清单与派生 economy_audit_types.yaml 一致（防手抄漂移，
    06 §1.3 反前缀法锚点：动作域结算类型在列）。"""
    econ = load_economy_types()
    derived = yaml.safe_load((SERVER_ROOT / "config" / "economy_audit_types.yaml").read_text(encoding="utf-8"))
    assert sorted(derived["economy_types"]) == econ
    assert {"agent.eat", "agent.shop", "agent.trade_stock",
            "social.give_gift", "social.borrow_money", "social.repay_money"} <= set(econ)


@pytest.mark.asyncio
async def test_cli_only_balance() -> None:
    """audit_run.py --only balance_conservation：干净库 exit 0 / 样例库 exit 1。"""
    pool = await make_pool("worldsim_audit_01c")
    name = "worldsim_audit_01c"
    try:
        dsn = f"postgresql:///{name}?host=/tmp"
        r0 = subprocess.run([sys.executable, str(SERVER_ROOT / "scripts" / "audit_run.py"),
                             "--dsn", dsn, "--only", "balance_conservation",
                             "--sim-now", SIM_NOW.isoformat()],
                            capture_output=True, text=True, cwd=SERVER_ROOT)
        assert r0.returncode == 0, r0.stderr
        _psql_file(get_item("01").sample_path, name)
        r1 = subprocess.run([sys.executable, str(SERVER_ROOT / "scripts" / "audit_run.py"),
                             "--dsn", dsn, "--only", "balance_conservation",
                             "--sim-now", SIM_NOW.isoformat()],
                            capture_output=True, text=True, cwd=SERVER_ROOT)
        assert r1.returncode == 1, r1.stdout
    finally:
        await close_pool(pool)
