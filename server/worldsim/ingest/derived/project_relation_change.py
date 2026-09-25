"""project_relation_change（07 T-SYN-07；05 §3.3/§4.2）：relation.changed 事件 payload.changes
逐边展开落 relation_change_log；UNIQUE(event_seq,a_id,b_id) 双保险幂等。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


async def project(conn: Any, event_seq: int) -> int:
    """展开一条 relation.changed → relation_change_log 行数（重复执行安全）。"""
    ev = await conn.fetchrow(
        "SELECT seq, sim_time, payload FROM events WHERE seq=$1 AND type='relation.changed'",
        event_seq)
    if ev is None:
        return 0
    payload = ev["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    sim_time = ev["sim_time"]
    sim_day = sim_time.astimezone(LOCAL_TZ).date()
    n = 0
    for c in payload.get("changes") or []:
        await conn.execute(
            """
            INSERT INTO relation_change_log (event_seq, a_id, b_id, delta_affinity, delta_tension,
                                             labels_added, labels_removed, sim_time, sim_day)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9)
            ON CONFLICT (event_seq, a_id, b_id) DO NOTHING
            """,
            event_seq, c["a_id"], c["b_id"], int(c.get("delta_affinity", 0)),
            int(c.get("delta_tension", 0)),
            json.dumps(c.get("labels_added"), ensure_ascii=False)
            if c.get("labels_added") is not None else None,
            json.dumps(c.get("labels_removed"), ensure_ascii=False)
            if c.get("labels_removed") is not None else None,
            sim_time, sim_day)
        n += 1
    return n
