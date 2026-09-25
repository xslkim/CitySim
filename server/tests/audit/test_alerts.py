"""T-OPS-01 告警通道验收（04 §12.1：WARN 聚合 15 分钟 / ERROR 立即；JSON Lines）。"""

from __future__ import annotations

import json

import pytest

from worldsim.audit import alerts


@pytest.fixture
def alerts_file(tmp_path):
    path = tmp_path / "alerts.log"
    alerts.set_path(path)
    yield path
    alerts.set_path(None)


def test_error_immediate(alerts_file) -> None:
    """ERROR 级立即落行。"""
    alerts.alert("ERROR", "probe.db_down", "主库不可用", {"detail": "x"})
    lines = alerts_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["level"] == "ERROR" and row["key"] == "probe.db_down"
    assert set(row) == {"wall_time", "level", "key", "message", "context"}


def test_warn_aggregates_15min(alerts_file) -> None:
    """同 key 两条 WARN 聚合为一条且计数=2；不同 key 各自成行。"""
    alerts.alert("WARN", "cost.throttle", "进入降速档")
    alerts.alert("WARN", "cost.throttle", "持续降速")
    alerts.alert("WARN", "sync.lag", "副本滞后")
    assert not alerts_file.exists() or not alerts_file.read_text().strip()  # 窗内未落盘
    n = alerts.flush_warnings()
    assert n == 2
    rows = [json.loads(line) for line in alerts_file.read_text(encoding="utf-8").splitlines()]
    agg = next(r for r in rows if r["key"] == "cost.throttle")
    assert "×2" in agg["message"]
    assert any(r["key"] == "sync.lag" for r in rows)


def test_json_lines_parseable(alerts_file) -> None:
    """每行可独立 JSON 解析且字段齐全。"""
    alerts.alert("ERROR", "audit.balance_conservation", "审计标红", {"day": "2026-10-12"})
    alerts.alert("WARN", "k", "m")
    alerts.flush_warnings()
    for line in alerts_file.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert row["wall_time"] and row["level"] in ("WARN", "ERROR")
        assert isinstance(row["context"], dict)
