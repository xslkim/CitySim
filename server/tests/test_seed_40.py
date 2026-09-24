"""T-DB-05 seed_40 验收：40 人全量 / 部门编制 / A01~A08 与 seed_8 一致 / 分层 / NPC 节点 / 月薪。

库 = worldsim_seed40 与 worldsim_seed8（conftest 两个 fixture）。
期望值从 config/agents.yaml + config/world.yaml 现读（数字引用纪律）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
P8_IDS = [f"A{i:02d}" for i in range(1, 9)]
NPC_IDS = [a for a in (f"A{i:02d}" for i in range(1, 41))]

AGENT_COLS = "id, name, gender, age, room_no, department, job_title, cognition_tier, persona::text, needs::text, balance_cents, position"


def _agents_yaml() -> list[dict]:
    with (SERVER_ROOT / "config" / "agents.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)["agents"]


def _world_yaml() -> dict:
    with (SERVER_ROOT / "config" / "world.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_seed40_determinism(tmp_path: Path) -> None:
    """再生成确定性：连跑两次字节相等，且与入库的 ddl/seed_40.sql 一致。"""
    outs = []
    for i in (1, 2):
        out = tmp_path / f"seed_40_{i}.sql"
        subprocess.run(
            [sys.executable, str(SERVER_ROOT / "scripts" / "seed.py"), "--ids", "A01..A40", "--out", str(out)],
            check=True, capture_output=True, text=True, cwd=SERVER_ROOT,
        )
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]
    assert outs[0] == (SERVER_ROOT / "ddl" / "seed_40.sql").read_bytes(), "入库文件与生成器漂移（重跑 seed.py）"


@pytest.mark.asyncio
async def test_seed40_counts(seed40_dsn: str) -> None:
    """agents = 40：24 有房间 + 16 room_no IS NULL（验收 1；01 §1.3 编制）。"""
    conn = await asyncpg.connect(seed40_dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM agents") == 40
        assert await conn.fetchval("SELECT count(*) FROM agents WHERE room_no IS NOT NULL") == 24
        assert await conn.fetchval("SELECT count(*) FROM agents WHERE room_no IS NULL") == 16
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed40_dept_headcount(seed40_dsn: str) -> None:
    """六部门人数与 01 §1.3 编制表逐行相等（验收 2；对拍 world.yaml，部门名取括号前缀）。"""
    world = _world_yaml()
    expected = {d["name"].split("（")[0]: d["total"] for d in world["company"]["departments"]}
    conn = await asyncpg.connect(seed40_dsn)
    try:
        rows = await conn.fetch("SELECT department, count(*) AS n FROM agents GROUP BY department")
    finally:
        await conn.close()
    assert {r["department"]: r["n"] for r in rows} == expected


@pytest.mark.asyncio
async def test_seed40_positions(seed40_dsn: str) -> None:
    """租客 position = 房间节点 apt.L<楼层>.<房号>；16 NPC = home.<agent_id>（01 §1.6）。"""
    conn = await asyncpg.connect(seed40_dsn)
    try:
        rows = await conn.fetch("SELECT id, room_no, position FROM agents")
    finally:
        await conn.close()
    for r in rows:
        if r["room_no"] is None:
            assert r["position"] == f"home.{r['id']}", r["id"]
        else:
            assert r["position"] == f"apt.L{int(r['room_no'][:-2])}.{r['room_no']}", r["id"]


@pytest.mark.asyncio
async def test_seed40_tiers(seed40_dsn: str) -> None:
    """分层：star=8（A01~A08）/ secondary=12 / background=20（06 §3 ~12/~20 落定为 12/20，01 文档 §6 D16）。"""
    conn = await asyncpg.connect(seed40_dsn)
    try:
        rows = await conn.fetch("SELECT cognition_tier, count(*) AS n FROM agents GROUP BY cognition_tier")
        tiers = {r["cognition_tier"]: r["n"] for r in rows}
        assert tiers == {"star": 8, "secondary": 12, "background": 20}
        stars = await conn.fetchval("SELECT string_agg(id, ',' ORDER BY id) FROM agents WHERE cognition_tier = 'star'")
        assert stars == ",".join(P8_IDS)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed40_debts(seed40_dsn: str) -> None:
    """初始债务 2~4 条且 ≤¥5,000（01 §2.1）；方向 a 债权→b 债务（round2 §A.18）。"""
    conn = await asyncpg.connect(seed40_dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM debts") in (2, 3, 4)
        assert await conn.fetchval("SELECT BOOL_AND(amount_cents <= 500000) FROM debts")
        rows = await conn.fetch("SELECT a_id, b_id, amount_cents FROM debts ORDER BY a_id")
        assert [(r["a_id"], r["b_id"], r["amount_cents"]) for r in rows] == [
            ("A01", "A06", 400000), ("A12", "A24", 200000), ("A16", "A38", 350000),
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed40_salary_covers_all(seed40_dsn: str) -> None:
    """economy.salary 覆盖 40 人全员，每人值 ∈ 职级档区间（月薪抽定键，T-DB-04 口径扩至 40 人）。"""
    world = _world_yaml()
    conn = await asyncpg.connect(seed40_dsn)
    try:
        salary = json.loads(await conn.fetchval("SELECT value::text FROM world_state WHERE key = 'economy.salary'"))
    finally:
        await conn.close()
    assert set(salary) == set(NPC_IDS)
    for a in _agents_yaml():
        band = world["economy"]["salary"][a["position"]]
        v = salary[a["agent_id"]]
        assert band["min_cents"] <= v <= band["max_cents"] and v % 100 == 0, a["agent_id"]


@pytest.mark.asyncio
async def test_seed40_superset_of_seed8(seed8_dsn: str, seed40_dsn: str) -> None:
    """A01~A08 行与 seed_8 同名字段一致（验收 3）：agents 全字段对拍。"""
    c8 = await asyncpg.connect(seed8_dsn)
    c40 = await asyncpg.connect(seed40_dsn)
    try:
        r8 = await c8.fetch(f"SELECT {AGENT_COLS} FROM agents WHERE id = ANY($1) ORDER BY id", P8_IDS)
        r40 = await c40.fetch(f"SELECT {AGENT_COLS} FROM agents WHERE id = ANY($1) ORDER BY id", P8_IDS)
    finally:
        await c8.close()
        await c40.close()
    assert [dict(r) for r in r8] == [dict(r) for r in r40]
