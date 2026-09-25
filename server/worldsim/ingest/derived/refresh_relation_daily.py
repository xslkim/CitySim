"""refresh_relation_daily（07 T-SYN-08；05 §3.4/§4.2）：每日关系矩阵定稿。

递推口径（05 §3.4）：`relation_daily(d) = relation_daily(d−1) + Σ relation_change_log(sim_day=d)`
（同边多次 delta 求和）；首日 = 基线（首日 `world_state_snapshot` 的 relations 矩阵，含人设初始
关系 01 §2.2）；**labels 不递推**——取当日快照关系矩阵该边 labels（快照缺席的边 labels='{}'）。
幂等：`DELETE WHERE sim_day=d + INSERT` 单事务（05 §3.4/§4.3）。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any


def _snapshot_relations(state: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """world_state_snapshot.state.relations 矩阵 → {(a,b): {affinity,tension,labels}}。"""
    out = {}
    for r in state.get("relations") or []:
        out[(str(r["a"]), str(r["b"]))] = {
            "affinity": int(r["affinity"]), "tension": int(r["tension"]),
            "labels": list(r.get("labels") or []),
        }
    return out


async def _snapshot_matrix(conn: Any, day: dt.date) -> dict[tuple[str, str], dict[str, Any]]:
    """当日（或最近 ≤ 当日）快照的关系矩阵；无快照 → 空。"""
    row = await conn.fetchrow(
        "SELECT state FROM world_state_snapshot WHERE sim_day<=$1 ORDER BY sim_day DESC LIMIT 1", day)
    if row is None:
        return {}
    state = row["state"]
    if isinstance(state, str):
        state = json.loads(state)
    return _snapshot_relations(state)


async def compute_day(conn: Any, day: dt.date) -> dict[tuple[str, str], dict[str, Any]]:
    """计算 sim_day=day 的矩阵（纯读取 + 合并；不落库）。"""
    prev = await conn.fetch(
        "SELECT a_id, b_id, affinity, tension, labels FROM relation_daily WHERE sim_day=$1",
        day - dt.timedelta(days=1))
    has_earlier = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM relation_daily WHERE sim_day < $1)", day)
    if prev:
        matrix = {(r["a_id"], r["b_id"]): {"affinity": int(r["affinity"]),
                                           "tension": int(r["tension"]),
                                           "labels": list(r["labels"])}
                  for r in prev}
    elif not has_earlier:
        matrix = await _snapshot_matrix(conn, day)  # 首日 = 基线矩阵（05 §3.4）
    else:
        matrix = {}
    deltas = await conn.fetch(
        """
        SELECT a_id, b_id, sum(delta_affinity)::int AS da, sum(delta_tension)::int AS dt
        FROM relation_change_log WHERE sim_day=$1 GROUP BY a_id, b_id
        """, day)
    for r in deltas:
        key = (r["a_id"], r["b_id"])
        cell = matrix.setdefault(key, {"affinity": 0, "tension": 0, "labels": []})
        cell["affinity"] += int(r["da"])
        cell["tension"] += int(r["dt"])
    # labels 不递推：取当日快照矩阵该边 labels（快照为该日权威源，05 §3.4）
    snap = await _snapshot_matrix(conn, day)
    for key, cell in matrix.items():
        if key in snap:
            cell["labels"] = list(snap[key]["labels"])
        elif not prev and has_earlier is False:
            pass  # 首日基线已带 labels
        else:
            cell["labels"] = cell.get("labels") or []
    return matrix


async def refresh(conn: Any, day: dt.date) -> int:
    """定稿 sim_day=day：DELETE+INSERT 单事务（幂等）。返回落行数。"""
    if isinstance(day, str):
        day = dt.date.fromisoformat(day)
    matrix = await compute_day(conn, day)
    await conn.execute("DELETE FROM relation_daily WHERE sim_day=$1", day)
    for (a, b), cell in sorted(matrix.items()):
        await conn.execute(
            """
            INSERT INTO relation_daily (sim_day, a_id, b_id, affinity, tension, labels)
            VALUES ($1,$2,$3,$4,$5,$6)
            """, day, a, b, cell["affinity"], cell["tension"], cell["labels"])
    return len(matrix)
