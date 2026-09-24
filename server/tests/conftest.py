"""pytest 公共 fixture（T-ENV-04）。

测试库 fixture：以 superuser（本机 OS 用户，initdb 默认）经 unix socket（/tmp，trust）
建/毁 worldsim_test，供全部 DB 测试复用（T-DB-02 契约测试等灌 schema_v1.sql）。
调用约定 00 §1 A14：一切执行走 `cd server && uv run pytest`。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator

import pytest

TEST_DB_NAME = "worldsim_test"
PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def test_db_dsn() -> Iterator[str]:
    """建 worldsim_test（含 vector/pg_partman 扩展），yield asyncpg 可用的 socket DSN，session 末销毁。"""
    _psql(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
    _psql(f"CREATE DATABASE {TEST_DB_NAME}")
    _psql("CREATE SCHEMA IF NOT EXISTS partman", db=TEST_DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS vector", db=TEST_DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman", db=TEST_DB_NAME)
    try:
        yield f"postgresql:///{TEST_DB_NAME}?host={SOCKET_DIR}"
    finally:
        _psql(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
