"""T-DB-04 seed_8 验收：确定性 / 余额分层 / 关系对拍 / 债务方向与期限 / world_state 键 / 月薪分层。

库 = worldsim_seed8（conftest `seed8_dsn`：schema_v1 + ddl/seed_8.sql）。
期望值一律从 config/agents.yaml + config/world.yaml 现读（数字引用纪律，00 §7 DoD 6）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
P8_IDS = [f"A{i:02d}" for i in range(1, 9)]
TZ8 = timezone(timedelta(hours=8))


def _agents_yaml() -> list[dict]:
    with (SERVER_ROOT / "config" / "agents.yaml").open(encoding="utf-8") as f:
        return [a for a in yaml.safe_load(f)["agents"] if a["agent_id"] in P8_IDS]


def _world_yaml() -> dict:
    with (SERVER_ROOT / "config" / "world.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _expected_band(hire_months: int, world: dict) -> dict:
    rule = world["economy"]["initial_balance"]["tenancy_rule"]
    bands = world["economy"]["initial_balance"]["tiers"]
    if hire_months > rule["senior_gt_months"]:
        return bands["senior"]
    if hire_months >= rule["regular_gte_months"]:
        return bands["regular"]
    return bands["new"]


def test_seed_determinism(tmp_path: Path) -> None:
    """再生成确定性：连跑两次字节相等，且与入库的 ddl/seed_8.sql 一致（T-DB-04 验收 1）。"""
    outs = []
    for i in (1, 2):
        out = tmp_path / f"seed_8_{i}.sql"
        subprocess.run(
            [sys.executable, str(SERVER_ROOT / "scripts" / "seed.py"), "--ids", "A01..A08", "--out", str(out)],
            check=True, capture_output=True, text=True, cwd=SERVER_ROOT,
        )
        outs.append(out.read_bytes())
    assert outs[0] == outs[1], "同输入两次生成不一致"
    assert outs[0] == (SERVER_ROOT / "ddl" / "seed_8.sql").read_bytes(), "入库文件与生成器漂移（重跑 seed.py）"


@pytest.mark.asyncio
async def test_seed_counts(seed8_dsn: str) -> None:
    """agents = 8 且全 star，id 集合恰 A01..A08（验收 3）。"""
    conn = await asyncpg.connect(seed8_dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM agents") == 8
        assert await conn.fetchval("SELECT count(*) FROM agents WHERE cognition_tier = 'star'") == 8
        ids = await conn.fetchval("SELECT string_agg(id, ',' ORDER BY id) FROM agents")
        assert ids == ",".join(P8_IDS)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_balance_bands(seed8_dsn: str) -> None:
    """余额整元（%100=0）且每人落在其入住分层区间内（验收 4，对拍 YAML 分层）。"""
    world = _world_yaml()
    conn = await asyncpg.connect(seed8_dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM agents WHERE balance_cents % 100 != 0") == 0
        rows = await conn.fetch("SELECT id, balance_cents FROM agents")
        balances = {r["id"]: r["balance_cents"] for r in rows}
    finally:
        await conn.close()
    for a in _agents_yaml():
        band = _expected_band(int(a["hire_months"]), world)
        assert band["min_cents"] <= balances[a["agent_id"]] <= band["max_cents"], (
            f"{a['name']} 余额 {balances[a['agent_id']]} 越出分层区间"
        )


@pytest.mark.asyncio
async def test_seed_relations_match_yaml(seed8_dsn: str) -> None:
    """relations 全量对拍 YAML relations_initial（a→b / affinity / tension / labels / one_line，验收 5）。"""
    expected: dict[tuple[str, str], tuple] = {}
    for a in _agents_yaml():
        for r in a.get("relations_initial", []):
            if r["target"] in P8_IDS:
                expected[(a["agent_id"], r["target"])] = (
                    r["affinity"], r["tension"], [r["type"]], r.get("note"),
                )
    conn = await asyncpg.connect(seed8_dsn)
    try:
        rows = await conn.fetch("SELECT a_id, b_id, affinity, tension, labels, one_line FROM relations")
    finally:
        await conn.close()
    actual = {(r["a_id"], r["b_id"]): (r["affinity"], r["tension"], list(r["labels"]), r["one_line"]) for r in rows}
    assert actual == expected


@pytest.mark.asyncio
async def test_seed_debts(seed8_dsn: str) -> None:
    """初始债务 ≤¥5,000；a_id=林晚 → b_id=韩彻 400000 行存在且 due_sim = 锚点 +9 模拟日（验收 6，round2 §A.18）。"""
    conn = await asyncpg.connect(seed8_dsn)
    try:
        assert await conn.fetchval("SELECT BOOL_AND(amount_cents <= 500000) FROM debts")
        row = await conn.fetchrow("SELECT amount_cents, due_sim, repaid_cents FROM debts WHERE a_id = 'A01' AND b_id = 'A06'")
        assert row["amount_cents"] == 400000 and row["repaid_cents"] == 0
        assert row["due_sim"] == datetime(2026, 10, 21, 0, 0, tzinfo=TZ8)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_goals(seed8_dsn: str) -> None:
    """goals 每 agent 1~3 行；明星层恰 3 条/人（01 §3.3，验收 7）。"""
    conn = await asyncpg.connect(seed8_dsn)
    try:
        rows = await conn.fetch("SELECT agent_id, count(*) AS n, BOOL_AND(sim_week = 1 AND status = 'active') AS ok FROM goals GROUP BY agent_id")
        assert len(rows) == 8
        for r in rows:
            assert 1 <= r["n"] <= 3 and r["n"] == 3 and r["ok"], r["agent_id"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_world_state_anchor_and_stocks(seed8_dsn: str) -> None:
    """world_state 锚点键存在且 anchor_sim 为周一；股价键 = world.yaml 3 标的初值（验收 7，§6 D4）。"""
    world = _world_yaml()
    conn = await asyncpg.connect(seed8_dsn)
    try:
        anchor = json.loads(await conn.fetchval("SELECT value::text FROM world_state WHERE key = 'clock.anchor'"))
        assert datetime.fromisoformat(anchor["anchor_sim"]).weekday() == 0
        assert set(anchor) == {"anchor_sim", "anchor_wall", "ratio"}  # 04 §3.2 值结构
        stocks = json.loads(await conn.fetchval("SELECT value::text FROM world_state WHERE key = 'economy.stocks'"))
    finally:
        await conn.close()
    assert stocks == {s["id"]: s["initial_price_cents"] for s in world["stocks"]["symbols"]}


@pytest.mark.asyncio
async def test_seed_salary_bands(seed8_dsn: str) -> None:
    """月薪键覆盖 8 人全员，每人值 ∈ 其职级档区间（单位分，验收 8；T-DB-04 评审增项）。"""
    world = _world_yaml()
    conn = await asyncpg.connect(seed8_dsn)
    try:
        salary = json.loads(await conn.fetchval("SELECT value::text FROM world_state WHERE key = 'economy.salary'"))
    finally:
        await conn.close()
    assert set(salary) == set(P8_IDS)
    for a in _agents_yaml():
        band = world["economy"]["salary"][a["position"]]
        v = salary[a["agent_id"]]
        assert band["min_cents"] <= v <= band["max_cents"] and v % 100 == 0, f"{a['name']} 月薪越档"
