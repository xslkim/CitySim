"""T-CFG-01 speed_table.yaml 验收：04 §3.1 两条加载期约束。

测试 basename 全库唯一（round2 §A.3 改名 test_speed_table_config.py）。
数值一律从 yaml 读，不硬编码。
"""

from __future__ import annotations

from pathlib import Path

import yaml

SPEED_TABLE_PATH = Path(__file__).resolve().parents[2] / "config" / "speed_table.yaml"


def _load() -> dict:
    with SPEED_TABLE_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _hhmm_to_min(s: str) -> int:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def test_schema_frozen_two_sections() -> None:
    cfg = _load()
    assert set(cfg.keys()) == {"segments", "constraints"}
    assert set(cfg["constraints"].keys()) == {"min_sim_days_per_real_week", "max_catchup_ratio"}
    for seg in cfg["segments"]:
        assert seg["mode"] in {"batch", "continuous"}
        assert set(seg.keys()) <= {"start", "end", "mode", "sim_hours_per_run", "ratio"}
        if seg["mode"] == "batch":
            assert "sim_hours_per_run" in seg
        else:
            assert "ratio" in seg


def test_covers_24h_no_gap() -> None:
    segs = sorted(_load()["segments"], key=lambda s: _hhmm_to_min(s["start"]))
    assert _hhmm_to_min(segs[0]["start"]) == 0, "首段必须从 00:00 开始"
    assert _hhmm_to_min(segs[-1]["end"]) == 24 * 60, "末段必须以 24:00 结束"
    for prev, nxt in zip(segs, segs[1:]):
        assert _hhmm_to_min(prev["end"]) == _hhmm_to_min(nxt["start"]), (
            f"段间空洞/重叠: {prev['start']}~{prev['end']} vs {nxt['start']}~{nxt['end']}"
        )


def test_min_sim_days_per_real_week() -> None:
    cfg = _load()
    sim_hours_per_real_day = 0.0
    for seg in cfg["segments"]:
        if seg["mode"] == "batch":
            sim_hours_per_real_day += float(seg["sim_hours_per_run"])
        else:
            real_hours = (_hhmm_to_min(seg["end"]) - _hhmm_to_min(seg["start"])) / 60
            sim_hours_per_real_day += real_hours * float(seg["ratio"])
    sim_days_per_real_week = sim_hours_per_real_day * 7 / 24
    assert sim_days_per_real_week >= cfg["constraints"]["min_sim_days_per_real_week"], (
        f"{sim_hours_per_real_day} 模拟小时/真实日 → {sim_days_per_real_week:.2f} 模拟日/周 "
        f"< {cfg['constraints']['min_sim_days_per_real_week']}"
    )
