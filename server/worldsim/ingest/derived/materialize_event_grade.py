"""materialize_event_grade（07 T-SYN-07；05 §3.8/§4.2）：event_grade_view 物化。

- 基线：`ui ? 'grade'` 事件（07 D6 摄入侧同排任务）→ `INSERT … ON CONFLICT DO NOTHING`（初值）。
- 复核：`director.grade_revise`（payload `target_seq` 裸 seq 数字字符串/`new_grade`/`reason`）→
  upsert，`ON CONFLICT (seq) DO UPDATE WHERE 传入 revise 事件 seq > 现有 revised_by_seq（或 IS NULL）`
  ——最新复核胜出（05 §3.8）。副本 `events` 行永不 UPDATE（append-only，09 §7 E4 必过项②）。
"""

from __future__ import annotations

import json
from typing import Any


async def materialize(conn: Any, event_seq: int) -> None:
    """消费一条任务源事件（ui.grade 基线 或 director.grade_revise 复核）；幂等。"""
    ev = await conn.fetchrow("SELECT seq, type, payload, ui FROM events WHERE seq=$1", event_seq)
    if ev is None:
        return
    ui = ev["ui"]
    if isinstance(ui, str):
        ui = json.loads(ui)
    if ev["type"] == "director.grade_revise":
        payload = ev["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        target = int(str(payload["target_seq"]))
        # 目标行可能尚未携带 ui.grade（先补基线占位）再按最新复核胜出覆盖
        await conn.execute(
            """
            INSERT INTO event_grade_view (seq, grade)
            SELECT seq, ui->>'grade' FROM events WHERE seq=$1 AND ui ? 'grade'
            ON CONFLICT (seq) DO NOTHING
            """, target)
        await conn.execute(
            """
            INSERT INTO event_grade_view (seq, grade, revised_by_seq, reason)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (seq) DO UPDATE SET grade=$2, revised_by_seq=$3, reason=$4,
              updated_at=now()
            WHERE event_grade_view.revised_by_seq IS NULL
               OR event_grade_view.revised_by_seq < $3
            """, target, str(payload["new_grade"]), int(ev["seq"]), payload.get("reason"))
    elif ui and "grade" in ui:
        await conn.execute(
            """
            INSERT INTO event_grade_view (seq, grade)
            VALUES ($1, $2) ON CONFLICT (seq) DO NOTHING
            """, int(ev["seq"]), str(ui["grade"]))


async def baseline_sweep(conn: Any) -> int:
    """每日兜底（05 §4.2）：全量基线重扫（ON CONFLICT DO NOTHING，不覆盖复核结果）。"""
    n = await conn.execute(
        """
        INSERT INTO event_grade_view (seq, grade)
        SELECT seq, ui->>'grade' FROM events WHERE ui ? 'grade'
        ON CONFLICT (seq) DO NOTHING
        """)
    return int(n.split()[-1])
