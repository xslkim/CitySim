"""T-AUD-01 审计骨架验收（08 文档 T-AUD-01 验收 2/3）：注册表 6 项、样例缺文件拒绝注册、
日报 JSON+MD 落盘、连续 3 日标红 → clock.pause 路径 + time.paused 事件。
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.audit.conftest import SIM_NOW, make_pool, close_pool
from worldsim.audit.daily import (
    REGISTRY, AuditItem, audit_and_maybe_pause, consecutive_red_days, run_daily_audit,
)


def test_registry_six_items() -> None:
    """注册 6 项且每项 SQL/样例文件齐备（缺文件即拒绝注册）。"""
    assert len(REGISTRY) == 6
    for it in REGISTRY:
        assert it.sql_path.is_file(), it.sql_path
        assert it.sample_path.is_file(), it.sample_path
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        bad = AuditItem("99_missing", "缺文件", Path(td) / "nope.sql", Path(td) / "nope2.sql")
        import asyncio

        async def _run():
            pool = await make_pool("worldsim_audit_fw1")
            try:
                from worldsim.audit.daily import run_item

                await run_item(pool, bad, sim_now=SIM_NOW, sim_day=SIM_NOW.date())
            finally:
                await close_pool(pool)

        with pytest.raises(FileNotFoundError):
            asyncio.run(_run())


@pytest.mark.asyncio
async def test_report_written_and_red(tmp_path) -> None:
    """日报 JSON+MD 落盘；任一不过 → 标红。"""
    pool = await make_pool("worldsim_audit_fw2")
    try:
        report = await run_daily_audit(pool, sim_now=SIM_NOW, report_dir=tmp_path)
        assert not report.red
        assert (tmp_path / f"{SIM_NOW.date()}.json").is_file()
        assert (tmp_path / f"{SIM_NOW.date()}.md").is_file()
        # 注入①样例 → 标红
        from tests.audit.conftest import _psql_file
        from worldsim.audit.daily import get_item

        _psql_file(get_item("01").sample_path, "worldsim_audit_fw2")
        report2 = await run_daily_audit(pool, sim_now=SIM_NOW, report_dir=tmp_path)
        assert report2.red and any(not r.ok and r.id.startswith("01") for r in report2.items)
    finally:
        await close_pool(pool)


@pytest.mark.asyncio
async def test_three_red_days_pause(tmp_path) -> None:
    """连续 3 个模拟日标红 → 调 clock.pause 路径且出现 time.paused 事件（trigger='system'，06 §1.2）。"""
    pool = await make_pool("worldsim_audit_fw3")
    try:
        from tests.audit.conftest import _psql_file
        from tests.world_agent._support import FakeClock
        from worldsim.audit.daily import get_item
        from worldsim.time_engine.clock import TimeEngine
        from worldsim.time_engine.speed_table import load as load_speed_table

        # 真 TimeEngine（pause 路径归 TIME，D8）
        clock = await TimeEngine.start(pool, load_speed_table(
            str(__import__("pathlib").Path(__file__).resolve().parents[2] / "config" / "speed_table.yaml")))
        from worldsim.audit.daily import ensure_anchors
        await ensure_anchors(pool)  # 锚点先于样例固化（08 D9/D14）
        _psql_file(get_item("01").sample_path, "worldsim_audit_fw3")  # 恒红样例
        for i in range(3):
            await audit_and_maybe_pause(
                pool, sim_now=SIM_NOW + dt.timedelta(days=i),
                pause_cb=lambda reason: clock.pause(reason), report_dir=tmp_path)
        assert consecutive_red_days(tmp_path) >= 3
        row = await pool.fetchrow(
            "SELECT type, source, trigger, payload FROM events WHERE type='time.paused' ORDER BY seq DESC LIMIT 1")
        assert row is not None and row["trigger"] == "system"
        import json

        p = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
        assert set(p.keys()) == {"reason", "at_sim"}
        assert clock.paused
    finally:
        await close_pool(pool)
