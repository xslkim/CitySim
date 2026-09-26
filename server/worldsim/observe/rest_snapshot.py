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
    """R1 #1：当日滚动 latest 行优先（世界日内推进的展示数据源）；无则回退日界定稿行。

    `obs.world_state_latest` 为副本库增量表（replica_world_state_latest.sql）：未建表的主库直连
    形态（M4/07 D8 切换桥）静默回退日界行，保持 T-SYN-10 零代码变更前提。
    """
    has_latest = await pool.fetchval("SELECT to_regclass('obs.world_state_latest') IS NOT NULL")
    if has_latest:
        row = await pool.fetchrow(
            "SELECT tick, sim_time, state::text AS state FROM obs.world_state_latest WHERE id = 1"
        )
        if row is not None:
            # T-ITER2-03：rolling 分支补 sim_day 派生（帧头 sim_time 的本地日期），
            # 与 day_end 分支键集一致——schedule 等消费方缺省日依赖本键（回归护栏见
            # tests/observe/test_latest_snapshot_contract.py）
            sim_time = row["sim_time"]
            sim_day = (sim_time.astimezone(LOCAL_TZ).date()
                       if isinstance(sim_time, dt.datetime) else None)
            return {"kind": "rolling", "tick": int(row["tick"]),
                    "sim_time": sim_time.isoformat(),
                    "sim_day": sim_day,
                    "state": json.loads(row["state"])}
    row = await pool.fetchrow(
        "SELECT sim_day, state::text AS state FROM obs.world_state_snapshot ORDER BY sim_day DESC LIMIT 1"
    )
    if row is None:
        return None
    return {"kind": "day_end", "sim_day": row["sim_day"], "state": json.loads(row["state"])}


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


async def fetch_llm_status(pool: Any) -> dict[str, Any]:
    """R1 #2：LLM 降级运行态（观众/GM 可见信号）——近 2 模拟小时 system.llm.failover 事件口径。

    `degraded` = 最近一次 star_decision failover 落入 chain_end（降级链走尽 → 裁决 think 兜底）；
    failover 事件 = 内核 breaker 落库（internal、结构化四键），随事件流同步副本，本层只读聚合。
    """
    ref = await pool.fetchval("SELECT max(sim_time) FROM obs.events")
    if ref is None:
        return {"degraded": False, "failover_count": 0, "last_reason": None}
    window_start = ref - dt.timedelta(hours=2)
    rows = await pool.fetch(
        """
        SELECT payload::text AS payload, sim_time FROM obs.events
        WHERE type = 'system.llm.failover' AND sim_time >= $1
        ORDER BY seq DESC
        """,
        window_start,
    )
    count = len(rows)
    latest_star = next(
        (json.loads(r["payload"]) for r in rows
         if json.loads(r["payload"]).get("task_type") == "star_decision"), None)
    degraded = bool(latest_star and latest_star.get("to_provider") == "chain_end")
    return {"degraded": degraded, "failover_count": count,
            "last_reason": (latest_star or {}).get("reason") if latest_star else None}


async def assemble_snapshot(pool: Any, snap: dict[str, Any]) -> dict[str, Any]:
    """state JSONB → 03 §5.1 /api/snapshot data 形态（T-WEB-04 历史合并复用本函数）。

    R1 #1：滚动行自带 tick/sim_time（帧头字段），日界行 tick = ≤快照 sim_time 的最大事件 tick。
    `snapshot_time`/`snapshot_kind` 为展示层时点标注数据源（刷新间隔 > 数秒不得自称 live）。
    """
    state = snap["state"]
    sim = state.get("sim") or {}
    if snap.get("kind") == "rolling":
        tick = int(snap["tick"])
        sim_time = snap.get("sim_time") or sim.get("sim_time")
    else:
        sim_time = sim.get("sim_time")
        tick = await snapshot_tick(pool, sim_time)
    return {
        "tick": tick,
        "sim_time": sim_time,
        "snapshot_time": sim_time,       # R1 #1：快照自身时点（展示层"截至 HH:MM"标注）
        "snapshot_kind": snap.get("kind") or "day_end",
        "sim_day": await sim_day_number(pool, (snap.get("sim_day") or
                                               (dt.date.fromisoformat(str(sim_time)[:10])
                                                if sim_time else None))),
        "compression_ratio": sim.get("compression_ratio"),
        "agents": [agent_ui_view(a) for a in state.get("agents") or []],
        "economy": state.get("economy") or {"stocks": []},
        "active_dialogues": await fetch_active_dialogues(pool, tick),
        "llm_status": await fetch_llm_status(pool),  # R1 #2：降级运行态（三端观众信号）
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
