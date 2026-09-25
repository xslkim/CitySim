"""REST：角色档案/状态/日程/反思接口组（05 T-WEB-03；03 §5.1、05 §3.6/§6、05 §3.2）。

- 人设卡 = `persona_display` 六键（big_five/backstory/appearance/signature_quirk/contrast_public/
  speech_style_public；`secret`/`trigger_point` 永不出站，05 §3.6 P2-6）+ 人口学字段 + 周目标。
- agent id 入参校验 `^A(0[1-9]|[1-3][0-9]|40)$`（04 §5.2 CHECK 同形），非法 422。
- 反思 = `obs.memory_projection WHERE kind='reflection'`，仅 `content_display`（05 §3.2 唯一全文通道）。
- 数据源 = 最新 `obs.world_state_snapshot`（人设静态字段不单独建表，05 §6 行）；
  `?at=<tick>` 历史态归 T-WEB-04 合并路径。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from .app import ApiError, get_pool, ok_envelope, require_token
from .rest_snapshot import latest_snapshot
from .serde import serialize_event

router = APIRouter(prefix="/api/agents", dependencies=[Depends(require_token)])

AGENT_ID_PATTERN = r"^A(0[1-9]|[1-3][0-9]|40)$"  # 04 §5.2 CHECK 同形


async def _agent_row(pool: Any, agent_id: str) -> dict[str, Any]:
    snap = await latest_snapshot(pool)
    if snap is None:
        raise ApiError("no_snapshot", "尚无世界快照（先跑 obs_refresh.py）", 404)
    for a in snap["state"].get("agents") or []:
        if a["id"] == agent_id:
            return a
    raise ApiError("agent_not_found", f"快照中无角色 {agent_id}", 404)


def _persona_card(a: dict[str, Any]) -> dict[str, Any]:
    """人设卡 = 人口学 + persona_display 六键 + 目标（secret/trigger_point 不存在于白名单快照）。"""
    return {
        "id": a["id"], "name": a["name"], "gender": a.get("gender"), "age": a.get("age"),
        "room_no": a.get("room_no"), "department": a.get("department"), "job_title": a.get("job_title"),
        "lod": a.get("cognition_tier"),
        "persona_display": a.get("persona_display") or {},
        "routine": a.get("routine") or {},
        "goals": a.get("goals") or [],
    }


@router.get("")
async def api_agents(pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """40 人列表（人设摘要字段：人口学 + LOD + 签名色外观摘要，供列表/头像兜底取色）。"""
    snap = await latest_snapshot(pool)
    if snap is None:
        raise ApiError("no_snapshot", "尚无世界快照（先跑 obs_refresh.py）", 404)
    items = []
    for a in snap["state"].get("agents") or []:
        pd = a.get("persona_display") or {}
        appearance = pd.get("appearance") or {}
        items.append({
            "id": a["id"], "name": a["name"], "gender": a.get("gender"), "age": a.get("age"),
            "room_no": a.get("room_no"), "department": a.get("department"), "job_title": a.get("job_title"),
            "lod": a.get("cognition_tier"),
            "signature_color": (appearance.get("signature_color") or {}).get("hex"),
            "appearance": {k: v for k, v in appearance.items() if k != "signature_color"} or None,
        })
    return await ok_envelope(pool, {"items": items})


@router.get("/{agent_id}")
async def api_agent(
    agent_id: str = Path(pattern=AGENT_ID_PATTERN),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    return await ok_envelope(pool, _persona_card(await _agent_row(pool, agent_id)))


@router.get("/{agent_id}/state")
async def api_agent_state(
    agent_id: str = Path(pattern=AGENT_ID_PATTERN),
    at: int | None = Query(default=None),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """需求六维/情绪/挫败值/周目标/LOD；`?at=<tick>` 历史态走 T-WEB-04 合并。"""
    if at is not None:
        try:
            from .snapshot_merge import agent_state_at_tick
        except ImportError as e:
            raise ApiError("not_implemented", "历史态合并（?at=）归 T-WEB-04") from e
        return await ok_envelope(pool, await agent_state_at_tick(pool, agent_id, at))
    a = await _agent_row(pool, agent_id)
    goals = a.get("goals") or []
    return await ok_envelope(pool, {
        "id": a["id"],
        "lod": a.get("cognition_tier"),
        "needs": a.get("needs"),
        "mood": a.get("mood"),
        "goals": goals,
        "frustration": max((int(g.get("frustration") or 0) for g in goals), default=0),
    })


@router.get("/{agent_id}/schedule")
async def api_agent_schedule(
    agent_id: str = Path(pattern=AGENT_ID_PATTERN),
    day: str | None = Query(default=None),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """指定模拟日日程 = 快照 `routine` + 当日该 actor 的 public 事件（05 §6 行）。day 缺省 = 最新快照日。"""
    a = await _agent_row(pool, agent_id)
    snap = await latest_snapshot(pool)
    sim_day = dt.date.fromisoformat(day) if day else snap["sim_day"]
    rows = await pool.fetch(
        """
        SELECT * FROM obs.events
        WHERE $1 = ANY(actors) AND visibility = 'public'
          AND (sim_time AT TIME ZONE 'Asia/Shanghai')::date = $2
        ORDER BY seq
        """,
        agent_id, sim_day,
    )
    return await ok_envelope(pool, {
        "id": agent_id,
        "day": sim_day.isoformat(),
        "routine": a.get("routine") or {},
        "events": [serialize_event(r) for r in rows],
    })


@router.get("/{agent_id}/reflections")
async def api_agent_reflections(
    agent_id: str = Path(pattern=AGENT_ID_PATTERN),
    limit: int = Query(default=3, ge=1, le=50),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """最近反思（仅 content_display，05 §3.2 唯一全文通道）。"""
    rows = await pool.fetch(
        """
        SELECT memory_id, sim_time, content_display, importance FROM obs.memory_projection
        WHERE agent_id = $1 AND kind = 'reflection'
        ORDER BY sim_time DESC LIMIT $2
        """,
        agent_id, limit,
    )
    items = [{
        "memory_id": int(r["memory_id"]),
        "sim_time": r["sim_time"].isoformat(),
        "content_display": r["content_display"],
        "importance": r["importance"],
    } for r in rows]
    return await ok_envelope(pool, {"items": items})
