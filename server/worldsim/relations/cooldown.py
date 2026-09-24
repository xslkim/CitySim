"""意图冷却与 argue 二道阻尼（02 T-REL-04；01 §3.4 / 04 §5.2 `intent_cooldown` 表 / 04 §6.2 通用校验冷却行）。

- `intent_key` = 主体+对象+动作三元组（04 §5.2 注释形如 `invite:A07:dinner`）：本模块落库键
  = `f"{action}:{target}"`（冷却语义对人不对活动；detail 段预留给需细粒度键的调用方，工程默认 D17）。
- 冷却时长表唯一持有方 = 01 §3.4（`config/relations.yaml` `cooldown_hours` 镜像），逐行实现；
  触发源 → 被冷却动作集映射见 `TRIGGER_MAP`（如 argue_reapproach = 主动再找同一人：chat/invite/argue/gossip）。
- 命中冷却（`until_sim > now_sim()`）→ 校验器拦截：`check_action` 为 T-ADJ-03（波次 2b）通用规则消费面。
- argue 阻尼二道（01 §3.4 表下注，P1 档，**常时生效、与冷却并行**）：tension ≥70 → 该对 argue 意图权重 ×0.5。
- refuse 连续 3 次（01 §4.1 refuse 段）：A 对该人 invite/chat 意图权重 ×0.5；streak 查事件流
  （最近一次被接受交互之后累计的 refuse 数，可回放）。
- 迁怒权重（01 §3.3 迁怒行，T-REL-03 写入）：marker 存 `agents.mood` JSON `grudges` 键
  （与 T-MEM-02 importance_acc 同例，工程默认 D17），argue/gossip 意图权重 ×3、48 模拟小时后自然失效。
- 全部判定只读 sim_time（00 §4 红线 11）。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

# 触发源 → (被冷却动作集, relations.yaml cooldown_hours 键)（01 §3.4 逐行）
TRIGGER_MAP: dict[str, tuple[tuple[str, ...], str]] = {
    "invite_refused": (("invite",), "invite_refused"),
    "invite_stood_up": (("invite",), "invite_stood_up"),
    "chat_perfunctory": (("chat",), "chat_perfunctory"),
    "argue_reapproach": (("chat", "invite", "argue", "gossip"), "argue_reapproach"),
    "gossip_same_target": (("gossip",), "gossip_same_target"),
    "confess_rejected": (("confess",), "confess_rejected"),
    "borrow_refused": (("borrow_money",), "borrow_refused"),
    "apologize_refused": (("apologize",), "apologize_refused"),
    "give_gift_same_target": (("give_gift",), "give_gift_same_target"),
    "raise_rejected": (("raise",), "raise_rejected"),  # P2 行；19 动作无 raise，映射键预留
}


class CooldownEngine:
    """意图冷却/权重引擎。`pool` = asyncpg pool；`cfg` = relations.yaml 字典。"""

    def __init__(self, pool: Any, cfg: dict[str, Any]) -> None:
        self._pool = pool
        self._hours = cfg["cooldown_hours"]
        self._damping = cfg["argue_damping"]
        self._streak = cfg["refuse_streak"]
        self._grudge = cfg["grudge"]

    # ---- 键与时长 ---------------------------------------------------------------

    @staticmethod
    def make_intent_key(action: str, target: str, detail: str = "") -> str:
        """三元组键（04 §5.2 注释形态）：`invite:A07` 或带 detail `invite:A07:dinner`。"""
        key = f"{action}:{target}"
        return f"{key}:{detail}" if detail else key

    def duration_hours(self, trigger_kind: str) -> float:
        if trigger_kind not in TRIGGER_MAP:
            raise KeyError(f"未知冷却触发源 {trigger_kind!r}（01 §3.4 行集 = {sorted(TRIGGER_MAP)}）")
        return float(self._hours[TRIGGER_MAP[trigger_kind][1]])

    # ---- 写入（触发源逐行按 01 §3.4） ----------------------------------------------

    async def write_cooldown(
        self, *, agent_id: str, trigger_kind: str, target: str, sim_now: dt.datetime, reason: str = "",
    ) -> dt.datetime:
        """按触发源写冷却（被冷却动作集每动作一行），返回 until_sim。"""
        actions, _ = TRIGGER_MAP[trigger_kind]  # KeyError 先于写库（未知触发源）
        until = sim_now + dt.timedelta(hours=self.duration_hours(trigger_kind))
        for action in actions:
            await self._pool.execute(
                """
                INSERT INTO intent_cooldown (agent_id, intent_key, until_sim, reason)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (agent_id, intent_key) DO UPDATE SET until_sim=$3, reason=$4
                """,
                agent_id, self.make_intent_key(action, target), until, reason or trigger_kind,
            )
        return until

    # ---- 判定（T-ADJ-03 通用规则消费面；只读 sim_time） -----------------------------

    async def until(self, agent_id: str, intent_key: str) -> dt.datetime | None:
        return await self._pool.fetchval(
            "SELECT until_sim FROM intent_cooldown WHERE agent_id=$1 AND intent_key=$2", agent_id, intent_key,
        )

    async def is_cooled(self, *, agent_id: str, action: str, target: str, sim_now: dt.datetime, detail: str = "") -> bool:
        until = await self.until(agent_id, self.make_intent_key(action, target, detail))
        return until is not None and until > sim_now

    async def check_action(
        self, *, agent_id: str, action: str, target: str, sim_now: dt.datetime, detail: str = "",
    ) -> tuple[bool, str | None]:
        """冷却通用校验（04 §6.2 冷却行）：命中 → (False, 原因)；未命中 → (True, None)。"""
        until = await self.until(agent_id, self.make_intent_key(action, target, detail))
        if until is not None and until > sim_now:
            return False, f"意图冷却中：{action}→{target} 至 {until.isoformat()}（01 §3.4）"
        return True, None

    # ---- 意图权重（argue 二道阻尼 / refuse 减半 / 迁怒 ×3） -------------------------

    async def refuse_streak(self, a_id: str, b_id: str) -> int:
        """B 连续 refuse A 的次数：最近一次被接受交互（chat/约定成立）之后累计的 refuse 数（事件流，可回放）。"""
        return await self._pool.fetchval(
            """
            SELECT count(*) FROM events e
            WHERE e.type='social.refuse' AND e.payload->>'from'=$2 AND e.payload->>'to'=$1
              AND e.seq > COALESCE((
                SELECT max(seq) FROM events
                WHERE type IN ('dialogue.chat', 'social.appointment.created') AND actors @> $3::text[]
              ), 0)
            """,
            a_id, b_id, sorted([a_id, b_id]),
        )

    async def intent_weight(
        self, *, agent_id: str, action: str, target: str, sim_now: dt.datetime, tension: int | None = None,
    ) -> float:
        """意图权重综合倍率（常时生效，与冷却并行；决策 prompt 侧加权用，系统不硬改判）。

        - argue 阻尼二道：tension ≥70 → ×0.5（01 §3.4 表下注）；
        - refuse 连续 3 次：invite/chat → ×0.5（01 §4.1 refuse 段）；
        - 迁怒：argue/gossip → ×3，48 模拟小时内（01 §3.3，marker 存 mood JSON grudges 键）。
        """
        w = 1.0
        if action == "argue" and tension is not None and tension >= self._damping["tension_threshold"]:
            w *= float(self._damping["weight_factor"])
        if action in self._streak["actions"] and await self.refuse_streak(agent_id, target) >= self._streak["count"]:
            w *= float(self._streak["weight_factor"])
        if action in ("argue", "gossip"):
            grudges = await self._read_grudges(agent_id)
            until_iso = grudges.get(target)
            if until_iso and dt.datetime.fromisoformat(until_iso) > sim_now:
                w *= float(self._grudge["argue_gossip_weight_factor"])
        return w

    # ---- 迁怒 marker（T-REL-03 挫败值 ≥30 时写入） ----------------------------------

    async def set_grudge(self, *, agent_id: str, target: str, sim_now: dt.datetime) -> dt.datetime:
        """写迁怒 marker（argue/gossip 权重 ×3，持续 48 模拟小时，01 §3.3 迁怒行），返回失效时点。"""
        until = sim_now + dt.timedelta(hours=float(self._grudge["duration_hours"]))
        mood = await self._read_mood(agent_id)
        grudges = dict(mood.get("grudges") or {})
        grudges[target] = until.isoformat()
        mood["grudges"] = grudges
        await self._pool.execute("UPDATE agents SET mood=$2::jsonb WHERE id=$1", agent_id, json.dumps(mood, ensure_ascii=False))
        return until

    async def _read_grudges(self, agent_id: str) -> dict[str, str]:
        return dict((await self._read_mood(agent_id)).get("grudges") or {})

    async def _read_mood(self, agent_id: str) -> dict[str, Any]:
        row = await self._pool.fetchrow("SELECT mood FROM agents WHERE id=$1", agent_id)
        if row is None:
            raise KeyError(f"agent {agent_id!r} 不存在")
        mood = row["mood"]
        if isinstance(mood, str):
            mood = json.loads(mood)
        return dict(mood or {})
