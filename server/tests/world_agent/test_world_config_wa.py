"""T-WA-01 world.yaml 追加段加载校验（04 文档 T-WA-01 验收 1/2；与 01 T-CFG-03 的
tests/test_world_config.py 不同目录不同名，basename 全库唯一，R2 §A.3）。

纪律：生产代码零硬编码阈值（校验仅结构性）；本文件中断言 01 §6.1 节假日表一一对应
的条目镜像属"配置↔设计对拍"，与 01 T-CFG-03 既有测试同例。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from worldsim.world_agent.config import WorldConfigError, load_world_config

WORLD_PATH = Path(__file__).resolve().parents[2] / "config" / "world.yaml"

# 01 §6.1 节假日表一一对应镜像（名称 → 天数；仅对拍用，不进入生产代码）
HOLIDAY_TABLE_01_6_1 = {
    "元旦": 1, "春节": 3, "情人节": 1, "清明": 1, "五一": 3,
    "端午": 1, "七夕": 1, "中秋": 1, "国庆": 3, "圣诞": 1,
}


def test_load_ok_full_sections() -> None:
    cfg = load_world_config(WORLD_PATH)
    # 既有四段（01 T-CFG-03 持有）原样在场（追加纪律：diff 为零）
    for key in ("locations", "company", "stocks", "economy"):
        assert key in cfg
    # T-WA-01 追加三段
    for key in ("schedule", "holidays", "triggers"):
        assert key in cfg
    assert cfg["holidays"]["rules"]["market_closed"] is True


def _write_tmp(tmp_path: Path, mutate) -> Path:
    with WORLD_PATH.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    mutate(raw)
    p = tmp_path / "world_bad.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return p


def test_reject_missing_key(tmp_path: Path) -> None:
    p = _write_tmp(tmp_path, lambda raw: raw.pop("holidays"))
    with pytest.raises(WorldConfigError):
        load_world_config(p)


def test_reject_inverted_range(tmp_path: Path) -> None:
    def mutate(raw: dict) -> None:
        raw["economy"]["utility"]["min_cents"], raw["economy"]["utility"]["max_cents"] = (
            raw["economy"]["utility"]["max_cents"], raw["economy"]["utility"]["min_cents"])
    with pytest.raises(WorldConfigError):
        load_world_config(_write_tmp(tmp_path, mutate))


def test_reject_dept_headcount_mismatch(tmp_path: Path) -> None:
    def mutate(raw: dict) -> None:
        raw["company"]["departments"][0]["tenants"] += 1  # total 不变 → 编制不自洽
    with pytest.raises(WorldConfigError):
        load_world_config(_write_tmp(tmp_path, mutate))


def test_reject_holiday_table_without_spring_festival(tmp_path: Path) -> None:
    def mutate(raw: dict) -> None:
        raw["holidays"]["table"] = [h for h in raw["holidays"]["table"] if h["name"] != "春节"]
    with pytest.raises(WorldConfigError):
        load_world_config(_write_tmp(tmp_path, mutate))


def test_reject_bad_probability(tmp_path: Path) -> None:
    def mutate(raw: dict) -> None:
        raw["triggers"]["disturb"]["weather_prob"] = 1.5
    with pytest.raises(WorldConfigError):
        load_world_config(_write_tmp(tmp_path, mutate))


def test_holiday_table_matches_01_6_1() -> None:
    """节假日表条目与 01 §6.1 节假日表一一对应（名称与天数逐条对拍）。"""
    cfg = load_world_config(WORLD_PATH)
    table = {h["name"]: int(h["days"]) for h in cfg["holidays"]["table"]}
    assert table == HOLIDAY_TABLE_01_6_1
    # 01 §6.1：情人节/七夕表白权重上调键存在
    by_name = {h["name"]: h for h in cfg["holidays"]["table"]}
    assert by_name["情人节"]["confess_weight"] > 1.0
    assert by_name["七夕"]["confess_weight"] > 1.0
