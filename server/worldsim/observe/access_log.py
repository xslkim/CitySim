"""obs-api 访问日志（05 T-WEB-02；03 §8.1 门禁④度量管道）。

- 每次鉴权通过的 REST 请求与 WS `hello` 记一行（token/时间/端点，**不含任何事件内容**，03 §8.1）；
- 存储 = obs-api 自管 SQLite（与 token 表同库，03 §8.1"同机 SQLite 文件"；不属于 05 七表契约）；
- `GET /api/usage?from=&to=` 按 token×自然日聚合返回
  `[{token, day, opens, first_seen_at, last_seen_at}]`（03 §5.1；挂管理鉴权——M4 开发期口径：
  任一有效 token 可调，用户体系/管理分级属商业化预留 03 §8.3 末节，05 文档偏差表登记）。
"""

from __future__ import annotations

import datetime as dt

from .auth import connect


def record_access(token: str, endpoint: str, db_path: str | None = None) -> None:
    """写一行访问日志（只含 token/时间/端点，不含事件内容）。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO access_log (token, ts, endpoint) VALUES (?, ?, ?)",
            (token, dt.datetime.now(dt.timezone.utc).isoformat(), endpoint),
        )
        conn.commit()
    finally:
        conn.close()


def usage_by_day(from_day: str | None, to_day: str | None, db_path: str | None = None) -> list[dict]:
    """token×自然日聚合（03 §5.1 /api/usage 形态）；from/to 为 ISO 日期（含端点）。"""
    sql = """
        SELECT token, substr(ts, 1, 10) AS day, count(*) AS opens,
               min(ts) AS first_seen_at, max(ts) AS last_seen_at
        FROM access_log
    """
    conds: list[str] = []
    args: list[str] = []
    if from_day:
        conds.append("substr(ts, 1, 10) >= ?")
        args.append(from_day)
    if to_day:
        conds.append("substr(ts, 1, 10) <= ?")
        args.append(to_day)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " GROUP BY token, day ORDER BY day, token"
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
