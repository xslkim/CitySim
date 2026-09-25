"""REST：关系接口组（05 T-WEB-06；03 §3.5/§5.1、05 §3.3/§3.4/§6）。

- `/api/relations/snapshots?from=&to=&step=day`：`obs.relation_daily` 稀疏编码
  `[{sim_day, edges:[{a,b,aff,ten,label}]}]`（非零边 = affinity/tension 非零或有标签）。
- `/api/relations/pair?a=&b=`：双人双向时序（relation_daily 补齐）+ relation_change_log 按对聚合
  + 关键事件标记（05 §6 行；事件序列化 = serde 唯一实现）。
- `/api/relations?agent=`：单角色有序出边列表（/agent/:id 关系区块数据源，03 §3.3），
  按 |affinity| 降序，附边色所需的 tension（02 §7.5 映射在前端）。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from .app import ApiError, get_pool, ok_envelope, require_token
from .rest_agents import AGENT_ID_PATTERN
from .serde import serialize_event

router = APIRouter(prefix="/api/relations", dependencies=[Depends(require_token)])


def _valid_agent(v: str, label: str) -> str:
    import re
    if not re.fullmatch(AGENT_ID_PATTERN, v):
        raise ApiError("bad_param", f"非法 {label}：{v!r}（04 §5.2 形态 A01~A40）", 422)
    return v


def _edge(r: Any) -> dict[str, Any]:
    return {"a": r["a_id"], "b": r["b_id"], "aff": r["affinity"], "ten": r["tension"],
            "label": list(r["labels"] or [])}


def _is_nonzero(r: Any) -> bool:
    return r["affinity"] != 0 or r["tension"] != 0 or bool(r["labels"])


@router.get("")
async def api_relations_agent(
    agent: str = Query(...),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    _valid_agent(agent, "agent")
    rows = await pool.fetch(
        """
        SELECT a_id, b_id, affinity, tension, labels FROM obs.relation_daily
        WHERE a_id = $1 AND sim_day = (SELECT max(sim_day) FROM obs.relation_daily)
        ORDER BY abs(affinity) DESC, abs(tension) DESC
        """,
        agent,
    )
    return await ok_envelope(pool, {"items": [_edge(r) for r in rows]})


@router.get("/snapshots")
async def api_relations_snapshots(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    step: str = Query(default="day"),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    if step != "day":
        raise ApiError("bad_param", "step 仅支持 day（05 §3.4 物化粒度）", 422)
    conds: list[str] = []
    args: list[Any] = []
    for label, v in (("from", from_), ("to", to)):
        if v:
            try:
                day = dt.date.fromisoformat(v)
            except ValueError as e:
                raise ApiError("bad_param", f"{label} 须为 ISO 日期：{v!r}", 422) from e
            conds.append(f"sim_day {'>=' if label == 'from' else '<='} ${len(args) + 1}")
            args.append(day)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = await pool.fetch(
        f"SELECT sim_day, a_id, b_id, affinity, tension, labels FROM obs.relation_daily {where}"
        " ORDER BY sim_day, a_id, b_id",
        *args,
    )
    days: dict[str, list[dict]] = {}
    for r in rows:
        if _is_nonzero(r):
            days.setdefault(r["sim_day"].isoformat(), []).append(_edge(r))
    return await ok_envelope(pool, {
        "items": [{"sim_day": d, "edges": edges} for d, edges in sorted(days.items())],
    })


@router.get("/pair")
async def api_relations_pair(
    a: str = Query(...),
    b: str = Query(...),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    _valid_agent(a, "a")
    _valid_agent(b, "b")
    if a == b:
        raise ApiError("bad_param", "a 与 b 须为不同角色", 422)
    daily = await pool.fetch(
        """
        SELECT sim_day, a_id, b_id, affinity, tension FROM obs.relation_daily
        WHERE (a_id, b_id) IN (($1, $2), ($2, $1)) ORDER BY sim_day
        """,
        a, b,
    )
    series = {"forward": [], "backward": []}  # forward = a→b；backward = b→a
    for r in daily:
        key = "forward" if r["a_id"] == a else "backward"
        series[key].append({"sim_day": r["sim_day"].isoformat(),
                            "affinity": r["affinity"], "tension": r["tension"]})
    changes = await pool.fetch(
        """
        SELECT event_seq, a_id, b_id, delta_affinity, delta_tension,
               labels_added::text AS labels_added, labels_removed::text AS labels_removed, sim_time
        FROM obs.relation_change_log
        WHERE (a_id, b_id) IN (($1, $2), ($2, $1)) ORDER BY sim_time
        """,
        a, b,
    )
    event_seqs = sorted({int(c["event_seq"]) for c in changes})
    key_events = []
    if event_seqs:
        rows = await pool.fetch(
            "SELECT * FROM obs.events WHERE seq = ANY($1::bigint[]) ORDER BY seq", event_seqs,
        )
        key_events = [serialize_event(r) for r in rows]
    return await ok_envelope(pool, {
        "a": a, "b": b,
        "series": series,
        "changes": [{
            "event_seq": int(c["event_seq"]), "a": c["a_id"], "b": c["b_id"],
            "delta_affinity": c["delta_affinity"], "delta_tension": c["delta_tension"],
            "labels_added": json.loads(c["labels_added"]) if c["labels_added"] else None,
            "labels_removed": json.loads(c["labels_removed"]) if c["labels_removed"] else None,
            "sim_time": c["sim_time"].isoformat(),
        } for c in changes],
        "key_events": key_events,
    })
