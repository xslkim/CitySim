"""T-SYN-07 投影类 derived worker 验收（05 §3.3/§4.1~§4.3）。三测试文件共用本 fixture 模块。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import asyncpg

from tests.conftest import DDL_DIR, _build_db, _drop_db

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
BASE = dt.datetime(2026, 10, 12, 9, 0, tzinfo=LOCAL_TZ)


async def ins_event(pool: Any, seq: int, type_: str, payload: dict, *, visibility: str = "internal",
                    ui: dict | None = None, minutes: int = 0, sim_time: dt.datetime | None = None,
                    trigger: str = "autonomous") -> int:
    await pool.execute(
        """
        INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, actors,
                            payload, visibility, ui)
        VALUES ($1,$1,$2,$2,$3,'agent:A01',$4,'{A01}',$5::jsonb,$6,$7::jsonb)
        """,
        seq, sim_time or (BASE + dt.timedelta(minutes=minutes or seq)), type_, trigger,
        json.dumps(payload, ensure_ascii=False), visibility,
        json.dumps(ui, ensure_ascii=False) if ui else None)
    return seq


async def ins_memory(pool: Any, memory_id: int, agent: str, *, source_event_seq: int | None,
                     kind: str = "projection", content_display: str = "展示文本",
                     sim_time: dt.datetime | None = None) -> None:
    await pool.execute(
        """
        INSERT INTO memory_projection (memory_id, agent_id, sim_time, kind, content_display,
                                       importance, source_event_seq, is_witness)
        VALUES ($1,$2,$3,$4,$5,5,$6,FALSE)
        """, memory_id, agent, sim_time or BASE, kind, content_display, source_event_seq)


async def ins_task(pool: Any, task_type: str, dedupe_key: str,
                   sim_day: dt.date | None = None) -> None:
    await pool.execute(
        "INSERT INTO derived_task (task_type, dedupe_key, sim_day) VALUES ($1,$2,$3)"
        " ON CONFLICT (dedupe_key) DO NOTHING",
        task_type, dedupe_key, sim_day or BASE.date())
