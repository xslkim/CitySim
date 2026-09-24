"""明星层轮换与事件驱动即时升格（02 T-LOD-02 路径二段 / T-LOD-03 路径一段；04 §4.2/§4.3、06 §1.2）。

**路径二：事件驱动即时升格（本模块 `EventDrivenLOD`，不受迟滞约束、不占每日 churn 名额）**：
- 触发集（裁决协程内当 tick 生效）：①成为他人动作对象（动作清单见 04 §4.2 路径二①）；
  ②成为 `visibility='public'` 或 A 级候选事件的非发起参与者/直接目击；③编剧点名（M1 接口
  `nominate`，M3 接 L1/L2）。升格目标层：①② background→secondary；③直达 star（04 §4.2）。
- 落库：当 tick 写 `agent.promoted`（`trigger='system'`，`payload.reason='event_driven'/
  'director_nominated'`，`payload.caused_by` = 裸 seq 数字字符串，00 §4 红线 3），本 tick 即按新层响应。
- 回落：事件驱动升入 secondary 者连续 2 模拟日零新交互 → `agent.demoted`（`reason='cooldown'`）。
- 硬上限：secondary ≤16 人；超额按 LRU（键 = 最近一次出现在事件 `actors` 的 sim_time，
  调度器随事件流维护）当 tick 挤出回 background，写 `agent.demoted`（`reason='lru_evict'`）。

数值载体：`config/models.yaml` `thresholds.lod` 段（00 §7 DoD 4/6；16 人上限/2 模拟日回落为
04 §4.2 镜像）。目标提取口径（工程默认，02 文档偏差表登记）：动作对象 = payload `to`/`target` 键，
或 `participants` 减发起者（source='agent:<id>'）；直接目击 = dialogue 域 `witnesses[]`。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_AGENT_RE = re.compile(r"^A(0[1-9]|[1-3][0-9]|40)$")  # agent id TEXT 形态（00 §4 红线 1）

# 04 §4.2 路径二① 触发动作清单（成为其 target 即升格）
PROMOTE_ACTION_TYPES = (
    "dialogue.chat", "social.invite", "dialogue.argue", "social.give_gift", "social.help",
    "social.borrow_money", "social.repay_money", "dialogue.confess", "dialogue.apologize",
)


def extract_targets(event: dict[str, Any]) -> list[str]:
    """从事件行提取"非发起参与者/动作对象/直接目击"（确定性序：id 升序）。

    发起者 = source 'agent:<id>'；对象 = payload.to / payload.target / participants 减发起者；
    直接目击 = payload.witnesses[]（dialogue 域注册键，06 §1.2）。
    """
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    source = str(event.get("source") or "")
    initiator = source.removeprefix("agent:") if source.startswith("agent:") else None
    targets: set[str] = set()
    for key in ("to", "target"):
        v = payload.get(key)
        if isinstance(v, str) and _AGENT_RE.fullmatch(v) and v != initiator:
            targets.add(v)
    for v in payload.get("participants") or []:
        if isinstance(v, str) and v != initiator:
            targets.add(v)
    if event.get("visibility") == "public" or (event.get("ui") or {}).get("grade") == "A":
        for v in payload.get("witnesses") or []:
            if isinstance(v, str) and v != initiator:
                targets.add(v)
    return sorted(targets)


class EventDrivenLOD:
    """事件驱动升格/挤出/回落。`pool` = asyncpg pool；`cfg` = models.yaml thresholds.lod 段。"""

    def __init__(self, pool: Any, cfg: dict[str, Any] | None = None) -> None:
        self._pool = pool
        cfg = cfg or {}
        self._cap = int(cfg.get("secondary_cap", 16))
        self._cooldown_days = int(cfg.get("cooldown_demote_sim_days", 2))

    # ---- 升格（当 tick 生效） -------------------------------------------------

    async def on_event(self, *, tick: int, sim_now: dt.datetime, event: dict[str, Any]) -> list[int]:
        """事件落库后调用：命中触发集的背景层 agent 当 tick 升格 secondary。返回新事件 seq 列表。"""
        if event.get("type") not in PROMOTE_ACTION_TYPES and event.get("visibility") != "public":
            return []
        seqs: list[int] = []
        for target in extract_targets(event):
            tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", target)
            if tier != "background":
                continue  # 已在 secondary/star 不动作（04 §4.2）
            seqs.append(
                await self._promote(target, to_tier="secondary", reason="event_driven",
                                    caused_by=str(event["seq"]), tick=tick, sim_now=sim_now)
            )
        if seqs:
            seqs.extend(await self.enforce_cap(tick=tick, sim_now=sim_now, caused_by=str(event["seq"])))
        return seqs

    async def nominate(self, *, agent_id: str, tick: int, sim_now: dt.datetime, caused_by: str | None = None) -> int | None:
        """编剧点名：直达 star（04 §4.2 路径二③；M1 留接口，M3 接 L1/L2）。已升/在星返回 None。"""
        tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", agent_id)
        if tier == "star":
            return None
        return await self._promote(agent_id, to_tier="star", reason="director_nominated",
                                   caused_by=caused_by, tick=tick, sim_now=sim_now)

    async def _promote(
        self, agent_id: str, *, to_tier: str, reason: str, caused_by: str | None, tick: int, sim_now: dt.datetime,
    ) -> int:
        from_tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", agent_id)
        await self._pool.execute("UPDATE agents SET cognition_tier=$2 WHERE id=$1", agent_id, to_tier)
        payload: dict[str, Any] = {"from_tier": from_tier, "to_tier": to_tier, "reason": reason}
        if caused_by is not None:
            if not caused_by.isdigit():
                raise ValueError(f"caused_by 必须为裸 seq 数字字符串，得到 {caused_by!r}（00 §4 红线 3）")
            payload["caused_by"] = caused_by
        seq = await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES ($1, $2, 'agent.promoted', 'system', 'system', $3, 'public', $4::jsonb)
            RETURNING seq
            """,
            tick, sim_now, [agent_id], json.dumps(payload, ensure_ascii=False),
        )
        log.info("升格：%s %s → %s（%s）", agent_id, from_tier, to_tier, reason)
        return int(seq)

    # ---- 硬上限 + LRU 挤出（当 tick 生效） ---------------------------------------

    async def enforce_cap(self, *, tick: int, sim_now: dt.datetime, caused_by: str | None = None) -> list[int]:
        """secondary 超员按 LRU 挤出回 background（`reason='lru_evict'`，04 §4.2 N-P1-7）。

        LRU 键 = 该 agent 最近一次出现在事件 `actors` 的 sim_time（调度器随事件流维护；
        从未出现 = 最久，NULLS FIRST）。同键并列按 id 升序（确定性）。
        """
        seqs: list[int] = []
        while True:
            count = await self._pool.fetchval("SELECT count(*) FROM agents WHERE cognition_tier='secondary'")
            if count <= self._cap:
                return seqs
            row = await self._pool.fetchrow(
                """
                SELECT a.id FROM agents a
                LEFT JOIN LATERAL (
                  SELECT max(e.sim_time) AS last_seen FROM events e WHERE a.id = ANY(e.actors)
                ) s ON true
                WHERE a.cognition_tier='secondary'
                ORDER BY s.last_seen ASC NULLS FIRST, a.id
                LIMIT 1
                """,
            )
            if row is None:
                return seqs
            seqs.append(await self._demote(row["id"], to_tier="background", reason="lru_evict",
                                           caused_by=caused_by, tick=tick, sim_now=sim_now))

    # ---- 回落（连续 2 模拟日零新交互；日界/周界批量驱动） -------------------------

    async def demote_inactive(self, *, tick: int, sim_now: dt.datetime) -> list[int]:
        """事件驱动升入 secondary 且连续 2 模拟日零新交互者回落 background（`reason='cooldown'`）。

        "事件驱动升入"判定 = 最近一次 `agent.promoted` 的 payload.reason='event_driven'（事件流推导，
        可回放）；零新交互 = 最近出现于事件 actors 的 sim_time < sim_now − 2 模拟日（含从未出现）。
        """
        rows = await self._pool.fetch(
            """
            SELECT a.id, s.last_seen FROM agents a
            LEFT JOIN LATERAL (
              SELECT max(e.sim_time) AS last_seen FROM events e WHERE a.id = ANY(e.actors)
            ) s ON true
            WHERE a.cognition_tier='secondary'
            ORDER BY a.id
            """,
        )
        cutoff = sim_now - dt.timedelta(days=self._cooldown_days)
        seqs: list[int] = []
        for r in rows:
            if r["last_seen"] is not None and r["last_seen"] >= cutoff:
                continue
            last_reason = await self._pool.fetchval(
                """
                SELECT payload->>'reason' FROM events
                WHERE type='agent.promoted' AND $1 = ANY(actors) ORDER BY seq DESC LIMIT 1
                """,
                r["id"],
            )
            if last_reason != "event_driven":
                continue
            seqs.append(await self._demote(r["id"], to_tier="background", reason="cooldown",
                                           caused_by=None, tick=tick, sim_now=sim_now))
        return seqs

    async def _demote(
        self, agent_id: str, *, to_tier: str, reason: str, caused_by: str | None, tick: int, sim_now: dt.datetime,
    ) -> int:
        from_tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", agent_id)
        await self._pool.execute("UPDATE agents SET cognition_tier=$2 WHERE id=$1", agent_id, to_tier)
        payload: dict[str, Any] = {"from_tier": from_tier, "to_tier": to_tier, "reason": reason}
        if caused_by is not None:
            if not caused_by.isdigit():
                raise ValueError(f"caused_by 必须为裸 seq 数字字符串，得到 {caused_by!r}（00 §4 红线 3）")
            payload["caused_by"] = caused_by
        seq = await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES ($1, $2, 'agent.demoted', 'system', 'system', $3, 'public', $4::jsonb)
            RETURNING seq
            """,
            tick, sim_now, [agent_id], json.dumps(payload, ensure_ascii=False),
        )
        log.info("降格：%s %s → %s（%s）", agent_id, from_tier, to_tier, reason)
        return int(seq)
