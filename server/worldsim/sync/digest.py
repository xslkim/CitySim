"""本机 digest 计算与推送（07 T-SYN-09；04 §9.1/§9.2）。

- 本机口径：events 用 02 T-ADJ-09 `digest_events`（唯一实现同源消费，R2 §A.7）；
  memory_projection 用 `COUNT(*)/SUM(memory_id)` 按 `date(sim_time)` 分组、**只计已出行**
  （`content_display IS NOT NULL`，两端同一过滤，05 §2.2）；snapshot 复核随帧 digest（sidecar 文件）。
- 推送：`POST {WSIM_CLOUD_INGEST_HTTPS}/v1/digest`，body `{sim_day, events:{count,sum_seq,digest},
  memories:{count,sum_id}, snapshot_digest}`（04 §9.1 契约）；Bearer 鉴权。
- 时点：每日凌晨 batch 段与 04:10 备份完成之后（04 §9.2）；`push_loop` 为常驻协程，
  `digest_check.py` 手动触发同款校验（演练/排障入口）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from ..snapshot.canonical import digest_events
from .snapshot import DEFAULT_SNAPSHOT_DIR

log = logging.getLogger(__name__)

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
COMPARE_WINDOW_LAG_DAYS = 2  # 比对窗口截止 T−2 模拟日（05 §2.2 P2-7）


async def digest_memories(pool: Any, sim_day: dt.date) -> dict[str, Any]:
    """memories 流 digest：COUNT(*)/SUM(id)，只计已出行（content_display IS NOT NULL）。"""
    rows = await pool.fetch(
        "SELECT id, sim_time FROM memories WHERE content_display IS NOT NULL ORDER BY id")
    count, sum_id = 0, 0
    for r in rows:
        if r["sim_time"].astimezone(LOCAL_TZ).date() == sim_day:
            count += 1
            sum_id += int(r["id"])
    return {"count": count, "sum_id": sum_id}


def snapshot_digest_from_disk(sim_day: dt.date, *, out_dir: str | Path | None = None) -> str | None:
    """随帧 digest（var/snapshot sidecar；缺文件 → None，推送体该键缺席由云端按无快照处理）。"""
    p = Path(out_dir or DEFAULT_SNAPSHOT_DIR) / f"snapshot_{sim_day.isoformat()}.digest"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


async def build_push_body(pool: Any, sim_day: dt.date, *,
                          out_dir: str | Path | None = None) -> dict[str, Any]:
    """04 §9.1 /v1/digest body 契约逐键。"""
    ev = await digest_events(pool, sim_day, exclude_blocked=True)  # 07 D3：block 行不计入（两端一致）
    mem = await digest_memories(pool, sim_day)
    return {
        "sim_day": sim_day.isoformat(),
        "events": {"count": ev["count"], "sum_seq": ev["sum_seq"], "digest": ev["digest"]},
        "memories": {"count": mem["count"], "sum_id": mem["sum_id"]},
        "snapshot_digest": snapshot_digest_from_disk(sim_day, out_dir=out_dir),
    }


def in_compare_window(sim_day: dt.date, today: dt.date) -> bool:
    """比对窗口：sim_day ≤ T−2（T−1/T 不比对、不报警、不重灌，05 §2.2/04 §9.2）。"""
    return sim_day <= today - dt.timedelta(days=COMPARE_WINDOW_LAG_DAYS)


async def push_once(pool: Any, sim_day: dt.date, *, base_url: str | None = None,
                    token: str | None = None, out_dir: str | Path | None = None) -> dict[str, Any]:
    """推送一日 digest，返回云端响应 `{status, mismatch_days[]}`（07 D4 工程默认响应体）。"""
    base = (base_url or os.environ.get("WSIM_CLOUD_INGEST_HTTPS")
            or "http://127.0.0.1:9100").rstrip("/")
    tok = token if token is not None else os.environ.get("WSIM_INGEST_TOKEN", "")
    body = await build_push_body(pool, sim_day, out_dir=out_dir)
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{base}/v1/digest", json=body,
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        r.raise_for_status()
        resp = r.json()
    log.info("digest 推送 sim_day=%s → %s", sim_day, resp.get("status"))
    return resp


async def push_loop(pool: Any, *, get_today: Any = None) -> None:  # pragma: no cover - 常驻协程
    """每日凌晨推送 ≤T−2 各日 digest（04 §9.2；开发期演练用 digest_check.py 手动触发）。"""
    pushed: set[str] = set()
    while True:
        today = get_today() if get_today else dt.datetime.now(LOCAL_TZ).date()
        rows = await pool.fetch("SELECT DISTINCT sim_time FROM events ORDER BY 1")
        days = sorted({r["sim_time"].astimezone(LOCAL_TZ).date() for r in rows})
        for day in days:
            if in_compare_window(day, today) and day.isoformat() not in pushed:
                try:
                    await push_once(pool, day)
                    pushed.add(day.isoformat())
                except Exception:  # noqa: BLE001
                    log.exception("digest 推送失败 sim_day=%s（下轮重试）", day)
        await asyncio.sleep(3600)
