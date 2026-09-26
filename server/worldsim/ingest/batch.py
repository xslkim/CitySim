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
- **时间线分叉检测（R3 #2 split-brain 修复）**：主库 reseed/重置后 seq 从低位重排，撞车帧若被
  `ON CONFLICT DO NOTHING` 静默吞掉，副本将永久冻结在旧世界。现规则：① 撞车且**内容不一致**
  （同 seq 逐字段比对）→ 判定新时间线；② 帧末 sim_time 早于副本最大 sim_time 超 12h → 同理。
  判定后走重建通道：副本原始层+派生层全量重置（reset_replica）→ 当前批重放 → ack 带
  `reset:true`，本机收到后游标回退续传。内容一致的重复批仍走幂等路径，不误判。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from ..audit.alerts import alert
from ..snapshot.canonical import canonical, sha256_hex

log = logging.getLogger(__name__)

DIVERGENCE_SIM_TIME_LAG = dt.timedelta(hours=12)  # sim_time 回退超 12h = 新时间线（单向推进为不变量）

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


class TimelineDiverged(Exception):
    """时间线分叉：同 seq 撞车内容不一致，或 sim_time 系统性回退（R3 #2）。

    触发副本重建通道（reset_replica + 批重放），不得按幂等重复批吞掉。
    """

    def __init__(self, seq: int, why: str) -> None:
        super().__init__(f"时间线分叉：seq={seq}（{why}）")
        self.seq = seq
        self.why = why


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


def _event_fingerprint(ev: dict[str, Any], sim_time: dt.datetime, wall_time: dt.datetime) -> str:
    """入排行规范形指纹（撞车比对用；canonical 唯一实现，两端同形态）。"""
    return sha256_hex(canonical({
        "tick": int(ev["tick"]), "sim_time": sim_time.isoformat(),
        "wall_time": wall_time.isoformat(), "type": str(ev["type"]),
        "source": str(ev["source"]), "trigger": str(ev["trigger"]),
        "arc_id": ev.get("arc_id"), "location_id": ev.get("location_id"),
        "actors": list(ev.get("actors") or []),
        "payload": ev["payload"], "visibility": str(ev["visibility"]),
    }))


async def _insert_events(conn: Any, events: list[dict[str, Any]]) -> None:
    """单事务：events 行（ON CONFLICT 幂等 + 撞车内容比对）+ derived_task 任务行（05 §4.1）。

    撞车（同 seq 已存在）分两种：内容一致 = 幂等重复批，跳过；内容不一致 = 时间线分叉，
    抛 TimelineDiverged 交 submit 走重建通道（R3 #2，禁止 DO NOTHING 静默吞）。
    """
    for ev in events:
        validate_event(ev)
    for ev in events:
        sim_time = _parse_ts(ev["sim_time"], "sim_time")
        wall_time = _parse_ts(ev["wall_time"], "wall_time")
        inserted = await conn.fetchval(
            """
            INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, arc_id,
                                location_id, actors, rng_seed, payload, visibility, ui, schema_version)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14::jsonb,$15)
            ON CONFLICT (seq) DO NOTHING
            RETURNING seq
            """,
            int(ev["seq"]), int(ev["tick"]), sim_time, wall_time,
            str(ev["type"]), str(ev["source"]), str(ev["trigger"]), ev.get("arc_id"),
            ev.get("location_id"), list(ev.get("actors") or []),
            ev.get("rng_seed") if ev.get("rng_seed") is None else int(ev["rng_seed"]),
            json.dumps(ev["payload"], ensure_ascii=False), str(ev["visibility"]),
            json.dumps(ev["ui"], ensure_ascii=False) if ev.get("ui") else None,
            SCHEMA_VERSION)
        if inserted is None:
            stored = await conn.fetchrow(
                """
                SELECT tick, sim_time, wall_time, type, source, trigger, arc_id, location_id,
                       actors, payload, visibility
                FROM events WHERE seq=$1
                """,
                int(ev["seq"]))
            fp_new = _event_fingerprint(ev, sim_time, wall_time)
            fp_old = sha256_hex(canonical({
                "tick": int(stored["tick"]),
                "sim_time": stored["sim_time"].isoformat(),
                "wall_time": stored["wall_time"].isoformat(),
                "type": stored["type"], "source": stored["source"],
                "trigger": stored["trigger"], "arc_id": stored["arc_id"],
                "location_id": stored["location_id"],
                "actors": list(stored["actors"] or []),
                "payload": (json.loads(stored["payload"]) if isinstance(
                    stored["payload"], str) else stored["payload"]),
                "visibility": stored["visibility"],
            }))
            if fp_new != fp_old:
                raise TimelineDiverged(int(ev["seq"]), "同 seq 撞车内容不一致")
            continue  # 幂等重复批：同内容，跳过后续任务插入（dedupe_key 本也幂等）
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
                task[0], f"{task[1]}:e{int(ev['seq'])}",
                sim_time.astimezone(dt.timezone(dt.timedelta(hours=8))).date())


