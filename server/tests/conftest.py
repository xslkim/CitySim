"""pytest 公共 fixture（T-ENV-04；T-DB-02 起灌 schema_v1.sql）。

测试库 fixture：以 superuser（本机 OS 用户，initdb 默认）经 unix socket（/tmp，trust）
建/毁 worldsim_test 等测试库，供全部 DB 测试复用。
- `test_db_dsn`：建库即灌 ddl/schema_v1.sql（T-DB-02 契约测试口径）；表 owner = superuser，
  GRANT/REVOKE 对 worldsim 真实生效。
- `seed8_dsn` / `seed40_dsn`（T-DB-04/05）：独立库灌 schema_v1 + 对应 seed，避免与契约测试
  插入行互相污染。
调用约定 00 §1 A14：一切执行走 `cd server && uv run pytest`。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

TEST_DB_NAME = "worldsim_test"
SEED8_DB_NAME = "worldsim_seed8"
SEED40_DB_NAME = "worldsim_seed40"
PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"
DDL_DIR = Path(__file__).resolve().parents[1] / "ddl"


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True,
        capture_output=True,
        text=True,
    )


def _psql_file(path: Path, db: str) -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-f", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _build_db(name: str, *ddl_files: Path) -> str:
    """建测试库（含 vector/pg_partman 扩展 + partman 授权），依次灌 DDL 文件，返回 socket DSN。"""
    _psql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    _psql(f"CREATE DATABASE {name}")
    _psql("CREATE SCHEMA IF NOT EXISTS partman", db=name)
    _psql("CREATE EXTENSION IF NOT EXISTS vector", db=name)
    _psql("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman", db=name)
    for f in ddl_files:
        _psql_file(f, name)
    return f"postgresql:///{name}?host={SOCKET_DIR}"


def _drop_db(name: str) -> None:
    _psql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="session")
def test_db_dsn() -> Iterator[str]:
    """worldsim_test：扩展 + schema_v1.sql（T-DB-02/T-DB-03 共用；不放 seed，各行集由用例自控）。"""
    dsn = _build_db(TEST_DB_NAME, DDL_DIR / "schema_v1.sql")
    try:
        yield dsn
    finally:
        _drop_db(TEST_DB_NAME)


@pytest.fixture(scope="session")
def seed8_dsn() -> Iterator[str]:
    """worldsim_seed8：schema_v1 + seed_8.sql（T-DB-04 验收库）。"""
    dsn = _build_db(SEED8_DB_NAME, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(SEED8_DB_NAME)


@pytest.fixture(scope="session")
def seed40_dsn() -> Iterator[str]:
    """worldsim_seed40：schema_v1 + seed_40.sql（T-DB-05 验收库）。"""
    dsn = _build_db(SEED40_DB_NAME, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_40.sql")
    try:
        yield dsn
    finally:
        _drop_db(SEED40_DB_NAME)
