"""T-ITER2-02 增量 DDL 授权一致性校验/重放验收（round2 #2①④）。

口径：scripts/check_grants.py 的 GRANT_REGISTRY 为唯一授权登记表；reset/seed 重放丢增量
GRANT 是 round1/round2 两次日界崩溃根因——校验脚本必须在 seed 阶段就报错/自动补授。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _drop_db, _psql

DB_NAME = "worldsim_check_grants_test"
DDL_DIR = Path(__file__).resolve().parents[1] / "ddl"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_grants.py"
PG_BIN = os.path.expanduser("~/pgsql/bin")

_spec = importlib.util.spec_from_file_location("check_grants", SCRIPT)
cg = importlib.util.module_from_spec(_spec)
sys.modules["check_grants"] = cg  # dataclass 内省要求模块已注册
_spec.loader.exec_module(cg)


def _psql_file(db: str, path: Path) -> None:
    subprocess.run(
        [f"{PG_BIN}/psql", "-h", "/tmp", "-d", db, "-v", "ON_ERROR_STOP=1", "-f", str(path)],
        check=True, capture_output=True, text=True,
    )


def _role_exists() -> bool:
    out = subprocess.run(
        [f"{PG_BIN}/psql", "-h", "/tmp", "-d", "postgres", "-tAc",
         "SELECT 1 FROM pg_roles WHERE rolname='worldsim'"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() == "1"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", DB_NAME, *args],
        capture_output=True, text=True,
    )


@pytest.fixture
def grant_db() -> str:
    _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
    _psql(f"CREATE DATABASE {DB_NAME}")
    if not _role_exists():
        _psql("CREATE ROLE worldsim LOGIN PASSWORD 'x'")
    _psql("CREATE SCHEMA IF NOT EXISTS partman", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS vector", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman", db=DB_NAME)
    for f in ("schema_v1.sql", "health_daily_v1.sql"):  # 故意不灌 memories_update_grant.sql
        _psql_file(DB_NAME, DDL_DIR / f)
    yield DB_NAME
    _drop_db(DB_NAME)


def test_registry_covers_known_incremental_grants() -> None:
    """登记表 = 两张历史事故表的增量授权（health_daily 表级 / memories 列级）。"""
    by_table = {(s.table, s.column) for s in cg.GRANT_REGISTRY}
    assert ("health_daily", None) in by_table
    assert ("memories", "archived") in by_table


def test_compute_diff_detects_missing() -> None:
    """登记项缺失 → diff 报告缺失；逐项在位 → diff 为 0。"""
    spec = cg.GrantSpec("ddl/x.sql", "health_daily", ("SELECT", "INSERT", "UPDATE"))
    missing, _ = cg.compute_diff((spec,), set())
    assert len(missing) == 3
    full = {cg.GrantSpec("", "health_daily", (p,)) for p in ("SELECT", "INSERT", "UPDATE")}
    missing, _ = cg.compute_diff((spec,), full)
    assert missing == []


def test_check_fails_then_apply_heals(grant_db: str) -> None:
    """reset 重放（未灌增量授权文件）→ --check 退出码 1 报缺失；--apply 补授后 diff=0。"""
    r = _run("--check")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "memories(archived)" in r.stdout
    r2 = _run("--apply")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "diff=0" in r2.stdout
    r3 = _run("--check")
    assert r3.returncode == 0, r3.stdout + r3.stderr
