"""obs-api 事件行 → 03 §5.1 单条事件协议形态的序列化（唯一实现，REST/WS 共用）。

- 顶层字段逐字 03 §5.1：seq/tick/sim_time/type/source/trigger/arc_id/ui/payload；
  `ui` JSONB 原样（含 grade 初值；最新生效 grade 读 obs.event_grade_view，05 §3.8）。
- `location_id` 为出站白名单列（05 §3.1），并入 `payload.location_id` 下发（03 §5.1 示例形态；
  原 payload 已含同名键时不覆盖）。
- 不新增任何字段；`text_raw`/internal 键已在 obs 视图层剥除（00 §4 红线 7），本层不再过滤。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any


def _jsonable(v: Any) -> Any:
    if isinstance(v, str):
        return v
    return json.loads(json.dumps(v, default=str))


def serialize_event(row: Any) -> dict[str, Any]:
    """asyncpg Record（obs.events 行）→ 协议 dict。"""
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    payload = dict(payload or {})
    if row["location_id"] and "location_id" not in payload:
        payload["location_id"] = row["location_id"]
    ui = row["ui"]
    if isinstance(ui, str):
        ui = json.loads(ui)
    sim_time = row["sim_time"]
    return {
        "seq": int(row["seq"]),
        "tick": int(row["tick"]),
        "sim_time": sim_time.isoformat() if isinstance(sim_time, dt.datetime) else str(sim_time),
        "type": row["type"],
        "source": row["source"],
        "trigger": row["trigger"],
        "arc_id": row["arc_id"],
        "ui": ui,
        "payload": payload,
    }
