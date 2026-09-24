"""三层认知统一接口与时钟兜底排程（02 T-LOD-01；04 §4.1 CognitiveUnit 协议、04 §2.2 兜底频率）。

- `CognitiveUnit` Protocol 按 04 §4.1：`next_due(sim_now)`（star=+15min、secondary=+2h、
  background=凌晨 batch 段——04 §3.3 单一时点，时钟兜底永不排程背景层）与 `run(ctx)`；
  `agent_id` 为 TEXT `'A01'~'A40'`（00 §4 红线 1）。
- `LodScheduler.collect_due`：每 tick 收集 `next_due_sim <= sim_now` 的 unit 进裁决队列
  （时钟兜底，经调用方入队），写回 `agents.next_due_sim`；交互唤醒插队升格判定归 T-LOD-02。
- 认知排程只读 sim_time（00 §4 红线 11）：一切判定入参为 sim_time，压缩比切换不影响次数。
- M1：8 人小世界全员 `'star'`；secondary/background 为同接口的廉价决策/日摘要桩
  （由 mock provider 驱动，02 文档 §1 范围行），升降格逻辑接口就位（T-LOD-02/03 接管）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# 三层兜底间隔（04 §4.1/§2.2：star 每模拟 15 分钟 = 每 3 tick；secondary 每模拟 2 小时 = 每 24 tick）
STAR_INTERVAL = dt.timedelta(minutes=15)
SECONDARY_INTERVAL = dt.timedelta(hours=2)
TIERS: tuple[str, ...] = ("star", "secondary", "background")


@dataclass(frozen=True)
class EventDraft:
    """unit.run 的产出（未落库事件草稿；落库串行归裁决协程，04 §2.2）。"""

    type: str
    actors: list[str]
    payload: dict[str, Any]
    visibility: str = "internal"
    location_id: str | None = None


@dataclass
class UnitContext:
    """unit.run 的上下文（网关/池/sim 时间/种子由调度器注入）。"""

    pool: Any
    gateway: Any
    sim_now: dt.datetime
    tick: int
    rng_seed: int
    extras: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CognitiveUnit(Protocol):
    """三层统一接口（04 §4.1）。agent_id 为 TEXT 'A01'~'A40'。"""

    agent_id: str

    def next_due(self, sim_now: dt.datetime) -> dt.datetime | None:
        """下次兜底时点；None = 时钟兜底不排程（background = 凌晨 batch 段单一时点，04 §3.3）。"""
        ...

    async def run(self, ctx: UnitContext) -> list[EventDraft]:
        """跑一拍认知，产出事件草稿（star=六步全循环 / secondary=廉价决策 / background=日摘要）。"""
        ...


def next_due_for(tier: str, sim_now: dt.datetime) -> dt.datetime | None:
    """分层兜底间隔（04 §4.1 写死）：star=+15min、secondary=+2h、background=None（batch 段）。"""
    if tier == "star":
        return sim_now + STAR_INTERVAL
    if tier == "secondary":
        return sim_now + SECONDARY_INTERVAL
    if tier == "background":
        return None
    raise ValueError(f"未知 cognition_tier {tier!r}（封闭枚举 {TIERS}，04 §5.1）")


class StarUnit:
    """明星层：六步全循环（04 §6.1；M1 由 Pipeline 驱动，本类持接口与兜底间隔）。"""

    task_type = "star_decision"

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def next_due(self, sim_now: dt.datetime) -> dt.datetime | None:
        return next_due_for("star", sim_now)

    async def run(self, ctx: UnitContext) -> list[EventDraft]:
        """桩：六步全循环的管道归 T-ADJ-02（Pipeline.run_tick）；本接口供分发层统一签名。"""
        return []


class SecondaryUnit:
    """次要层：廉价决策桩（04 §4.3 prompt 结构；M1 mock provider 驱动，task_type='secondary'）。

    只出高层意图与情绪变化、可主动发起事件（04 §4.3）；M1 桩将意图折成 think 草稿，
    真实动作进裁决队列的联动随 T-ADJ-03 校验器接线后生效。
    """

    task_type = "secondary"

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def next_due(self, sim_now: dt.datetime) -> dt.datetime | None:
        return next_due_for("secondary", sim_now)

    async def run(self, ctx: UnitContext) -> list[EventDraft]:
        result = await ctx.gateway.chat(
            self.task_type,
            [
                {"role": "system", "content": "你是角色扮演引擎。只输出 JSON。"},
                {"role": "user", "content": f'OBS_JSON={{"agent_id":"{self.agent_id}"}}\n输出高层意图。'},
            ],
            ctx.gateway.gen_params(self.task_type) if hasattr(ctx.gateway, "gen_params") else None,
            seed=ctx.rng_seed, agent_id=self.agent_id, sim_time=ctx.sim_now,
        )
        try:
            body = json.loads(result.text)
            intent = str(body.get("intent") or "")[:30]
        except (ValueError, TypeError):
            intent = ""
        if not intent:
            return []
        return [EventDraft(type="agent.think", actors=[self.agent_id], payload={"topic_hint": intent})]


class BackgroundUnit:
    """背景层：日摘要桩（04 §4.3；唯一时点 = 凌晨 batch 段，时钟兜底不排程）。"""

    task_type = "bgsummary"

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def next_due(self, sim_now: dt.datetime) -> dt.datetime | None:
        return next_due_for("background", sim_now)  # None：04 §3.3 单一时点（评审 P2-4）

    async def run(self, ctx: UnitContext) -> list[EventDraft]:
        result = await ctx.gateway.chat(
            self.task_type,
            [
                {"role": "system", "content": "总结该角色今天的模拟日。输出 JSON。"},
                {"role": "user", "content": f'OBS_JSON={{"agent_id":"{self.agent_id}"}}\n总结今天。'},
            ],
            ctx.gateway.gen_params(self.task_type) if hasattr(ctx.gateway, "gen_params") else None,
            seed=ctx.rng_seed, agent_id=self.agent_id, sim_time=ctx.sim_now,
        )
        try:
            diary = str(json.loads(result.text).get("diary") or "")
        except (ValueError, TypeError):
            diary = ""
        if not diary:
            return []
        return [EventDraft(type="agent.think", actors=[self.agent_id], payload={"topic_hint": diary[:20]})]


def unit_for(tier: str, agent_id: str) -> CognitiveUnit:
    """按 `agents.cognition_tier` 分发到同一接口的三个实现（04 §4.1）。"""
    if tier == "star":
        return StarUnit(agent_id)
    if tier == "secondary":
        return SecondaryUnit(agent_id)
    if tier == "background":
        return BackgroundUnit(agent_id)
    raise ValueError(f"未知 cognition_tier {tier!r}（封闭枚举 {TIERS}）")


class LodScheduler:
    """时钟兜底排程器：每 tick 收集到期 unit，写回 `agents.next_due_sim`（04 §4.1）。

    背景层永不进入时钟兜底（`next_due` = None，日摘要唯一时点 = 凌晨 batch 段，04 §3.3）。
    写回发生在裁决协程内（唯一写协程前提，00 §4 红线 10）。
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def collect_due(self, *, tick: int, sim_now: dt.datetime) -> list[str]:
        """收集 `next_due_sim <= sim_now`（NULL 视为立即到期）的 star/secondary agent，写回兜底时点。

        返回到期 agent id 列表（按 id 升序，确定性）；调用方（裁决协程）据此进队列。
        """
        rows = await self._pool.fetch(
            """
            SELECT id, cognition_tier FROM agents
            WHERE cognition_tier <> 'background'
              AND (next_due_sim IS NULL OR next_due_sim <= $1)
            ORDER BY id
            """,
            sim_now,
        )
        due: list[str] = []
        for r in rows:
            nxt = next_due_for(r["cognition_tier"], sim_now)
            await self._pool.execute("UPDATE agents SET next_due_sim=$2 WHERE id=$1", r["id"], nxt)
            due.append(r["id"])
        return due

    async def next_due_of(self, agent_id: str) -> dt.datetime | None:
        return await self._pool.fetchval("SELECT next_due_sim FROM agents WHERE id=$1", agent_id)
