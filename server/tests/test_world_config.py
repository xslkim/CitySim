"""T-CFG-03 world.yaml 验收：编制表/房间性别规则/股价参数/金额 _cents 纪律。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORLD_PATH = Path(__file__).resolve().parents[1] / "config" / "world.yaml"

# economy 段内允许的非金额叶子键（其余一律 _cents 后缀，06 §1.3 口径延伸）
NON_MONEY_KEYS = {
    # 日期/计数/比率/开关
    "payday", "bill_day", "dept_adjust_pct", "random_walk_pct", "floors",
    "compound_interest", "per_person", "consumption_downgrade", "two_periods_unpaid",
    # 需求/情绪联动量（非金额）
    "hunger", "mood", "energy", "achievement", "social",
    "mood_delta", "wealth_delta", "frustration_delta",
    # 财富需求映射公式参数
    "divisor", "clamp_min", "clamp_max", "sign", "salary_credit", "bill_range", "cap",
    # 目标权重倍率 / 绩效档 / 入住月数规则
    "G-MON-04", "S", "A", "B", "C",
    "senior_gt_months", "regular_gte_months", "new_lt_months",
}


def _load() -> dict:
    with WORLD_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _leaf_keys(node: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                keys.extend(_leaf_keys(v))
            else:
                keys.append(str(k))
    elif isinstance(node, list):
        for item in node:
            keys.extend(_leaf_keys(item))
    return keys


def test_frozen_four_sections() -> None:
    assert set(_load().keys()) == {"locations", "company", "stocks", "economy"}


def test_dept_headcount_sums_40() -> None:
    depts = _load()["company"]["departments"]
    assert len(depts) == 6
    assert sum(d["total"] for d in depts) == 40
    assert sum(d["managers"] for d in depts) == 6
    assert sum(d["npc_staff"] for d in depts) == 10
    assert sum(d["tenants"] for d in depts) == 24
    for d in depts:
        assert d["managers"] + d["npc_staff"] + d["tenants"] == d["total"], d["name"]
    ranks = _load()["company"]["ranks"]
    assert ranks == {"M": 6, "P2": 12, "P1": 22}


def test_room_gender_rule() -> None:
    rooms = _load()["locations"]["apartment"]["rooms"]
    generated = [f"{floor}{suffix:02d}" for floor in rooms["floors"] for suffix in rooms["rooms_per_floor"]]
    assert len(generated) == 24, "楼层×间数应推出 24 间"
    rule = rooms["gender_rule"]
    male = set(rule["male_suffixes"])
    female = set(rule["female_suffixes"])
    assert male | female == set(rooms["rooms_per_floor"]), "性别后缀须覆盖全部 4 间"
    assert not male & female, "性别后缀不得重叠"
    for no in generated:
        suffix = int(no[-2:])
        assert suffix in male or suffix in female


def test_stocks_match_01_1_5() -> None:
    stocks = _load()["stocks"]
    by_name = {s["name"]: s["initial_price_cents"] for s in stocks["symbols"]}
    assert by_name == {"星澜科技": 1800, "临江银行": 950, "云岭新能源": 4200}
    assert stocks["random_walk"] == {"mu": 0.0004, "sigma": 0.018}
    assert stocks["fee"] == {"rate": 0.0005, "min_cents": 500}
    assert stocks["trading_hours"] == ["09:30", "15:00"]


def test_all_money_in_cents() -> None:
    bad = [k for k in _leaf_keys(_load()["economy"]) if not k.endswith("_cents") and k not in NON_MONEY_KEYS]
    assert not bad, f"economy 段出现非 _cents 金额键或非登记键: {bad}"


def test_node_id_naming_rules() -> None:
    loc = _load()["locations"]
    assert loc["apartment"]["rooms"]["id_pattern"] == "apt.L{floor}.{room_no}"
    commons = {c["id"] for c in loc["apartment"]["commons"]}
    assert commons == {"apt.lobby", "apt.kitchen", "apt.gym", "apt.laundry", "apt.roof"}
    office_ids = {n["id"] for n in loc["office"]}
    assert {"corp.product", "corp.tech", "corp.market", "corp.ops", "corp.design", "corp.admin"} <= office_ids
    ceo = next(n for n in loc["office"] if n["id"] == "corp.ceo")
    assert ceo["enterable"] is False, "总经理室不可进入仅事件源（01 §1.3）"
    assert loc["offsite"]["id_pattern"] == "home.{agent_id}" and loc["offsite"]["count"] == 16
