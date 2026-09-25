"""云端 digest 比对与当日重灌编排（07 T-SYN-09；04 §9.1/§9.2 + 05 §2.2/§4.3）。

- `/v1/digest` 路由：收本机推送 → 副本侧自算（同一 canonical/digest 唯一实现 = 02 T-ADJ-09，
  副本 payload 已是同步规范形，白名单剥除幂等）→ 逐日比对（**窗口截止 T−2 模拟日**，
  T = 副本已知最大模拟日，07 D 系工程默认）→ 结果落 `digest_log`（D5）。
- 不一致处置（04 §9.2）：该 sim_day `DELETE FROM events/memory_projection/world_state_snapshot`
  → 派生表对该日排全量重算任务（05 §4.3）→ 响应 `{status:"mismatch", mismatch_days:[...]}`；
  本机重发该日（帧带 `mode:"reproject"` 豁免乱序校验，D4）→ 复检一致闭环。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..snapshot.canonical import canonical, digest_events, sha256_hex
from .routes import require_ingest_token

log = logging.getLogger(__name__)

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
WINDOW_LAG = 2  # T−2（05 §2.2）

router = APIRouter(prefix="/v1")


async def replica_day_digest(pool: Any, sim_day: dt.date) -> dict[str, Any]:
    """副本侧自算一日 digest（与本机 build_push_body 同口径同形态）。"""
    ev = await digest_events(pool, sim_day)
    rows = await pool.fetch(
        "SELECT memory_id, sim_time FROM memory_projection ORDER BY memory_id")
    count, sum_id = 0, 0
    for r in rows:
        if r["sim_time"].astimezone(LOCAL_TZ).date() == sim_day:
            count += 1
            sum_id += int(r["memory_id"])
    snap_row = await pool.fetchrow(
        "SELECT state, digest FROM world_state_snapshot WHERE sim_day=$1", sim_day)
    snap = None
    if snap_row is not None:
        # 比对用 digest = 对库存 state 现场重算（不信存储列，篡改可检出，05 §3.6 复核语义）
        state = snap_row["state"]
        if isinstance(state, str):
            state = json.loads(state)
        snap = "sha256:" + sha256_hex(canonical(state))
    return {"sim_day": sim_day.isoformat(),
            "events": {"count": ev["count"], "sum_seq": ev["sum_seq"], "digest": ev["digest"]},
            "memories": {"count": count, "sum_id": sum_id},
            "snapshot_digest": snap}


def compare_day(local: dict[str, Any], remote: dict[str, Any]) -> list[str]:
    """逐字段比对；返回不一致字段清单（空 = 一致）。"""
    diffs = []
    for k in ("count", "sum_seq", "digest"):
        if local["events"].get(k) != remote["events"].get(k):
            diffs.append(f"events.{k}")
    for k in ("count", "sum_id"):
        if local["memories"].get(k) != remote["memories"].get(k):
            diffs.append(f"memories.{k}")
    if local.get("snapshot_digest") is not None and \
            local.get("snapshot_digest") != remote.get("snapshot_digest"):
        diffs.append("snapshot_digest")
    return diffs


async def reproject_day(pool: Any, sim_day: dt.date) -> None:
    """当日重灌（04 §9.2）：删该日三流原始层 + 排派生全量重算（05 §4.3）；单事务。"""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM events WHERE (sim_time AT TIME ZONE 'Asia/Shanghai')::date=$1", sim_day)
        await conn.execute(
            "DELETE FROM memory_projection WHERE (sim_time AT TIME ZONE 'Asia/Shanghai')::date=$1",
            sim_day)
        await conn.execute("DELETE FROM world_state_snapshot WHERE sim_day=$1", sim_day)
        for tt, prefix in (("refresh_relation_daily", "relation_daily"),
                           ("refresh_health_daily", "health_daily")):
            await conn.execute(
                """
                INSERT INTO derived_task (task_type, dedupe_key, sim_day)
                VALUES ($1,$2,$3) ON CONFLICT (dedupe_key) DO NOTHING
                """, tt, f"reproject:{prefix}:{sim_day.isoformat()}", sim_day)
    log.warning("digest 不一致 → sim_day=%s 当日重灌编排完成（待本机重发，04 §9.2）", sim_day)


async def _log_result(pool: Any, sim_day: dt.date, status: str, detail: dict[str, Any]) -> None:
    await pool.execute(
        "INSERT INTO digest_log (sim_day, status, detail) VALUES ($1,$2,$3::jsonb)",
        sim_day, status, json.dumps(detail, ensure_ascii=False))


@router.post("/digest")
async def push_digest(request: Request, body: dict[str, Any],
                      _auth: None = Depends(require_ingest_token)) -> JSONResponse:
    """04 §9.1 body 契约；响应 `{status: ok|mismatch|skipped_window, mismatch_days[]}`（07 D4）。"""
    pool = request.app.state.pool
    day = dt.date.fromisoformat(str(body.get("sim_day")))
    today = await pool.fetchval(
        "SELECT max((sim_time AT TIME ZONE 'Asia/Shanghai')::date) FROM events")
    remote = await replica_day_digest(pool, day)
    if today is not None and day > today - dt.timedelta(days=WINDOW_LAG):
        await _log_result(pool, day, "skipped_window", {"today": today.isoformat()})
        return JSONResponse(content={"status": "skipped_window", "mismatch_days": []})
    diffs = compare_day(body, remote)
    if not diffs:
        await _log_result(pool, day, "ok", {"remote": remote})
        return JSONResponse(content={"status": "ok", "mismatch_days": []})
    log.error("digest 不一致 sim_day=%s 字段=%s → 当日重灌（04 §9.2）", day, diffs)
    await _log_result(pool, day, "mismatch", {"diffs": diffs, "local": body, "remote": remote})
    await reproject_day(pool, day)
    await _log_result(pool, day, "reprojected", {"diffs": diffs})
    return JSONResponse(content={"status": "mismatch", "mismatch_days": [day.isoformat()]})