class BatchBuffer:
    """追平窗口内乱序缓冲（04 §9.1：窗口 8 批内由服务端缓冲排序，超窗拒绝）。

    R3 #2：submit 返回 `(acked_upto, reset)`——reset=True 表示本批触发了时间线分叉重建，
    摄入侧已重置副本并重放该批，本机须将同步游标回退到 acked_upto 续传。
    """

    def __init__(self, *, window: int = CATCHUP_WINDOW_BATCHES * CATCHUP_BATCH) -> None:
        self.window = window
        self._pending: dict[int, tuple[int, list[dict[str, Any]]]] = {}  # from_seq → (to_seq, rows)

    async def submit(self, conn: Any, from_seq: int, events: list[dict[str, Any]],
                     *, mode: str | None = None, _retried: bool = False) -> tuple[int, bool]:
        """提交一批，返回 (acked_upto, reset)。"""
        if mode == "reproject":
            async with conn.acquire() as c, c.transaction():
                await _insert_events(c, events)
            return await _max_seq(conn), False
        try:
            if await self._sim_time_regressed(conn, events):
                raise TimelineDiverged(int(events[-1]["seq"]),
                                       "帧末 sim_time 早于副本最大 sim_time")
            expected = await _max_seq(conn) + 1
            if from_seq > expected:
                if from_seq > expected + self.window:
                    raise BatchReject("乱序超窗", status=409, expected_seq=expected,
                                      reason="order")
            self._pending[from_seq] = (int(events[-1]["seq"]) if events else from_seq - 1,
                                       events)
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
        except TimelineDiverged as e:
            if _retried:
                raise
            log.error("时间线分叉检测触发（%s）→ 副本重建（R3 #2 split-brain 修复）", e)
            alert("ERROR", "replica.timeline_diverged",
                  f"摄入检测到时间线分叉（{e}），副本全量重置并重建",
                  {"seq": e.seq, "why": e.why})
            self._pending.clear()  # 旧时间线缓冲全部作废
            async with conn.acquire() as c, c.transaction():
                await reset_replica(c)
            acked, _ = await self.submit(conn, from_seq, events, mode=mode, _retried=True)
            return acked, True
        return await _max_seq(conn), False

    @staticmethod
    async def _sim_time_regressed(conn: Any, events: list[dict[str, Any]]) -> bool:
        """帧末 sim_time 比副本最大 sim_time 回退超 12h → 新时间线（正常流单向推进）。"""
        if not events:
            return False
        remote_max = await conn.fetchval("SELECT max(sim_time) FROM events")
        if remote_max is None:
            return False
        last = _parse_ts(events[-1]["sim_time"], "sim_time")
        return last < remote_max - DIVERGENCE_SIM_TIME_LAG


# 副本重建清空表清单（原始层 + 派生层 + 观测镜像；逐个 to_regclass 探测，缺表跳过）。
_RESET_TABLES = (
    "events", "memory_projection", "relation_change_log", "relation_daily", "health_daily",
    "world_state_snapshot", "world_state_latest", "ripple_edge", "derived_task", "digest_log",
    "obs.world_state_snapshot", "obs.relation_daily", "obs.ripple_edge",
)


async def reset_replica(conn: Any) -> None:
    """副本全量重置（R3 #2：世界 reseed/reset 后不残留旧 timeline 数据）。

    与 digest/reproject 同一一致性通道的重建侧：reset 后本机从低位 seq 全量重放，
    digest 比对照常兜底逐日校验。DELETE 全表（副本无本机独有数据）。
    """
    for table in _RESET_TABLES:
        if await conn.fetchval("SELECT to_regclass($1)", table) is not None:
            await conn.execute(f"DELETE FROM {table}")
    log.warning("副本已全量重置（%d 张表），等待本机全量重放", len(_RESET_TABLES))


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


async def ingest_state_latest(conn: Any, tick: Any, sim_time: Any, digest: str,
                              state: dict[str, Any]) -> None:
    """当日滚动 latest 帧（R1 #1 独立通道）：随帧 digest canonical 复核 + 单行恒等 upsert。

    与日界快照流（ingest_snapshot，sim_day 键 + d−1 定稿任务）完全分离——不排派生任务、
    不推进 sync_state.snap_upto，digest_check（T-SYN-09）事件流口径不受影响。
    """
    expect = "sha256:" + sha256_hex(canonical(state))
    if digest != expect:
        raise BatchReject(f"latest digest 不符（随帧 {digest} ≠ 复核 {expect}）", reason="digest")
    ts = _parse_ts(sim_time, "sim_time")
    async with conn.acquire() as c, c.transaction():
        await c.execute(
            """
            INSERT INTO world_state_latest (id, tick, sim_time, state, digest, updated_at)
            VALUES (1, $1, $2, $3::jsonb, $4, now())
            ON CONFLICT (id) DO UPDATE SET tick=$1, sim_time=$2, state=$3::jsonb, digest=$4,
                                           updated_at=now()
            """,
            int(tick), ts, json.dumps(state, ensure_ascii=False), digest)


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
