"""T-CFG-07 health_thresholds.yaml 验收：指标集合恰为 01 §9 七行、三档数值逐项相等。"""

from __future__ import annotations

from pathlib import Path

import yaml

THRESHOLDS_PATH = Path(__file__).resolve().parents[1] / "config" / "health_thresholds.yaml"

# 01 §9 阈值表七行（指标 key 为工程命名，三档数值与 01 §9 逐字相等）
EXPECTED = {
    "a_grade_event_interval_days": {
        "healthy": {"max": 1},
        "warning": {"min": 1, "max": 2},
        "alarm": {"min": 2},
        "window": "7d_mean",
        "unit": "sim_day",
    },
    "event_type_entropy_bits": {
        "healthy": {"min": 2.4},
        "warning": {"min": 2.0, "max": 2.4},
        "alarm": {"max": 2.0},
        "window": "7d_mean",
        "unit": "bit",
    },
    "appearance_gini": {
        "healthy": {"max": 0.45},
        "warning": {"min": 0.45, "max": 0.6},
        "alarm": {"min": 0.6},
        "window": "sim_week",
        "unit": "ratio",
    },
    "dialogue_3gram_repeat_ratio": {
        "healthy": {"max": 0.08},
        "warning": {"min": 0.08, "max": 0.12},
        "alarm": {"min": 0.12},
        "window": "7d",
        "unit": "ratio",
    },
    "relation_graph_weekly_change_ratio": {
        "healthy": {"min": 0.08, "max": 0.25},
        "warning": {"low": {"min": 0.05, "max": 0.08}, "high": {"min": 0.25, "max": 0.40}},
        "alarm": {"low": {"max": 0.05}, "high": {"min": 0.40}},
        "window": "sim_week",
        "unit": "ratio",
    },
    "high_tension_edge_ratio": {
        "healthy": {"max": 0.08},
        "warning": {"min": 0.08, "max": 0.15},
        "alarm": {"min": 0.15},
        "window": "sim_day",
        "unit": "ratio",
    },
    "active_conflict_edges": {
        "healthy": {"min_total": 12, "min_per_star": 1},
        "warning": {"total_range": [8, 12], "star_zero_count": 1},
        "alarm": {"max_total": 8, "min_star_zero_count": 2},
        "window": "sim_day",
        "unit": "count",
    },
}


def _metrics() -> dict[str, dict]:
    with THRESHOLDS_PATH.open(encoding="utf-8") as f:
        return {m["metric"]: m for m in yaml.safe_load(f)["metrics"]}


def test_metric_set_complete() -> None:
    assert set(_metrics().keys()) == set(EXPECTED.keys()), "指标集合须恰为 01 §9 七行"


def test_thresholds_match_01_9() -> None:
    metrics = _metrics()
    for name, expected in EXPECTED.items():
        m = metrics[name]
        for field in ("healthy", "warning", "alarm", "window", "unit"):
            assert m[field] == expected[field], f"{name}.{field}: {m[field]} != {expected[field]}"


def test_header_ownership_comment() -> None:
    head = THRESHOLDS_PATH.read_text(encoding="utf-8").splitlines()[:5]
    assert any("01 §9" in line and "唯一持有方" in line for line in head), "文件头须含 01 §9 持有方声明"
