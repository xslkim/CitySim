"""摄入事务核心（07 T-SYN-05；04 §9.1 幂等/乱序/schema 校验 + 05 §4.1 单事务排派生任务）。

WS 主通道（ws.py）与 HTTPS 回退（routes.py）共用本模块，保证两通道语义一致：

- 幂等：`events.seq` PK `ON CONFLICT DO NOTHING`；`memory_projection.memory_id` 同理；
  快照按 `sim_day` 覆盖（落库前复核随帧 digest，05 §3.6）。
- 顺序：`from_seq` 必须 = `max(seq)+1`，否则 409 `{expected_seq}`；追平窗口内（8 批 × 500）
  乱序由服务端缓冲排序（BatchBuffer），超窗拒绝（04 §9.1）。
- 重灌豁免（07 D4）：帧带 `mode="reproject"` 跳过顺序校验（当日区间已被 DELETE，幂等兜底安全）。
- 摄入事务 = 原始层行 + `derived_task` 任务行**单事务**（05 §4.1）；任务映射：
  relation.changed→project_relation_change(relchg:e<seq>)、dialogue.gossip→compute_ripple_edge
  (ripple:e<seq>)、director.grade_revise→materialize_event_grade(grade:e<seq>)、
  `ui ? 'grade'` 同排 materialize_event_grade（07 D6 基线物化）；快照到达排
  refresh_relation_daily/refresh_health_daily（`relation_daily:<d-1>`/`health_daily:<d-1>`，定稿 d−1，05 §4.2）。
- schema 校验：逐事件 `schema_version=1` 与必填字段；不合格整批拒绝记日志，不静默丢弃。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from ..snapshot.canonical import canonical, sha256_hex

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CATCHUP_BATCH = 500          # 追平批大小（04 §1.3）
CATCHUP_WINDOW_BATCHES = 8   # 追平流水窗口（04 §1.3/§9.1）

REQUIRED_EVENT_FIELDS = ("seq", "tick", "sim_time", "wall_time", "type", "source",
                         "trigger", "payload", "visibility")
REQUIRED_MEMORY_FIELDS = ("memory_id", "agent_id", "sim_time", "kind", "content_display",
                          "importance")

# 事件类型 → （派生任务类型， dedupe_key 前缀）（05 §4.2 + 07 D6）
_DERIVED_BY_TYPE = {
    "relation.changed": ("project_relation_change", "relchg"),
    "dialogue.gossip": ("compute_ripple_edge", "ripple"),
    "director.grade_revise": ("materialize_event_grade", "grade"),
}


class BatchReject(Exception):
    """整批拒绝（04 §9.1：不静默丢弃）。status 409 = 乱序（带 expected_seq）；400 = schema。"""

    def __init__(self, message: str, *, status: int = 400, expected_seq: int | None = None,
                 reason: str = "schema") -> None:
        super().__init__(message)
        self.status = status
        self.expected_seq = expected_seq
        self.reason = reason


def _parse_ts(value: Any, field: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError as e:
        raise BatchReject(f"字段 {field} 非 ISO 时间戳：{value!r}", reason="schema") from e


def validate_event(ev: dict[str, Any]) -> None:
    """逐事件 schema 校验（04 §9.1：schema_version=1 + 必填字段）。"""
    if not isinstance(ev, dict):
        raise BatchReject("事件行非 object", reason="schema")
    if ev.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise BatchReject(f"未知 schema_version={ev.get('schema_version')}", reason="schema")
    for f in REQUIRED_EVENT_FIELDS:
        if f not in ev:
            raise BatchReject(f"事件缺必填字段 {f}", reason="schema")
    if not isinstance(ev["payload"], dict):
        raise BatchReject("payload 非 object", reason="schema")
    if "text_raw" in ev["payload"]:
        # 防线第二道：出站侧已剥除；帧内出现 text_raw 一律拒收（红线 7）
        raise BatchReject("payload 含 text_raw（红线 7，永不入副本）", reason="schema")


async def _max_seq(conn: Any) -> int:
    return int(await conn.fetchval("SELECT COALESCE(max(seq), 0) FROM events"))


async def _insert_events(conn: Any, events: list[dict[str, Any]]) -> None:
    """单事务：events 行（ON CONFLICT DO NOTHING）+ derived_task 任务行（05 §4.1）。"""
    for ev in events:
        validate_event(ev)
    for ev in events:
        sim_time = _parse_ts(ev["sim_time"], "sim_time")
        sim_day = sim_time.astimezone(dt.timezone(dt.timedelta(hours=8))).date()
        await conn.execute(
            """
            INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, arc_id,
                                location_id, actors, rng_seed, payload, visibility, ui, schema_version)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14::jsonb,$15)
            ON CONFLICT (seq) DO NOTHING
            """,
            int(ev["seq"]), int(ev["tick"]), sim_time, _parse_ts(ev["wall_time"], "wall_time"),
            str(ev["type"]), str(ev["source"]), str(ev["trigger"]), ev.get("arc_id"),
            ev.get("location_id"), list(ev.get("actors") or []),
            ev.get("rng_seed") if ev.get("rng_seed") is None else int(ev["rng_seed"]),
            json.dumps(ev["payload"], ensure_ascii=False), str(ev["visibility"]),
            json.dumps(ev["ui"], ensure_ascii=False) if ev.get("ui") else None,
            SCHEMA_VERSION)
        task = _DERIVED_BY_TYPE.get(str(ev["type"]))
        ui = ev.get("ui") or {}
        if task is None and isinstance(ui, dict) and "grade" in ui:
            task = _DERIVED_BY_TYPE["director.grade_revise"]  # 07 D6：ui.grade 初值基线物化
        if task is not None:
            await conn.execute(
                """
                INSERT INTO derived_task (task_type, dedupe_key, sim_day)
                VALUES ($1, $2, $3) ON CONFLICT (dedupe_key) DO NOTHING
                """,
                task[0], f"{task[1]}:e{int(ev['seq'])}", sim_day)


class BatchBuffer:
    """追平窗口内乱序缓冲（04 §9.1：窗口 8 批内由服务端缓冲排序，超窗拒绝）。"""

    def __init__(self, *, window: int = CATCHUP_WINDOW_BATCHES * CATCHUP_BATCH) -> None:
        self.window = window
        self._pending: dict[int, tuple[int, list[dict[str, Any]]]] = {}  # from_seq → (to_seq, rows)

    async def submit(self, conn: Any, from_seq: int, events: list[dict[str, Any]],
                     *, mode: str | None = None) -> int:
        """提交一批，返回 acked_upto（连续已落库的最大 seq）。"""
        if mode == "reproject":
            async with conn.acquire() as c, c.transaction():
                await _insert_events(c, events)
            return await _max_seq(conn)
        expected = await _max_seq(conn) + 1
        if from_seq > expected:
            if from_seq > expected + self.window:
                raise BatchReject("乱序超窗", status=409, expected_seq=expected, reason="order")
            self._pending[from_seq] = (int(events[-1]["seq"]) if events else from_seq - 1, events)
        else:
            self._pending[from_seq] = (int(events[-1]["seq"]) if events else from_seq - 1, events)
        # 连续段冲刷（重复批 ON CONFLICT 幂等，重叠段安全）
        while True:
            expected = await _max_seq(conn) + 1
            nxt = None
            for fs in sorted(self._pending):
                if fs <= expected:
                    nxt = fs
                    break
            if nxt is None:
                break
            _, rows = self._pending.pop(nxt)
            async with conn.acquire() as c, c.transaction():
                await _insert_events(c, rows)
        return await _max_seq(conn)


async def ingest_memories(conn: Any, from_id: int, memories: list[dict[str, Any]]) -> int:
    """memories 投影批（≤500 单批由调用侧控制；ON CONFLICT (memory_id) DO NOTHING 幂等）。"""
    if len(memories) > CATCHUP_BATCH:
        raise BatchReject(f"单批 {len(memories)} > 500", reason="schema")
    expected = int(await conn.fetchval("SELECT COALESCE(max(memory_id), 0) FROM memory_projection")) + 1
    if from_id > expected + CATCHUP_WINDOW_BATCHES * CATCHUP_BATCH:
        raise BatchReject("乱序超窗", status=409, expected_seq=expected, reason="order")
    async with conn.acquire() as c, c.transaction():
        for m in memories:
            for f in REQUIRED_MEMORY_FIELDS:
                if f not in m:
                    raise BatchReject(f"记忆投影缺字段 {f}", reason="schema")
            if "content" in m or "embedding" in m:
                raise BatchReject("记忆投影含 content/embedding（永不出站，05 §3.2）", reason="schema")
            await c.execute(
                """
                INSERT INTO memory_projection (memory_id, agent_id, sim_time, kind, content_display,
                                               importance, source_event_seq, is_witness)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (memory_id) DO NOTHING
                """,
                int(m["memory_id"]), str(m["agent_id"]), _parse_ts(m["sim_time"], "sim_time"),
                str(m["kind"]), str(m["content_display"]), int(m["importance"]),
                m.get("source_event_seq"), bool(m.get("is_witness", False)))
    return int(await conn.fetchval("SELECT COALESCE(max(memory_id), 0) FROM memory_projection"))


async def ingest_snapshot(conn: Any, sim_day: str, digest: str, state: dict[str, Any]) -> str:
    """快照按 sim_day 幂等覆盖（05 §3.6）；落库前复核随帧 digest；排 d−1 定稿任务（05 §4.2）。"""
    day = dt.date.fromisoformat(str(sim_day))
    expect = "sha256:" + sha256_hex(canonical(state))
    if digest != expect:
        raise BatchReject(f"快照 digest 不符（随帧 {digest} ≠ 复核 {expect}）", reason="digest")
    finalize_day = day - dt.timedelta(days=1)  # 新快照到达 → 定稿 d−1（05 §4.2）
    async with conn.acquire() as c, c.transaction():
        await c.execute(
            """
            INSERT INTO world_state_snapshot (sim_day, state, digest) VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (sim_day) DO UPDATE SET state=$2::jsonb, digest=$3, created_at=now()
            """,
            day, json.dumps(state, ensure_ascii=False), digest)
        for task_type, prefix in (("refresh_relation_daily", "relation_daily"),
                                  ("refresh_health_daily", "health_daily")):
            await c.execute(
                """
                INSERT INTO derived_task (task_type, dedupe_key, sim_day)
                VALUES ($1, $2, $3) ON CONFLICT (dedupe_key) DO NOTHING
                """,
                task_type, f"{prefix}:{finalize_day.isoformat()}", finalize_day)
    return day.isoformat()
