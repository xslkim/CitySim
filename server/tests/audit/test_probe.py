"""T-OPS-03 致命故障探测验收（04 §3.2：检测 → clock.pause → 恢复；事件 trigger/payload 06 §1.2）。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from worldsim.audit import alerts
from worldsim.audit.probe import FatalProbe, consume_throttle_escalation
from worldsim.time_engine.clock import TimeEngine
from worldsim.time_engine.speed_table import load as load_speed_table

pytestmark = pytest.mark.asyncio


class FakePool:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok

    async def fetchval(self, *_a: Any, **_k: Any) -> int:
        if not self.ok:
            raise ConnectionError("db down")
        return 1


class Recorder:
    def __init__(self) -> None:
        self.pauses: list[str] = []
        self.resumes: list[str] = []

    async def pause(self, reason: str) -> None:
        self.pauses.append(reason)

    async def resume(self, reason: str) -> None:
        self.resumes.append(reason)


@pytest.fixture
def alerts_file(tmp_path):
    path = tmp_path / "alerts.log"
    alerts.set_path(path)
    yield path
    alerts.set_path(None)


async def test_db_down_pauses_clock(alerts_file) -> None:
    """验收 1a：主库不可用 → pause + ERROR 告警立即落行。"""
    rec = Recorder()
    probe = FatalProbe(FakePool(ok=False), pause_cb=rec.pause, resume_cb=rec.resume)
    faults = await probe.check_once()
    assert faults == ["db"] and probe.paused
    assert rec.pauses and "db" in rec.pauses[0]
    rows = [json.loads(line) for line in alerts_file.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["level"] == "ERROR" and rows[0]["key"] == "probe.db"


async def test_all_providers_broken_pauses() -> None:
    """验收 1b：全 provider 熔断 → pause。"""
    rec = Recorder()
    probe = FatalProbe(FakePool(), pause_cb=rec.pause, providers_ok=lambda: False)
    assert await probe.check_once() == ["providers"]
    assert rec.pauses


async def test_disk_full_pauses() -> None:
    """验收 1c：磁盘满（可用 < 暂停线）→ pause。"""
    rec = Recorder()

    class Usage:
        total = 100
        free = 2

    probe = FatalProbe(FakePool(), pause_cb=rec.pause, disk_pause_free_pct=5.0,
                       disk_usage=lambda _p: Usage())
    assert await probe.check_once() == ["disk"]
    assert rec.pauses


async def test_resume_after_probe_ok(alerts_file) -> None:
    """验收 1d：故障清除 → resume_cb 恢复（追平档由 TIME 执行）。"""
    pool = FakePool(ok=False)
    rec = Recorder()
    probe = FatalProbe(pool, pause_cb=rec.pause, resume_cb=rec.resume)
    await probe.check_once()
    assert probe.paused
    pool.ok = True
    assert await probe.check_once() == []
    assert not probe.paused and rec.resumes == ["故障清除恢复"]


async def test_pause_event_payload_contract(test_db_dsn: str) -> None:
    """验收 3：time.paused 事件 trigger='system'，payload 键 = {reason, at_sim}（06 §1.2 逐字）。"""
    pool = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=2)
    try:
        from pathlib import Path
        table = load_speed_table(str(Path(__file__).resolve().parents[2] / "config"
                                       / "speed_table.yaml"))
        clock = await TimeEngine.start(pool, table, unthrottled=True)
        probe = FatalProbe(FakePool(ok=False), pause_cb=clock.pause)
        await probe.check_once()
        assert clock.paused
        row = await pool.fetchrow(
            "SELECT source, trigger, visibility, payload FROM events"
            " WHERE type='time.paused' ORDER BY seq DESC LIMIT 1")
        assert row["trigger"] == "system"
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        assert set(payload) == {"reason", "at_sim"}
        await clock.resume("测试恢复")
    finally:
        await pool.close()


async def test_throttle_24h_escalation_consumed(test_db_dsn: str) -> None:
    """T-OPS-02 验收 3 衔接：needs_clock_pause 标志位 → consume_throttle_escalation → time.paused。"""
    from worldsim.llm_gateway.breaker import ThrottleState

    state = ThrottleState(level="throttle", needs_clock_pause=True)
    pool = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=2)
    try:
        from pathlib import Path
        table = load_speed_table(str(Path(__file__).resolve().parents[2] / "config"
                                       / "speed_table.yaml"))
        clock = await TimeEngine.start(pool, table, unthrottled=True)
        baseline = await pool.fetchval("SELECT count(*) FROM events WHERE type='time.paused'")
        fired = await consume_throttle_escalation(state, clock.pause)
        assert fired and clock.paused
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='time.paused'") == baseline + 1
        # 标志未置位 → 不触发
        assert await consume_throttle_escalation(ThrottleState(), clock.pause) is False
    finally:
        await pool.close()
