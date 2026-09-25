"""内核三流读取器 · events/memories 两流（07 T-SYN-02；04 §1.3 outbox = events 表本身 + sync_state 三位点）。

- events 流：白名单 15 列（05 §3.1），`WHERE seq > last_acked_seq ORDER BY seq`；payload 经
  T-SYN-01 键白名单剥除为同步规范形。
- block→internal 事件**整行不出站**（07 D3 取整行不出站分支）：判定规则（工程默认）=
  `visibility='internal'` 且 payload 携带 `text_raw`——正常 internal 类型（state.*/relation.*/
  time.*/system.llm.*）从不携带 text_raw，唯安全管线 block 落库事件携带原文（04 §11.1/§11.2），
  原文与该行永不出站。
- memories 流：投影行形态按 05 §3.2 八列；**只出已出行**（`content_display IS NOT NULL`，
  未过审行两侧同时缺席，05 §2.2 口径）；`content`/`embedding`/`archived` 永不出站；位点 =
  `last_acked_memory_id`，按 id 升序。
"""

from __future__ import annotations

import json
from typing import Any

from .whitelist import strip_payload

EVENT_COLUMNS = ("seq", "tick", "sim_time", "wall_time", "type", "source", "trigger",
                 "arc_id", "location_id", "actors", "rng_seed", "payload",
                 "visibility", "ui", "schema_version")  # 05 §3.1 白名单 15 列

MEMORY_COLUMNS = ("memory_id", "agent_id", "sim_time", "kind", "content_display",
                  "importance", "source_event_seq", "is_witness")  # 05 §3.2 八列


async def fetch_events_after(
    pool: Any, last_acked_seq: int, *, limit: int, whitelist: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """events 流待发送行（seq 升序；block→internal 整行跳过，payload 剥除为同步规范形）。"""
    rows = await pool.fetch(
        """
        SELECT seq, tick, sim_time, wall_time, type, source, trigger, arc_id, location_id,
               actors, rng_seed, payload, visibility, ui, schema_version
        FROM events
        WHERE seq > $1 AND NOT (visibility = 'internal' AND payload ? 'text_raw')  -- 07 D3 整行不出站
        ORDER BY seq LIMIT $2
        """,
        last_acked_seq, limit,
    )
    out = []
    for r in rows:
        row = dict(r)
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        row["payload"] = strip_payload(payload, type_=row["type"], visibility=row["visibility"],
                                       whitelist=whitelist)
        if isinstance(row.get("ui"), str):
            row["ui"] = json.loads(row["ui"])
        out.append(row)
    return out


async def fetch_memory_projections_after(
    pool: Any, last_acked_memory_id: int, *, limit: int,
) -> list[dict[str, Any]]:
    """memories 投影流待发送行（id 升序；只出已出行，content/embedding/archived 不出站）。"""
    rows = await pool.fetch(
        """
        SELECT id AS memory_id, agent_id, sim_time, kind, content_display, importance,
               source_event_seq, is_witness
        FROM memories
        WHERE id > $1 AND content_display IS NOT NULL
        ORDER BY id LIMIT $2
        """,
        last_acked_memory_id, limit,
    )
    return [dict(r) for r in rows]


async def read_sync_state(pool: Any) -> dict[str, Any]:
    """sync_state 单行三位点（04 §5.2 DDL）。"""
    r = await pool.fetchrow(
        "SELECT last_acked_seq, last_acked_memory_id, last_acked_snapshot_day FROM sync_state WHERE id=1")
    return dict(r) if r else {"last_acked_seq": 0, "last_acked_memory_id": 0,
                              "last_acked_snapshot_day": None}


async def update_sync_state(
    pool: Any, *, upto: int | None = None, mem_upto: int | None = None, snap_upto: Any = None,
) -> None:
    """ACK 后推进位点（04 §1.3 流程；三流各推各位点，None = 不动该列）。"""
    await pool.execute(
        """
        UPDATE sync_state SET
          last_acked_seq = COALESCE($2::bigint, last_acked_seq),
          last_acked_memory_id = COALESCE($3::bigint, last_acked_memory_id),
          last_acked_snapshot_day = COALESCE($4::date, last_acked_snapshot_day),
          updated_at = now()
        WHERE id = $1::smallint
        """,
        1, upto, mem_upto, snap_upto,
    )
