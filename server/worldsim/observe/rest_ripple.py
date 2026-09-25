"""REST：涟漪接口组（05 T-WEB-06；03 §3.4 五段聚合 + 今日热涟漪；05 §3.7 distortion 只读）。

- `/api/ripple/:eventId`：URL `e<seq>` 去前缀（03 §3.4）；单接口聚合五段——
  ① `obs.memory_projection WHERE source_event_seq=:e0`（直接投影，含 is_witness 目击标注）；
  ② `obs.ripple_edge WHERE root_event_seq=:e0 ORDER BY hop, sim_time`（传播链）；
  ③ distortion 只读 ripple_edge（05 §3.7 唯一持有方，不回读 payload）；
  ④ `(payload->>'caused_by')::bigint IN (:e0_plus_chain)`（裸 seq 数字字符串，06 §2）；
  ⑤ `obs.relation_change_log WHERE event_seq IN (:chain)`。
  响应 `{projections[], chain[], followups[], relation_changes[], stats{}}`（03 §5.1）。
- `/api/ripple/today`：当前模拟日最新生效 grade='A'（obs.event_grade_view）事件按涟漪统计
  （投影人数 × 传播手数 × 关系边变更数）Top 5（03 §3.4 推荐位）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Path

from .app import ApiError, get_pool, ok_envelope, require_token
from .serde import serialize_event

router = APIRouter(prefix="/api/ripple", dependencies=[Depends(require_token)])

LOCAL_TZ_NAME = "Asia/Shanghai"  # 模拟日聚合时区（01 文档 §6 D4）


def parse_event_id(event_id: str) -> int:
    """`e<seq>` 去前缀（03 §3.4）；非数字 422。"""
    s = event_id[1:] if event_id.startswith("e") else event_id
    if not s.isdigit():
        raise ApiError("bad_param", f"事件 id 须为 seq 或 e<seq> 形态：{event_id!r}（06 §2）", 422)
    return int(s)


async def ripple_five_sections(pool: Any, e0: int) -> dict[str, Any]:
    projections = await pool.fetch(
        """
        SELECT agent_id, memory_id, importance, is_witness, content_display, sim_time
        FROM obs.memory_projection WHERE source_event_seq = $1 ORDER BY memory_id
        """,
        e0,
    )
    chain = await pool.fetch(
        """
        SELECT dst_event_seq, src_event_seq, teller_id, listener_id, hop, distortion, sim_time
        FROM obs.ripple_edge WHERE root_event_seq = $1 ORDER BY hop, sim_time
        """,
        e0,
    )
    chain_seqs = [int(c["dst_event_seq"]) for c in chain]
    followups = await pool.fetch(
        """
        SELECT * FROM obs.events
        WHERE (payload->>'caused_by') ~ '^\\d+$'
          AND (payload->>'caused_by')::bigint = ANY($1::bigint[])
        ORDER BY sim_time
        """,
        [e0, *chain_seqs],
    )
    all_seqs = [e0, *chain_seqs, *[int(f["seq"]) for f in followups]]
    # ⑤ 关系边变化：event_seq ∈ 链 ∪ 由链上事件引发（relation.changed 为 internal 事件，
    # caused_by 被白名单剥除；因果链改走 changes[].cause（裸 seq 数字字符串，06 §2）——05 文档偏差表登记）
    relation_changes = await pool.fetch(
        """
        SELECT rcl.a_id, rcl.b_id, rcl.delta_affinity, rcl.delta_tension,
               rcl.labels_added::text AS labels_added, rcl.labels_removed::text AS labels_removed,
               rcl.event_seq, rcl.sim_time
        FROM obs.relation_change_log rcl
        WHERE rcl.event_seq = ANY($1::bigint[])
           OR rcl.event_seq IN (
             SELECT e.seq FROM obs.events e
             WHERE e.type = 'relation.changed'
               AND EXISTS (
                 SELECT 1 FROM jsonb_to_recordset(e.payload->'changes') AS c(cause TEXT)
                 WHERE c.cause ~ '^\\d+$' AND c.cause::bigint = ANY($1::bigint[])
               )
           )
        ORDER BY rcl.event_seq
        """,
        all_seqs,
    )
    covered = {p["agent_id"] for p in projections} | {c["listener_id"] for c in chain}
    max_hop = max((int(c["hop"]) for c in chain), default=0)
    max_dist = max((float(c["distortion"]) for c in chain if c["distortion"] is not None), default=0.0)
    import json as _json
    src_row = await pool.fetchrow("SELECT * FROM obs.events WHERE seq = $1", e0)
    return {
        "source_seq": e0,
        "source": serialize_event(src_row),  # 源事件卡数据源（03 §3.4 渲染结构首行）
        "projections": [{
            "agent_id": p["agent_id"], "memory_id": int(p["memory_id"]),
            "importance": p["importance"], "is_witness": p["is_witness"],
            "content_display": p["content_display"], "sim_time": p["sim_time"].isoformat(),
        } for p in projections],
        "chain": [{
            "dst_event_seq": int(c["dst_event_seq"]), "src_event_seq": int(c["src_event_seq"]),
            "teller_id": c["teller_id"], "listener_id": c["listener_id"], "hop": int(c["hop"]),
            "distortion": float(c["distortion"]) if c["distortion"] is not None else None,
            "sim_time": c["sim_time"].isoformat(),
        } for c in chain],
        "followups": [serialize_event(f) for f in followups],
        "relation_changes": [{
            "a_id": r["a_id"], "b_id": r["b_id"],
            "delta_affinity": r["delta_affinity"], "delta_tension": r["delta_tension"],
            "labels_added": _json.loads(r["labels_added"]) if r["labels_added"] else None,
            "labels_removed": _json.loads(r["labels_removed"]) if r["labels_removed"] else None,
            "event_seq": int(r["event_seq"]), "sim_time": r["sim_time"].isoformat(),
        } for r in relation_changes],
        "stats": {
            "covered_agents": len(covered),
            "hops": max_hop,
            "max_distortion": max_dist,
            "followup_count": len(followups),
        },
    }


@router.get("/today")
async def api_ripple_today(pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """今日热涟漪 Top 5（当前模拟日最新生效 grade='A'，按 投影人数×传播手数×关系边变更数 排序）。"""
    cur_day = await pool.fetchval(
        f"SELECT (max(sim_time) AT TIME ZONE '{LOCAL_TZ_NAME}')::date FROM obs.events"
    )
    if cur_day is None:
        return await ok_envelope(pool, {"sim_day": None, "items": []}, kind="ripple_today")
    a_events = await pool.fetch(
        f"""
        SELECT e.seq FROM obs.events e JOIN obs.event_grade_view g ON g.seq = e.seq
        WHERE g.grade = 'A' AND (e.sim_time AT TIME ZONE '{LOCAL_TZ_NAME}')::date = $1
        ORDER BY e.seq
        """,
        cur_day,
    )
    items = []
    for r in a_events:
        seq = int(r["seq"])
        sections = await ripple_five_sections(pool, seq)
        stats = sections["stats"]
        score = stats["covered_agents"] * stats["hops"] * len(sections["relation_changes"])
        row = await pool.fetchrow("SELECT * FROM obs.events WHERE seq = $1", seq)
        items.append({"score": score, "stats": stats, "event": serialize_event(row)})
    items.sort(key=lambda x: (-x["score"], -x["event"]["seq"]))
    return await ok_envelope(pool, {
        "sim_day": cur_day.isoformat(),
        "items": [{**it, "event": it["event"]} for it in items[:5]],
    }, kind="ripple_today")


@router.get("/{event_id}")
async def api_ripple(
    event_id: str = Path(),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    e0 = parse_event_id(event_id)
    exists = await pool.fetchval("SELECT 1 FROM obs.events WHERE seq = $1", e0)
    if not exists:
        raise ApiError("event_not_found", f"无事件 e{e0}", 404)
    return await ok_envelope(pool, await ripple_five_sections(pool, e0), kind="ripple")
