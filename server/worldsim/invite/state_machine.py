"""邀约协议状态机（02 T-REL-05；01 §5 全节 / 01 §3.5 willingness / 06 §1.2 social.* / 04 §4.2 送达升格）。

invite → 接受/拒绝/改期（≤2 轮）→ 约定 → 提醒/执行/爽约全状态机：

- **可达性六种情形**逐行按 01 §5.1 表：同节点直达 / 同建筑折算 10min 送达 / 异地降级 send_message /
  睡眠（0:30~6:30）作废写记忆 / 对话或约定执行中进意图队列 30min 重试 ≤2 / 背景层送达即升格当 tick 响应
  （升格写 `agent.promoted`，`trigger='system'`、`reason='event_driven'`、`caused_by` 裸 seq，04 §4.2 路径二①）。
- **改期 ≤2 轮**：每轮独立事件 `social.invite.counter`（payload `round: 1/2`、`new_time`，06 §1.2），
  轮次经 `caused_by` 链计数；第 2 轮后禁止再改期；第 1 次改期**不计拒绝**（接受率统计剔除，01 §5.2/02 D6）。
- **约定生命周期**：`social.appointment.created`（接受即落，system 发起）→ T-30min
  `social.appointment.remind` → T+15min 宽限后 `social.appointment.stood_up`（事件名逐字 06 §1.2）；
  爽约结算：被放方 affinity -8/tension +5、情绪 -10（01 §5.3，经 T-REL-01/02 通道并入聚合事件）、
  放方愧疚/无所谓记忆（按 A/N，工程口径 D20）、被放方 48h 接受先验 -0.15（willingness 注入）。
- **取消**：T-30min 前主动取消 affinity -2/tension +1；每周第 2 次取消起按爽约半价（防滥用，01 §5.3）。
  取消**无注册事件类型**（06 §1.2 未列）：M1 以单源记忆（内容约定前缀【取消】）+ 关系结算落地，
  周取消次数查记忆约定前缀（工程口径 D20；如需独立事件类型须先回登 06 §1.2）。
- invite 拒绝/爽约冷却写入接 T-REL-04（时长 01 §3.4：24h/48h）。
- **willingness 计算点**：invite 响应决策时计算意愿分（公式 01 §3.5，系数读 relations.yaml；
  供 prompt 注入与观测，不做判定），输入读数 = T-REL-02 关系缓存列 + T-REL-01 需求当前值 + 人设 Big Five。
- 约定状态一律从事件流推导（无独立日程表，04 §5.2）；全部判定只读 sim_time（00 §4 红线 11）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..adjudicator.state_events import StateAggregator
from ..memory import store
from ..relations.cooldown import CooldownEngine
from ..relations.relations import RelationEngine
from .willingness import Willingness, compute_willingness

log = logging.getLogger(__name__)

SAME_BUILDING_MOVE_MIN = 10         # 同建筑折算 10min 移动送达（01 §5.1 行 2）
BUSY_RETRY_MIN = 30                 # 对话/约定执行中 30min 后重试（01 §5.1 行 5）
BUSY_RETRY_MAX = 2                  # 至多 2 次
BUSY_DIALOGUE_WINDOW_MIN = 45       # "对话中"判定窗：chat sim_cost 上限 45min（01 §4 chat 行，D20）
SLEEP_WINDOW = ((0, 30), (6, 30))   # 睡眠 0:30~6:30（01 §5.1 行 4 / §1.4）
REMIND_BEFORE_MIN = 30              # T-30min 提醒（01 §5.3）
GRACE_MIN = 15                      # T+15min 宽限（01 §5.3）
STANDING_UP_PENALTY_HOURS = 48      # 被放方接受先验 -0.15 持续 48h（01 §5.3）
STANDING_UP_WILLINGNESS_PENALTY = -0.15
CANCEL_MEMO_PREFIX = "【取消】"      # 取消记忆内容约定前缀（无注册事件类型，D20）
MOOD_STOOD_UP_VICTIM = -10.0        # 爽约：被放方情绪 -10（01 §5.3）

REACH_SAME_NODE = "same_node"
REACH_SAME_BUILDING = "same_building"
REACH_REMOTE = "remote"
REACH_SLEEPING = "sleeping"
REACH_BUSY = "busy"


@dataclass(frozen=True)
class Reachability:
    kind: str
    detail: str = ""


@dataclass
class InviteOutcome:
    kind: str                       # delivered / degraded_message / voided / queued_retry
    event_seqs: list[int] = field(default_factory=list)
    reason: str = ""
    retry_at: dt.datetime | None = None
    promoted: bool = False


def _building_of(position: str) -> str | None:
    if position.startswith("apt."):
        return "apt"
    if position.startswith("corp."):
        return "corp"
    return None  # home.*/ext.* = 校外/外部（异地）


def is_sleeping(sim_now: dt.datetime) -> bool:
    m = sim_now.hour * 60 + sim_now.minute
    (h0, m0), (h1, m1) = SLEEP_WINDOW
    start, end = h0 * 60 + m0, h1 * 60 + m1
    return start <= m < end


class InviteMachine:
    """邀约状态机。`pool`/`gateway` 注入；`agg` 聚合器；`tick_of` = sim→tick 换算（clock 注入）。"""

    def __init__(
        self,
        pool: Any,
        gateway: Any,
        cfg_relations: dict[str, Any],
        *,
        agg: StateAggregator,
        cooldown: CooldownEngine,
        relations: RelationEngine,
        tick_of: Any = None,
        grader: Any = None,
    ) -> None:
        self._pool = pool
        self._gw = gateway
        self._cfg = cfg_relations
        self._agg = agg
        self._cooldown = cooldown
        self._relations = relations
        self._tick_of = tick_of or (lambda _sim: 0)
        self._grader = grader  # T-ADJ-07：grade 初值唯一实现挂接（None = 不打分）

    # ---- 可达性六种情形（01 §5.1 逐行；睡眠/对话中优先于位置判定） ----------------------

    async def reach(self, *, a_id: str, b_id: str, sim_now: dt.datetime) -> Reachability:
        if is_sleeping(sim_now):
            return Reachability(REACH_SLEEPING, "0:30~6:30 睡眠中")
        if await self._is_busy(b_id, sim_now):
            return Reachability(REACH_BUSY, "对话或约定执行中")
        row = await self._pool.fetchrow("SELECT position FROM agents WHERE id=$1", a_id)
        b = await self._pool.fetchrow("SELECT position, cognition_tier FROM agents WHERE id=$1", b_id)
        if row is None or b is None:
            raise KeyError(f"agent 不存在：{a_id if row is None else b_id}")
        if b["position"] == row["position"]:
            return Reachability(REACH_SAME_NODE, "同节点当面邀约")
        if _building_of(b["position"]) is not None and _building_of(b["position"]) == _building_of(row["position"]):
            return Reachability(REACH_SAME_BUILDING, f"同建筑折算 {SAME_BUILDING_MOVE_MIN}min 送达")
        return Reachability(REACH_REMOTE, "异地/外部场所")

    async def _is_busy(self, b_id: str, sim_now: dt.datetime) -> bool:
        """对话/约定执行中（M1 工程口径 D20）：45min 内有其 dialogue.chat，
        或其约定处于执行窗（at_sim-0~+15min）。"""
        return bool(
            await self._pool.fetchval(
                """
                SELECT count(*) FROM events
                WHERE type='dialogue.chat' AND $1 = ANY(actors)
                  AND sim_time > $2::timestamptz - ($3 || ' minutes')::interval
                """,
                b_id, sim_now, str(BUSY_DIALOGUE_WINDOW_MIN),
            )
        ) or bool(await self._appointments(b_id, sim_now, execution_window=True))

    async def _appointments(self, agent_id: str, sim_now: dt.datetime, *, execution_window: bool = False) -> list[Any]:
        if execution_window:
            return await self._pool.fetch(
                """
                SELECT seq, payload FROM events
                WHERE type='social.appointment.created' AND $1 = ANY(actors)
                  AND payload->>'at_sim' <= $2::text
                  AND (payload->>'at_sim')::timestamptz > $2::timestamptz - ($3 || ' minutes')::interval
                """,
                agent_id, sim_now.isoformat(), str(GRACE_MIN),
            )
        return await self._pool.fetch(
            "SELECT seq, payload FROM events WHERE type='social.appointment.created' AND $1 = ANY(actors) ORDER BY seq",
            agent_id,
        )

    # ---- 发出（可达性分流；背景层送达即升格当 tick，04 §4.2 路径二①） ---------------------

    async def send(
        self, *, a_id: str, b_id: str, activity: str, at_sim: dt.datetime, location: str,
        sim_now: dt.datetime, tick: int, rng_seed: int, retry_count: int = 0,
    ) -> InviteOutcome:
        reach = await self.reach(a_id=a_id, b_id=b_id, sim_now=sim_now)
        if reach.kind == REACH_SLEEPING:
            await store.insert_memory(
                self._pool, self._gw, agent_id=a_id, sim_time=sim_now, kind="event",
                content=f"想约{await self._name(b_id)}{activity}，但对方在睡，没能发出", importance=3, rng_seed=rng_seed,
            )
            return InviteOutcome("voided", reason=reach.detail)
        if reach.kind == REACH_BUSY:
            if retry_count < BUSY_RETRY_MAX:
                return InviteOutcome(
                    "queued_retry", reason=reach.detail,
                    retry_at=sim_now + dt.timedelta(minutes=BUSY_RETRY_MIN),
                )
            await store.insert_memory(
                self._pool, self._gw, agent_id=a_id, sim_time=sim_now, kind="event",
                content=f"想约{await self._name(b_id)}{activity}，重试 {BUSY_RETRY_MAX} 次都没碰上", importance=3, rng_seed=rng_seed,
            )
            return InviteOutcome("voided", reason=f"{reach.detail}，重试 {BUSY_RETRY_MAX} 次用尽")
        if reach.kind == REACH_REMOTE:
            seq = await self._insert_social(
                "social.send_message", tick, sim_now, f"agent:{a_id}", "autonomous", [a_id, b_id], rng_seed,
                {"from": a_id, "to": b_id, "content_hint": f"想约你{activity}（{at_sim.isoformat()}，{location}）"},
            )
            await store.write_event_memories(
                self._pool, self._gw, event_seq=seq, sim_time=sim_now,
                perspectives={
                    a_id: f"异地约不上，给{await self._name(b_id)}捎了话：{activity}",
                    b_id: f"{await self._name(a_id)}捎话来：想约你{activity}",
                },
                importance=4, rng_seed=rng_seed,
            )
            return InviteOutcome("degraded_message", [seq], reach.detail)
        seq = await self._insert_social(
            "social.invite", tick, sim_now, f"agent:{a_id}", "autonomous", [a_id, b_id], rng_seed,
            {"from": a_id, "to": b_id, "activity": activity, "time": at_sim.isoformat(), "location": location},
        )
        outcome = InviteOutcome("delivered", [seq], reach.detail)
        b_tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", b_id)
        if b_tier == "background":
            outcome.event_seqs.append(await self._promote_background(b_id, sim_now, tick, rng_seed, cause_seq=seq))
            outcome.promoted = True
        return outcome

    async def _promote_background(self, b_id: str, sim_now: dt.datetime, tick: int, rng_seed: int, *, cause_seq: int) -> int:
        """背景层送达即升格（当 tick）：background → secondary + `agent.promoted`（04 §4.2 路径二①）。"""
        await self._pool.execute("UPDATE agents SET cognition_tier='secondary' WHERE id=$1 AND cognition_tier='background'", b_id)
        return await self._insert_social(
            "agent.promoted", tick, sim_now, "system", "system", [b_id], rng_seed,
            {"from_tier": "background", "to_tier": "secondary", "reason": "event_driven", "caused_by": store.caused_by(cause_seq)},
        )

    # ---- 响应（接受/拒绝/改期；willingness 计算点 = 响应决策时，观测不做判定） ---------------

    async def respond(
        self, *, invite_seq: int, action: str, sim_now: dt.datetime, tick: int, rng_seed: int,
        new_time: dt.datetime | None = None, politeness: int = 0, activity_match: float = 0.0,
    ) -> dict[str, Any]:
        invite = await self._pool.fetchrow("SELECT seq, payload, actors FROM events WHERE seq=$1 AND type='social.invite'", invite_seq)
        if invite is None:
            raise KeyError(f"invite 事件 {invite_seq} 不存在")
        payload = invite["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        a_id, b_id = payload["from"], payload["to"]
        willing = await self.willingness_now(a_id=a_id, b_id=b_id, sim_now=sim_now, activity_match=activity_match)
        out: dict[str, Any] = {"action": action, "willingness": willing, "event_seqs": []}
        if action == "counter":
            round_no = await self.counter_round(invite_seq) + 1
            if round_no > 2:
                raise ValueError("改期限 2 轮：第 2 轮后禁止再改期，必须接受/拒绝（01 §5.2）")
            if new_time is None:
                raise ValueError("counter 需要 new_time")
            seq = await self._insert_social(
                "social.invite.counter", tick, sim_now, f"agent:{b_id}", "autonomous", [a_id, b_id], rng_seed,
                {"from": b_id, "to": a_id, "round": round_no, "new_time": new_time.isoformat(),
                 "caused_by": store.caused_by(invite_seq)},
            )
            out["event_seqs"].append(seq)
            out["round"] = round_no
            out["counts_as_refusal"] = round_no > 1  # 第 1 次改期不计拒绝（01 §5.2）
            return out
        if action == "accept":
            seq = await self._insert_social(
                "social.appointment.created", tick, sim_now, "system", "system", sorted([a_id, b_id]), rng_seed,
                {"participants": sorted([a_id, b_id]), "activity": payload["activity"], "at_sim": payload["time"]},
            )
            await self._relations.settle(
                self._agg, kind="invite_accepted", a_id=a_id, b_id=b_id, cause=store.caused_by(seq),
            )
            await store.write_event_memories(
                self._pool, self._gw, event_seq=seq, sim_time=sim_now,
                perspectives={
                    a_id: f"{await self._name(b_id)}答应了：{payload['activity']}（{payload['time']}）",
                    b_id: f"我答应了{await self._name(a_id)}：{payload['activity']}（{payload['time']}）",
                },
                importance=6, rng_seed=rng_seed,
            )
            out["event_seqs"].append(seq)
            out["appointment_seq"] = seq
            return out
        if action == "refuse":
            seq = await self._insert_social(
                "social.refuse", tick, sim_now, f"agent:{b_id}", "autonomous", [a_id, b_id], rng_seed,
                {"from": b_id, "to": a_id, "request_ref": store.caused_by(invite_seq), "politeness": int(politeness)},
            )
            row = self._cfg["matrix"]["invite_refused"]
            factor = float(row.get("polite_factor", 1.0)) if politeness else 1.0  # 礼貌拒绝减半（01 §3.2）
            await self._relations.settle(
                self._agg, kind="invite_refused", a_id=a_id, b_id=b_id, cause=store.caused_by(seq), factor=factor,
            )
            until = await self._cooldown.write_cooldown(
                agent_id=a_id, trigger_kind="invite_refused", target=b_id, sim_now=sim_now,
            )
            await store.write_event_memories(
                self._pool, self._gw, event_seq=seq, sim_time=sim_now,
                perspectives={
                    a_id: f"{await self._name(b_id)}没答应我的邀约（{payload['activity']}）",
                    b_id: f"我婉拒了{await self._name(a_id)}的邀约（{payload['activity']}）" if politeness else f"我拒绝了{await self._name(a_id)}的邀约（{payload['activity']}）",
                },
                importance=4, rng_seed=rng_seed,
            )
            out["event_seqs"].append(seq)
            out["cooldown_until"] = until
            return out
        raise ValueError(f"未知响应动作 {action!r}（accept/refuse/counter）")

    async def counter_round(self, invite_seq: int) -> int:
        """链上已有改期轮数（`caused_by` 链计数，01 §5.2 限 2 轮）。"""
        return await self._pool.fetchval(
            "SELECT count(*) FROM events WHERE type='social.invite.counter' AND payload->>'caused_by'=$1",
            store.caused_by(invite_seq),
        )

    # ---- willingness 计算点（invite 送达/响应决策时；供 prompt 注入与观测，不做判定） --------

    async def willingness_now(self, *, a_id: str, b_id: str, sim_now: dt.datetime, activity_match: float = 0.0) -> Willingness:
        """B 对 A 邀约的意愿分：关系缓存列（B→A affinity/tension）+ B 社交/情绪 + Big Five + 场合 +
        活动偏好匹配 + 被放鸽 48h 先验惩罚（01 §5.3）。"""
        rel = await self._pool.fetchrow("SELECT affinity, tension FROM relations WHERE a_id=$1 AND b_id=$2", b_id, a_id)
        brow = await self._pool.fetchrow("SELECT needs, persona FROM agents WHERE id=$1", b_id)
        needs = brow["needs"]
        if isinstance(needs, str):
            needs = json.loads(needs)
        persona = brow["persona"]
        if isinstance(persona, str):
            persona = json.loads(persona)
        penalty = await self.stood_up_prior_penalty(inviter=a_id, invitee=b_id, sim_now=sim_now)
        return compute_willingness(
            self._cfg["willingness"],
            affinity=float(rel["affinity"]) if rel else 0.0,
            tension=float(rel["tension"]) if rel else 0.0,
            social_need=float((needs or {}).get("social", 50)),
            mood=float((needs or {}).get("mood", 50)),
            big_five=(persona or {}).get("big_five"),
            sim_now=sim_now,
            activity_match=activity_match,
            prior_penalty=penalty,
        )

    async def stood_up_prior_penalty(self, *, inviter: str, invitee: str, sim_now: dt.datetime) -> float:
        """inviter 过去 48h 放过 invitee 鸽子 → 接受先验 -0.15（01 §5.3；经爽约关系结算链判定）。"""
        hit = await self._pool.fetchval(
            """
            WITH su AS (
              SELECT seq FROM events
              WHERE type='social.appointment.stood_up' AND sim_time > $3::timestamptz - interval '48 hours'
            )
            SELECT count(*) FROM events e, LATERAL jsonb_array_elements(e.payload->'changes') c
            WHERE e.type='relation.changed'
              AND c->>'a_id' = $2 AND c->>'b_id' = $1
              AND c->>'cause' IN (SELECT seq::text FROM su)
            """,
            inviter, invitee, sim_now,
        )
        return STANDING_UP_WILLINGNESS_PENALTY if hit else 0.0

    # ---- 提醒 / 爽约（调度驱动） ---------------------------------------------------

    async def due_reminders(
        self, *, sim_now: dt.datetime, tick: int, rng_seed: int, participants: list[str] | None = None,
    ) -> list[int]:
        """T-30min：到点未提醒的约定落 `social.appointment.remind`（每条约定恰一次）。

        已取消约定（取消记忆经 source_event_seq 回链，D20）不再提醒；`participants` 可按约定双方过滤。
        """
        rows = await self._pool.fetch(
            """
            SELECT seq, actors, payload FROM events c
            WHERE c.type='social.appointment.created'
              AND (c.payload->>'at_sim')::timestamptz - ($1 || ' minutes')::interval <= $2::timestamptz
              AND (c.payload->>'at_sim')::timestamptz > $2::timestamptz
              AND ($3::text[] IS NULL OR c.actors @> $3::text[])
              AND NOT EXISTS (
                SELECT 1 FROM events r
                WHERE r.type='social.appointment.remind'
                  AND r.payload->>'at_sim' = c.payload->>'at_sim'
                  AND r.payload->'participants' = c.payload->'participants'
              )
              AND NOT EXISTS (
                SELECT 1 FROM memories x
                WHERE x.kind='event' AND x.source_event_seq = c.seq AND x.content LIKE '【取消】%'
              )
            ORDER BY c.seq
            """,
            str(REMIND_BEFORE_MIN), sim_now, participants,
        )
        seqs: list[int] = []
        for r in rows:
            payload = r["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            seqs.append(
                await self._insert_social(
                    "social.appointment.remind", tick, sim_now, "system", "system", r["actors"], rng_seed,
                    {"participants": payload["participants"], "activity": payload["activity"], "at_sim": payload["at_sim"]},
                )
            )
        return seqs

    async def due_stood_ups(
        self, *, sim_now: dt.datetime, tick: int, rng_seed: int, participants: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """T+15min 宽限后未执行 → `social.appointment.stood_up` + 爽约结算（01 §5.3）。

        执行判定（M1 工程口径 D20）：执行窗 [at_sim, at_sim+15min) 内存在恰含一方的事件 →
        另一方为放方；双方都无 → 放方缺省取 participants 序首（确定性）；含双方事件 → 视为已执行。
        已取消约定（取消记忆经 source_event_seq 回链）不再判爽约；`participants` 可按约定双方过滤。
        """
        rows = await self._pool.fetch(
            """
            SELECT seq, actors, payload FROM events c
            WHERE c.type='social.appointment.created'
              AND (c.payload->>'at_sim')::timestamptz + ($1 || ' minutes')::interval <= $2::timestamptz
              AND ($3::text[] IS NULL OR c.actors @> $3::text[])
              AND NOT EXISTS (
                SELECT 1 FROM events s
                WHERE s.type='social.appointment.stood_up'
                  AND s.payload->>'at_sim' = c.payload->>'at_sim'
                  AND s.payload->'participants' = c.payload->'participants'
              )
              AND NOT EXISTS (
                SELECT 1 FROM memories x
                WHERE x.kind='event' AND x.source_event_seq = c.seq AND x.content LIKE '【取消】%'
              )
            ORDER BY c.seq
            """,
            str(GRACE_MIN), sim_now, participants,
        )
        outs: list[dict[str, Any]] = []
        for r in rows:
            payload = r["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            participants = list(payload["participants"])
            at_sim = dt.datetime.fromisoformat(payload["at_sim"])
            no_show = await self._detect_no_show(participants, at_sim)
            if no_show is None:
                continue  # 双方有共同活动事件 → 已执行
            victim = participants[0] if participants[1] == no_show else participants[1]
            seq = await self._insert_social(
                "social.appointment.stood_up", tick, sim_now, "system", "system", participants, rng_seed,
                {"participants": participants, "activity": payload["activity"], "at_sim": payload["at_sim"]},
            )
            cause = store.caused_by(seq)
            await self._relations.settle(
                self._agg, kind="stood_up", a_id=victim, b_id=no_show, cause=cause,
            )
            await self._agg.apply_needs_delta(agent_id=victim, need="mood", delta=MOOD_STOOD_UP_VICTIM, cause=cause)
            await self._cooldown.write_cooldown(
                agent_id=victim, trigger_kind="invite_stood_up", target=no_show, sim_now=sim_now,
            )
            persona = await self._pool.fetchval("SELECT persona FROM agents WHERE id=$1", no_show)
            if isinstance(persona, str):
                persona = json.loads(persona)
            bf = (persona or {}).get("big_five") or {}
            guilty = float(bf.get("agreeableness", 0)) >= 70 or float(bf.get("neuroticism", 0)) >= 70  # 按 A/N（D20）
            await store.insert_memory(
                self._pool, self._gw, agent_id=no_show, sim_time=sim_now, kind="event",
                content=(f"我放了{await self._name(victim)}鸽子（{payload['activity']}），有点过意不去" if guilty
                         else f"我放了{await self._name(victim)}鸽子（{payload['activity']}），无所谓"),
                importance=5, rng_seed=rng_seed,
            )
            await store.insert_memory(
                self._pool, self._gw, agent_id=victim, sim_time=sim_now, kind="event",
                content=f"{await self._name(no_show)}放了我鸽子（{payload['activity']}）",
                importance=6, rng_seed=rng_seed,
            )
            outs.append({"appointment_seq": r["seq"], "stood_up_seq": seq, "no_show": no_show, "victim": victim})
        return outs

    async def _detect_no_show(self, participants: list[str], at_sim: dt.datetime) -> str | None:
        """执行窗内单方活动事件判定放方；双方共同事件 = 已执行返回 None；无事件缺省序首（D20）。"""
        rows = await self._pool.fetch(
            """
            SELECT actors FROM events
            WHERE sim_time >= $1 AND sim_time < $1::timestamptz + ($2 || ' minutes')::interval
              AND type NOT LIKE 'social.appointment.%' AND type NOT LIKE 'state.%' AND type NOT LIKE 'relation.%'
              AND actors && $3::text[]
            """,
            at_sim, str(GRACE_MIN), participants,
        )
        seen: set[str] = set()
        for r in rows:
            acts = list(r["actors"])
            if all(p in acts for p in participants):
                return None  # 共同活动事件 → 已执行
            seen.update(p for p in acts if p in participants)
        if len(seen) == 1:
            (showed,) = seen
            return participants[0] if participants[1] == showed else participants[1]
        return participants[0]

    # ---- 取消（T-30min 前；每周第 2 次起按爽约半价，01 §5.3） -----------------------------

    async def cancel(
        self, *, appointment_seq: int, by: str, sim_now: dt.datetime, tick: int, rng_seed: int,
    ) -> dict[str, Any]:
        appt = await self._pool.fetchrow(
            "SELECT seq, payload FROM events WHERE seq=$1 AND type='social.appointment.created'", appointment_seq,
        )
        if appt is None:
            raise KeyError(f"约定 {appointment_seq} 不存在")
        payload = appt["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        at_sim = dt.datetime.fromisoformat(payload["at_sim"])
        if sim_now > at_sim - dt.timedelta(minutes=REMIND_BEFORE_MIN):
            raise ValueError("取消须在 T-30min 前（01 §5.3）")
        participants = list(payload["participants"])
        if by not in participants:
            raise ValueError("仅约定参与方可取消")
        other = participants[0] if participants[1] == by else participants[1]
        weekly = await self.cancels_this_week(by, sim_now)
        half_price = weekly >= 1  # 每周第 2 次取消起按爽约半价（防滥用）
        cause = store.caused_by(appointment_seq)
        if half_price:
            row = self._cfg["matrix"]["stood_up"]
            await self._agg.apply_relation_delta(
                a_id=other, b_id=by,
                delta_affinity=int(row["delta_affinity"] / 2), delta_tension=int(row["delta_tension"] / 2), cause=cause,
            )
            await self._agg.apply_needs_delta(agent_id=other, need="mood", delta=MOOD_STOOD_UP_VICTIM / 2, cause=cause)
        else:
            await self._agg.apply_relation_delta(a_id=other, b_id=by, delta_affinity=-2, delta_tension=1, cause=cause)
        await store.insert_memory(
            self._pool, self._gw, agent_id=by, sim_time=sim_now, kind="event",
            content=f"{CANCEL_MEMO_PREFIX}我取消了和{await self._name(other)}的约定（{payload['activity']}）",
            importance=3, rng_seed=rng_seed, source_event_seq=appointment_seq,
        )
        await store.insert_memory(
            self._pool, self._gw, agent_id=other, sim_time=sim_now, kind="event",
            content=f"{CANCEL_MEMO_PREFIX}{await self._name(by)}取消了和你的约定（{payload['activity']}）",
            importance=4, rng_seed=rng_seed, source_event_seq=appointment_seq,
        )
        return {"cancelled": appointment_seq, "half_price": half_price, "weekly_count": weekly + 1}

    async def cancels_this_week(self, agent_id: str, sim_now: dt.datetime) -> int:
        """本周已取消次数（记忆约定前缀计数；无注册事件类型，D20）。"""
        week_start = (sim_now - dt.timedelta(days=sim_now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._pool.fetchval(
            """
            SELECT count(*) FROM memories
            WHERE agent_id=$1 AND kind='event' AND sim_time >= $2 AND content LIKE $3
            """,
            agent_id, week_start, f"{CANCEL_MEMO_PREFIX}我取消了%",
        )

    # ---- 内部 -------------------------------------------------------------------

    async def _name(self, agent_id: str) -> str:
        return await self._pool.fetchval("SELECT name FROM agents WHERE id=$1", agent_id) or agent_id

    async def _insert_social(
        self, type_: str, tick: int, sim_now: dt.datetime, source: str, trigger: str,
        actors: list[str], rng_seed: int, payload: dict[str, Any],
    ) -> int:
        ui: dict[str, Any] | None = None
        if self._grader is not None:
            ui = {"grade": await self._grader.grade(
                type_=type_, actors=actors, payload=payload, sim_now=sim_now, followups=True,
            )}  # T-ADJ-07（04 §6.6；邀约链事件恒有后续——送达/约定/提醒，R4 命中）
        return await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, rng_seed, visibility, payload, ui)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'public', $8::jsonb, $9::jsonb)
            RETURNING seq
            """,
            tick, sim_now, type_, source, trigger, actors, rng_seed,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(ui, ensure_ascii=False) if ui else None,
        )
