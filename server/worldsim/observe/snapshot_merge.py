"""`/api/snapshot?tick=` 历史合并（05 T-WEB-04；03 §4.2 W3 简化回放唯一状态数据源）。

合并 = ≤tick 最近 `obs.world_state_snapshot` + 其间 `obs.events` 增量归约：
- 归约只覆盖 UI 字段（03 §4.2 边界 3）：`agent.move` → location_id；
  `state.needs_delta` 的 `payload.changes[]`（`new_value` 直写需求/情绪；need='mood' 同步 mood）。
- 历史时刻对话显整段文本、不做连续状态机动画（边界 1/2）；关系增量走 `/api/relations/*`（05 §6 行）。
- 正确性自检 = `scripts/snapshot_diff_check.py`：合并结果 vs 下一份快照字段级 diff（03 §4.2）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .app import ApiError
from .rest_snapshot import assemble_snapshot, sim_day_number, snapshot_tick

MERGE_EVENT_TYPES = ("agent.move", "state.needs_delta")  # 归约类型集（03 §4.2 边界 3 UI 字段）


async def _snapshot_before(pool: Any, sim_time: dt.datetime) -> dict[str, Any] | None:
    """≤sim_time 的最近快照（state.sim.sim_time 为快照时点口径，05 §3.6）。"""
    rows = await pool.fetch(
        "SELECT sim_day, state::text AS state FROM obs.world_state_snapshot ORDER BY sim_day DESC"
    )
    chosen = None
    for r in rows:
        import json
        state = json.loads(r["state"])
        st = (state.get("sim") or {}).get("sim_time")
        if st and dt.datetime.fromisoformat(st) <= sim_time:
            chosen = {"sim_day": r["sim_day"], "state": state}
            break
    return chosen


def apply_events(state: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """纯函数归约（事件按 seq 升序输入；输出新 state dict，不改入参）。"""
    import copy
    import json as _json

    out = copy.deepcopy(state)
    agents = {a["id"]: a for a in out.get("agents") or []}
    for ev in events:
        payload = ev.get("payload") or {}
        if ev["type"] == "agent.move":
            actor = (ev.get("actors") or [None])[0]
            if actor in agents and payload.get("to"):
                agents[actor]["position"] = payload["to"]
        elif ev["type"] == "state.needs_delta":
            for ch in payload.get("changes") or []:
                a = agents.get(ch.get("agent_id"))
                if a is None:
                    continue
                needs = a.setdefault("needs", {})
                if ch.get("need") and ch.get("new_value") is not None:
                    needs[ch["need"]] = ch["new_value"]
                    if ch["need"] == "mood":
                        a["mood"] = ch["new_value"]
    return out


async def merged_state_at_tick(pool: Any, tick: int) -> tuple[dict[str, Any], dt.datetime]:
    """合并核心：返回（合并后 state, 目标 sim_time）。供 /api/snapshot?tick= 与 agents state?at= 共用。"""
    sim_time = await pool.fetchval("SELECT max(sim_time) FROM obs.events WHERE tick <= $1", tick)
    if sim_time is None:
        raise ApiError("tick_out_of_range", f"tick={tick} 之前无事件", 422)
    snap = await _snapshot_before(pool, sim_time)
    if snap is None:
        raise ApiError("tick_out_of_range", f"tick={tick} 早于首份世界快照", 422)
    base_tick = await snapshot_tick(pool, (snap["state"].get("sim") or {}).get("sim_time"))
    rows = await pool.fetch(
        """
        SELECT seq, type, actors, payload::text AS payload FROM obs.events
        WHERE tick > $1 AND tick <= $2 AND type = ANY($3::text[])
        ORDER BY seq
        """,
        base_tick, tick, list(MERGE_EVENT_TYPES),
    )
    import json
    events = [
        {"type": r["type"], "actors": list(r["actors"] or []),
         "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]}
        for r in rows
    ]
    merged = apply_events(snap["state"], events)
    merged.setdefault("sim", {})["sim_time"] = sim_time.isoformat()
    return merged, sim_time


async def snapshot_at_tick(pool: Any, tick: int) -> dict[str, Any]:
    """合并至 tick 的世界投影（03 §5.1 /api/snapshot data 形态；同 tick 两次调用逐字节一致）。"""
    merged, sim_time = await merged_state_at_tick(pool, tick)
    data = await assemble_snapshot(pool, {"sim_day": sim_time.astimezone().date(), "state": merged})
    data["tick"] = tick  # 历史查询的 tick = 请求 tick（非快照时点）
    data["sim_day"] = await sim_day_number(pool, sim_time.astimezone().date())
    return data


async def agent_state_at_tick(pool: Any, agent_id: str, tick: int) -> dict[str, Any]:
    """`/api/agents/:id/state?at=` 历史态（T-WEB-03 挂载点）：合并后的单人需求/情绪/目标/LOD。"""
    merged, _ = await merged_state_at_tick(pool, tick)
    for a in merged.get("agents") or []:
        if a["id"] == agent_id:
            goals = a.get("goals") or []
            return {
                "id": agent_id, "lod": a.get("cognition_tier"), "needs": a.get("needs"),
                "mood": a.get("mood"), "goals": goals,
                "frustration": max((int(g.get("frustration") or 0) for g in goals), default=0),
            }
    raise ApiError("agent_not_found", f"快照中无角色 {agent_id}", 404)
