"""obs-api per-user token 鉴权（05 T-WEB-02；03 §8.3）。

token 表固定 10 行容量（种子用户上限），字段 `token/user_label/issued_at/disabled`，
存 obs-api 自管 SQLite（`WSIM_OBS_TOKEN_DB`；不入仓、不进副本库、与访问日志同库，03 §8.1）。
携带方式：`?token=` 或 `Authorization: Bearer`（03 §8.3 两种携带）；失败 401。
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import sqlite3
from pathlib import Path

TOKEN_CAPACITY = 10  # token 表固定 10 行容量（03 §8.3 种子用户上限）
DEFAULT_TOKEN_DB = str(Path(__file__).resolve().parents[3] / "var" / "observe" / "tokens.db")  # gitignored var/

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  token TEXT PRIMARY KEY,
  user_label TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS access_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT NOT NULL,
  ts TEXT NOT NULL,
  endpoint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS access_log_token_ts ON access_log (token, ts);
"""


def token_db_path() -> str:
    return os.environ.get("WSIM_OBS_TOKEN_DB", DEFAULT_TOKEN_DB)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or token_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


class TokenStore:
    """token 表操作（issue/revoke/list/verify）；超容量拒发。"""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or token_db_path()

    def issue(self, user_label: str) -> str:
        conn = connect(self._db_path)
        try:
            n = conn.execute("SELECT count(*) FROM tokens WHERE disabled = 0").fetchone()[0]
            if n >= TOKEN_CAPACITY:
                raise CapacityError(f"token 表容量 {TOKEN_CAPACITY} 已满（03 §8.3 种子用户上限）")
            token = "dev_" + secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO tokens (token, user_label, issued_at) VALUES (?, ?, ?)",
                (token, user_label, dt.datetime.now(dt.timezone.utc).isoformat()),
            )
            conn.commit()
            return token
        finally:
            conn.close()

    def revoke(self, token: str) -> bool:
        conn = connect(self._db_path)
        try:
            cur = conn.execute("UPDATE tokens SET disabled = 1 WHERE token = ?", (token,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list(self) -> list[dict]:
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT token, user_label, issued_at, disabled FROM tokens ORDER BY issued_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def verify(self, token: str) -> dict | None:
        """有效（存在且未禁用）→ 返回行；否则 None。"""
        if not token:
            return None
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT token, user_label, issued_at, disabled FROM tokens WHERE token = ?", (token,)
            ).fetchone()
            if row is None or row["disabled"]:
                return None
            return dict(row)
        finally:
            conn.close()


class CapacityError(RuntimeError):
    pass


def extract_token(query_token: str | None, authorization: str | None) -> str | None:
    """两种携带方式（03 §8.3）：`?token=` 优先，其次 `Authorization: Bearer`。"""
    if query_token:
        return query_token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None
