"""REST：世界快照接口（05 T-WEB-03；03 §5.1 /api/snapshot 形态、05 §3.6 快照白名单）。

- 无参 = 最新 `obs.world_state_snapshot` 组装；`?tick=` 历史合并归 `snapshot_merge.py`（T-WEB-04）。
- `agents[]` 仅 UI 状态子集（05 §3.6 白名单键）；`compression_ratio`/`activity` 由白名单供数（D14 销项）；
  `economy` 按 `stocks[]` 形态透传（03 §5.1 R1 回登）。
- `active_dialogues` = 最近 1 tick 内 public dialogue 事件（工程默认窗口，05 文档 D6②）；
  `lines` 整段 + `at_offset_s` 原样下发（06 §2）。
- `sim_day` = 相对首事件模拟日的日序整数（03 §5.1 示例 int 形态；obs 层无内核 world_state 时钟键，
  epoch 取 `obs.events` 最早 sim_time 的本地日期为 Day 1，05 文档偏差表登记）。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from .app import ApiError, get_pool, ok_envelope, require_token
from .serde import serialize_event

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))  # 模拟日聚合时区（01 文档 §6 D4）


async def sim_day_number(pool: Any, day: dt.date | str | None) -> int:
    """模拟日序整数：epoch = obs.events 最早 sim_time 的本地日期 = Day 1。"""
    if day is None:
        return 0
    if isinstance(day, str):
        day = dt.date.fromisoformat(day)
    epoch = await pool.fetchval(
        "SELECT (min(sim_time) AT TIME ZONE 'Asia/Shanghai')::date FROM obs.events"
    )
    if epoch is None:
        return 1
    return (day - epoch).days + 1


async def latest_snapshot(pool: Any) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT sim_day, state::text AS state FROM obs.world_state_snapshot ORDER BY sim_day DESC LIMIT 1"
    )
    if row is None:
        return None
    return {"sim_day": row["sim_day"], "state": json.loads(row["state"])}


async def snapshot_tick(pool: Any, sim_time_iso: str | None) -> int:
    """快照对应 tick = ≤快照 sim_time 的最大事件 tick（无事件 = 0）。"""
    if not sim_time_iso:
        v = await pool.fetchval("SELECT max(tick) FROM obs.events")
        return int(v or 0)
    v = await pool.fetchval(
        "SELECT max(tick) FROM obs.events WHERE sim_time <= $1",
        dt.datetime.fromisoformat(str(sim_time_iso)),
    )
    return int(v or 0)


def agent_ui_view(a: dict[str, Any]) -> dict[str, Any]:
    """03 §5.1 agents[] UI 状态子集。"""
    return {
        "id": a["id"],
        "name": a["name"],
        "lod": a.get("cognition_tier"),
        "location_id": a.get("position"),
        "activity": a.get("activity"),
        "mood": a.get("mood"),
        "needs": a.get("needs"),
    }


async def fetch_active_dialogues(pool: Any, tick: int) -> list[dict[str, Any]]:
    """最近 1 tick 内 public dialogue 事件（D6② 工程默认窗口）；lines 整段原样（06 §2）。"""
    if tick <= 0:
        return []
    rows = await pool.fetch(
        """
        SELECT * FROM obs.events
        WHERE type LIKE 'dialogue.%%' AND visibility = 'public' AND tick > $1 - 1
        ORDER BY seq
        """,
        tick,
    )
    out = []
    for r in rows:
        ev = serialize_event(r)
        p = ev["payload"]
        participants = p.get("participants") or [
            x for x in (p.get("teller"), p.get("listener")) if x
        ]
        out.append({
            "event_seq": ev["seq"],
            "participants": participants,
            "location_id": p.get("location_id"),
            "lines": p.get("lines") or [],
        })
    return out


async def assemble_snapshot(pool: Any, snap: dict[str, Any]) -> dict[str, Any]:
    """state JSONB → 03 §5.1 /api/snapshot data 形态（T-WEB-04 历史合并复用本函数）。"""
    state = snap["state"]
    sim = state.get("sim") or {}
    tick = await snapshot_tick(pool, sim.get("sim_time"))
    return {
        "tick": tick,
        "sim_time": sim.get("sim_time"),
        "sim_day": await sim_day_number(pool, snap.get("sim_day")),
        "compression_ratio": sim.get("compression_ratio"),
        "agents": [agent_ui_view(a) for a in state.get("agents") or []],
        "economy": state.get("economy") or {"stocks": []},
        "active_dialogues": await fetch_active_dialogues(pool, tick),
    }


@router.get("/snapshot")
async def api_snapshot(
    tick: int | None = Query(default=None),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    if tick is not None:
        # 历史合并（T-WEB-04 交付）；模块缺失 = 尚未交付的明确错误
        try:
            from .snapshot_merge import snapshot_at_tick
        except ImportError as e:
            raise ApiError("not_implemented", "历史合并（?tick=）归 T-WEB-04") from e
        data = await snapshot_at_tick(pool, tick)
        return await ok_envelope(pool, data, kind="snapshot")
    snap = await latest_snapshot(pool)
    if snap is None:
        raise ApiError("no_snapshot", "尚无世界快照（obs.world_state_snapshot 为空，先跑 obs_refresh.py）", 404)
    return await ok_envelope(pool, await assemble_snapshot(pool, snap), kind="snapshot")
