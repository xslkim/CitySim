"""六步裁决管道骨架与裁决主循环（02 T-ADJ-02；04 §6.1 六步逐步伪码、04 §2.2 唯一写协程）。

M1 段范围：step1~step6 管道跑通 **think/轻动作（move）闭环**；LLM 经 03 T-LLM-01 网关注入
（mock provider 归 T-LLM-02，本文只消费注入）。挂接点（后续波次接管）：
- step2 配额检索 → `retrieve`（T-MEM-01）
- step4 动作校验 → `validate`（T-ADJ-03；M1 默认校验器只放行 think/move，其余拦截留痕）
- step5 同源写入/聚合状态事件 → `after_settle`（T-ADJ-05/06）
- step6 反思 → `reflect`（T-MEM-02）

纪律：
- **只有一个裁决协程消费队列并写状态**（00 §4 红线 10）；tick 内 LLM 并发发出（gather），
  结果按队列顺序**串行落库**（04 §2.2）。
- 数值只由系统改，LLM 不直接写（04 §6.1 step5）；本层不改任何数值字段（needs/relations 归 T-REL）。
- 解析失败 → 重试 1 次（追加格式纠错后缀）→ 仍失败记 think 动作（"走神了"）并留痕（04 §6.1 step3）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..llm_gateway import LLMGateway, ChainExhausted, ProviderUnavailable
from ..memory.store import insert_memory
from ..time_engine.batch import batch_advance
from ..time_engine.clock import TimeEngine
from .queue import CLOCK_TICK, WAKEUP, WORLD_EVENT, AdjudicationQueue

log = logging.getLogger(__name__)

STAR_EVERY_N_TICKS = 3  # 明星层每 3 tick 时钟兜底（04 §2.2；T-LOD-01 接管排程前的 M1 最小兜底）

FORMAT_FIX_SUFFIX = (
    "格式错误：请只输出一个合法 JSON 对象，键含 intent/action{type,args}/emotion_delta，不要输出其他任何文字。"
)
DEGRADED_THINK_TEXT = "走神了"  # 04 §6.1 step3 降级 think 动作留痕文本

# think 轻反思档重要性区间 1~3（04 §6.2 think 行）；移动耗时档 5~15min（04 §6.2 move 行，
# 档位映射 T-ADJ-03 持有，M1 工程默认按 rng 在三档间确定性取数，登记偏差）
_MOVE_COST_TIERS = (5, 10, 15)


@dataclass
class Decision:
    """step3 产出的结构化决策（04 §6.1：{intent, action:{type,args}, say?, emotion_delta}）。"""

    agent_id: str
    intent: str
    action_type: str
    action_args: dict[str, Any]
    say: str | None = None
    emotion_delta: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False      # 解析失败降级 think
    attempts: int = 1           # LLM 调用次数（含重试）
    blocked_reason: str | None = None  # step4 拦截原因


@dataclass
class Observation:
    """step1 结构化感知（04 §6.1 step1：sim_time/地点/在场者/需求/余额/日程/未读事件/人设卡）。"""

    agent_id: str
    name: str
    sim_time: dt.datetime
    position: str
    exits: list[str]
    co_located: list[str]
    needs: dict[str, Any]
    balance_cents: int
    goals: list[str]
    recent_events: list[dict[str, Any]]
    persona: dict[str, Any]
    cognition_tier: str

    def prompt_line(self) -> str:
        """OBS_JSON 行（mock provider 的确定性决策契约；M2 模板渲染归 T-LLM-09）。"""
        return "OBS_JSON=" + json.dumps(
            {
                "agent_id": self.agent_id,
                "name": self.name,
                "sim_time": self.sim_time.isoformat(),
                "position": self.position,
                "exits": self.exits,
                "participants": [self.agent_id, *self.co_located],
                "co_located": self.co_located,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


RetrieveFn = Callable[[str, Observation], Awaitable[list[Any]]]
ValidateFn = Callable[[Observation, str, dict[str, Any]], Any]
ReflectFn = Callable[[str, Decision], Awaitable[None]]
AfterSettleFn = Callable[[Decision, list[int]], Awaitable[None]]
ObsBuilderFn = Callable[[str, dt.datetime], Awaitable[Observation]]
SettleFn = Callable[[Observation, Decision, int, dt.datetime, int], Awaitable[list[int]]]


class Pipeline:
    """六步管道。`pool` = asyncpg pool；`gateway` = 03 T-LLM-01 门面；`world` = world.yaml 字典。"""

    def __init__(
        self,
        pool: Any,
        gateway: LLMGateway,
        world: dict[str, Any] | None = None,
        *,
        retrieve: RetrieveFn | None = None,
        validate: ValidateFn | None = None,
        reflect: ReflectFn | None = None,
        after_settle: AfterSettleFn | None = None,
        obs_builder: ObsBuilderFn | None = None,
        settler: SettleFn | None = None,
    ) -> None:
        self._pool = pool
        self._gw = gateway
        self._world = world or {}
        self._retrieve = retrieve
        self._validate = validate or self._default_validate
        self._reflect = reflect
        self._after_settle = after_settle
        self._obs_builder = obs_builder
        self._settler = settler  # T-ADJ-03：19 动作结算总线（None = M1 骨架 think/move 内置路径）

    # ---- tick 驱动（04 §2.2：LLM 并发、落库串行） -------------------------

    async def run_tick(self, *, tick: int, sim_now: dt.datetime, agent_ids: list[str], rng_seed: int) -> list[int]:
        """本 tick 一批 agent 的六步闭环；返回落库事件 seq 列表（按队列顺序）。"""
        # step1~step3：LLM 并发发出（04 §2.2"tick 内 LLM 调用并发发出"）
        decisions = await asyncio.gather(
            *(self._decide_one(aid, tick, sim_now, rng_seed) for aid in agent_ids)
        )
        # step4~step6：按队列顺序串行回写（04 §2.2"结果写回按队列顺序串行落库"）
        seqs: list[int] = []
        for decision in decisions:
            seqs.extend(await self._settle_one(tick, sim_now, rng_seed, decision))
        return seqs

    # ---- step1~step3（并发安全，无写） ------------------------------------

    async def _decide_one(self, agent_id: str, tick: int, sim_now: dt.datetime, rng_seed: int) -> Decision:
        obs = await (self._obs_builder(agent_id, sim_now) if self._obs_builder else self._build_obs(agent_id, sim_now))
        mems = await self._retrieve(agent_id, obs) if self._retrieve else []  # step2 挂接点（T-MEM-01）
        messages = self._render_messages(obs, mems)
        gen_params = self._gw.gen_params("star_decision")
        attempts = 0
        last_text = ""
        for attempt in range(2):  # 首次 + 重试 1 次（04 §6.1 step3）
            attempts += 1
            try:
                result = await self._gw.chat(
                    "star_decision", messages, gen_params, seed=rng_seed, agent_id=agent_id, sim_time=sim_now
                )
            except (ChainExhausted, ProviderUnavailable) as exc:
                # M2 真接入崩溃护栏（03 §6 D43/D45）：链尽/全跳不可用（pause_clock 等末端动作的消费
                # 接线归 08 T-OPS-03）→ 裁决侧降级 think 兜底，防单点撞墙拖垮主循环。
                log.error("LLM 链路不可用（%s）→ 本拍降级 think 兜底", exc)
                break
            last_text = result.text
            decision = self._parse_decision(agent_id, last_text, attempts)
            if decision is not None:
                return decision
            messages = [*messages, {"role": "user", "content": FORMAT_FIX_SUFFIX}]
        log.warning("agent %s 决策解析两次失败，降级 think（%s）", agent_id, DEGRADED_THINK_TEXT)
        return Decision(
            agent_id=agent_id,
            intent=DEGRADED_THINK_TEXT,
            action_type="think",
            action_args={"topic_hint": "走神"},
            degraded=True,
            attempts=attempts,
        )

    @staticmethod
    def _parse_decision(agent_id: str, text: str, attempts: int) -> Decision | None:
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(body, dict):
            return None
        action = body.get("action")
        if not isinstance(action, dict) or not isinstance(action.get("type"), str):
            return None
        # 真 LLM 鲁棒性（T-LLM-12）：args/emotion_delta 非 object 时走重试/降级，不崩管道
        args = action.get("args")
        if args is not None and not isinstance(args, dict):
            return None
        emotion_delta = body.get("emotion_delta")
        if emotion_delta is not None and not isinstance(emotion_delta, dict):
            return None
        return Decision(
            agent_id=agent_id,
            intent=str(body.get("intent", "")),
            action_type=action["type"],
            action_args=dict(args or {}),
            say=body.get("say") if isinstance(body.get("say"), str) else None,
            emotion_delta=dict(emotion_delta or {}),
            attempts=attempts,
        )

    def _render_messages(self, obs: Observation, mems: list[Any]) -> list[dict[str, str]]:
        """M1 轻量渲染（prompt 模板族归 03 T-LLM-09，M2 替换；人设卡每次必带，04 §6.1 step1）。"""
        persona_brief = {
            "name": obs.persona.get("name", obs.name),
            "age": obs.persona.get("age"),
            "big_five": obs.persona.get("big_five"),
            "speech_style": obs.persona.get("speech_style"),
        }
        system = (
            "你是角色扮演引擎。只输出 JSON。"
            + json.dumps({"persona": persona_brief}, ensure_ascii=False, sort_keys=True)
        )
        mem_lines = "\n".join(f"- {m}" for m in mems[:10]) or "- （无）"
        user = (
            obs.prompt_line()
            + f"\n需求={json.dumps(obs.needs, ensure_ascii=False, sort_keys=True)}"
            + f"\n余额分={obs.balance_cents}"
            + f"\n目标={json.dumps(obs.goals, ensure_ascii=False)}"
            + f"\n近期事件={json.dumps(obs.recent_events, ensure_ascii=False)}"
            + f"\n相关记忆：\n{mem_lines}"
            + "\n请输出本拍决策 JSON：{intent, action:{type, args}, say?, emotion_delta}。"
            "轻动作只可选 think 或 move（move 的 args.to 必须取自 OBS_JSON.exits）。"
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def _build_obs(self, agent_id: str, sim_now: dt.datetime) -> Observation:
        row = await self._pool.fetchrow(
            "SELECT id, name, persona, needs, balance_cents, position, cognition_tier FROM agents WHERE id=$1",
            agent_id,
        )
        if row is None:
            raise KeyError(f"agent {agent_id!r} 不存在")
        co_located = [
            r["id"]
            for r in await self._pool.fetch(
                "SELECT id FROM agents WHERE position=$1 AND id<>$2 ORDER BY id", row["position"], agent_id
            )
        ]
        goals = [
            r["goal"]
            for r in await self._pool.fetch(
                "SELECT goal FROM goals WHERE agent_id=$1 AND status='active' ORDER BY id", agent_id
            )
        ]
        recent = await self._pool.fetch(
            "SELECT seq, type, visibility FROM events WHERE $1 = ANY(actors) ORDER BY seq DESC LIMIT 5",
            agent_id,
        )
        persona = row["persona"]
        if isinstance(persona, str):
            persona = json.loads(persona)
        needs = row["needs"]
        if isinstance(needs, str):
            needs = json.loads(needs)
        return Observation(
            agent_id=agent_id,
            name=row["name"],
            sim_time=sim_now,
            position=row["position"],
            exits=self.exits_for(row["position"], sim_now),
            co_located=co_located,
            needs=dict(needs or {}),
            balance_cents=row["balance_cents"],
            goals=goals,
            recent_events=[dict(r) for r in recent],
            persona=dict(persona or {}),
            cognition_tier=row["cognition_tier"],
        )

    # ---- step4~step6（串行，唯一写路径） -----------------------------------

    async def _settle_one(self, tick: int, sim_now: dt.datetime, rng_seed: int, decision: Decision) -> list[int]:
        obs = await self._build_obs(decision.agent_id, sim_now)  # 结算前重读最新态（串行安全）
        verdict = self._validate(obs, decision.action_type, decision.action_args)
        if asyncio.iscoroutine(verdict):
            verdict = await verdict  # T-ADJ-03 校验器为异步（DB 读取），M1 默认校验器保持同步
        ok, reason = verdict
        seqs: list[int] = []
        if not ok:
            decision.blocked_reason = reason
            await self._write_blocked_memory(decision, reason, sim_now, rng_seed)
            if self._after_settle:
                await self._after_settle(decision, seqs)
            return seqs
        if self._settler is not None:
            seqs.extend(await self._settler(obs, decision, tick, sim_now, rng_seed))
        elif decision.action_type == "move":
            seqs.append(await self._settle_move(obs, decision, tick, sim_now, rng_seed))
        else:  # think（含降级）与未实现动作被拦截后兜底不到的 think
            seqs.append(await self._settle_think(obs, decision, tick, sim_now, rng_seed))
        if self._after_settle:
            await self._after_settle(decision, seqs)
        if self._reflect:
            await self._reflect(decision.agent_id, decision)  # step6 挂接点（T-MEM-02）
        return seqs

    def _default_validate(self, obs: Observation, action_type: str, args: dict[str, Any]) -> tuple[bool, str | None]:
        """M1 默认校验器（19 动作校验器归 T-ADJ-03）：放行 think/move，move 目标须可达。"""
        if action_type == "think":
            return True, None
        if action_type == "move":
            target = args.get("to")
            if isinstance(target, str) and target in obs.exits and target != obs.position:
                return True, None
            return False, f"目标不可达: {target!r}"
        return False, f"动作 {action_type} 校验器未实现（T-ADJ-03）"

    async def _settle_think(self, obs: Observation, d: Decision, tick: int, sim_now: dt.datetime, rng_seed: int) -> int:
        """think 轻反思档（04 §6.2 think 行）：agent.think 事件 + 低重要性 reflection 记忆，不产 agent.reflection。"""
        seq = await self._insert_event(
            tick=tick, sim_time=sim_now, type_="agent.think", source=f"agent:{obs.agent_id}",
            trigger="autonomous", actors=[obs.agent_id], location_id=obs.position,
            visibility="internal", rng_seed=rng_seed,
            payload={"topic_hint": d.action_args.get("topic_hint", d.intent[:20])},
        )
        importance = 1 + (rng_seed + int(obs.agent_id[1:])) % 3  # 低重要性 1~3（04 §6.2）
        content = d.intent if not d.degraded else f"{DEGRADED_THINK_TEXT}（LLM 输出解析失败降级，04 §6.1 step3）"
        await self._insert_memory(
            agent_id=obs.agent_id, sim_time=sim_now, kind="reflection", content=content,
            importance=importance, source_event_seq=seq, rng_seed=rng_seed,
        )
        return seq

    async def _settle_move(self, obs: Observation, d: Decision, tick: int, sim_now: dt.datetime, rng_seed: int) -> int:
        """move 轻结算：更新位置缓存列 + agent.move 事件（payload from/to/sim_cost_min 逐字 06 §1.2）。"""
        target = d.action_args["to"]
        await self._pool.execute("UPDATE agents SET position=$2 WHERE id=$1", obs.agent_id, target)
        cost = _MOVE_COST_TIERS[(rng_seed + int(obs.agent_id[1:])) % len(_MOVE_COST_TIERS)]
        return await self._insert_event(
            tick=tick, sim_time=sim_now, type_="agent.move", source=f"agent:{obs.agent_id}",
            trigger="autonomous", actors=[obs.agent_id], location_id=target,
            visibility="public", rng_seed=rng_seed,
            payload={"from": obs.position, "to": target, "sim_cost_min": cost},
        )

    async def _write_blocked_memory(self, d: Decision, reason: str, sim_now: dt.datetime, rng_seed: int) -> None:
        """step4 拦截留痕："想做 X 但被现实阻止：原因"（04 §6.1 step4）。"""
        await self._insert_memory(
            agent_id=d.agent_id, sim_time=sim_now, kind="event",
            content=f"想做{d.action_type}但被现实阻止：{reason}",
            importance=2,  # 工程默认：轻档记忆（与 think 重要性同带 1~3）
            source_event_seq=None, rng_seed=rng_seed,
        )

    # ---- 可达性（M1 轻量版；真实可达图归 T-ADJ-03） -------------------------

    def exits_for(self, position: str, sim_now: dt.datetime) -> list[str]:
        """当前位置的可移动候选（M1 骨架：公寓公共区按开放时段过滤 / 公司公共节点）。"""
        minute = sim_now.hour * 60 + sim_now.minute
        exits: list[str] = []
        if position.startswith("apt."):
            for node in self._world.get("locations", {}).get("apartment", {}).get("commons", []):
                if self._node_open(node, minute):
                    exits.append(node["id"])
        elif position.startswith("corp."):
            for node in self._world.get("locations", {}).get("office", []):
                if node.get("kind") in ("common", "meeting") and node.get("enterable", True):
                    exits.append(node["id"])
        return sorted(set(exits))

    @staticmethod
    def _node_open(node: dict[str, Any], minute: int) -> bool:
        def _hhmm(s: str) -> int:
            hh, mm = str(s).split(":")
            return int(hh) * 60 + int(mm)

        open_ = node.get("open")
        if open_:
            start, end = _hhmm(open_[0]), _hhmm(open_[1])
            if not (start <= minute < end):
                return False
        closed = node.get("closed")
        if closed:
            c0, c1 = _hhmm(closed[0]), _hhmm(closed[1])
            if c0 <= minute < c1:
                return False
        return True

    # ---- SQL 写入原语（仅本类调用，裁决协程内串行执行） ----------------------

    async def _insert_event(
        self, *, tick: int, sim_time: dt.datetime, type_: str, source: str, trigger: str,
        actors: list[str], location_id: str | None, visibility: str, rng_seed: int,
        payload: dict[str, Any],
    ) -> int:
        return await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, location_id, actors, rng_seed, visibility, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            RETURNING seq
            """,
            tick, sim_time, type_, source, trigger, location_id, actors, rng_seed, visibility,
            json.dumps(payload, ensure_ascii=False),
        )

    async def _insert_memory(
        self, *, agent_id: str, sim_time: dt.datetime, kind: str, content: str,
        importance: int, source_event_seq: int | None, rng_seed: int,
    ) -> int:
        """写记忆（委托 memory/store.insert_memory 唯一写径；content_display M1 直写，02 文档 D10）。"""
        return await insert_memory(
            self._pool, self._gw, agent_id=agent_id, sim_time=sim_time, kind=kind,
            content=content, importance=importance, source_event_seq=source_event_seq, rng_seed=rng_seed,
        )


# ---- 裁决主循环（04 §2.2 adjudication_loop：唯一写协程） ---------------------


async def adjudication_loop(
    pool: Any,
    queue: AdjudicationQueue,
    gateway: LLMGateway,
    clock: TimeEngine,
    pipeline: Pipeline,
    *,
    stop: asyncio.Event,
    notify: asyncio.Event | None = None,
    drained: asyncio.Event | None = None,
    agent_ids: tuple[str, ...] = (),
    batch_summarize: Callable[[str, float], Awaitable[None]] | None = None,
    director_preempt: Callable[[], Awaitable[None]] | None = None,
    after_tick: Callable[[int, dt.datetime], Awaitable[None]] | None = None,
    lod: Any = None,
) -> None:
    """唯一裁决协程：消费队列、驱动六步管道、串行落库（04 §2.2，00 §4 红线 10）。

    - 时钟兜底：star 每 3 tick 全员排程（M1 最小兜底；T-LOD-01 接管 next_due 排程）。
    - 交互唤醒：入队即在本裁决点处理（唤醒去重已在队列侧完成）。
    - 世界事件：`enter_batch` → 驱动 T-TIME-03 batch_advance（编剧插队接口留位）。
    - `after_tick(tick, sim_now)`：每裁决点收尾挂点（T-REL-01 需求衰减 / 04 §6.5 聚合事件 flush /
      T-MEM-02 每日兜底反思，波次 2a 接线）；sim_now 取 `clock.now_sim()`（batch 后为补进后的当前模拟时刻）。
    - `lod`：T-LOD-01 时钟兜底排程器（`collect_due` 写回 `agents.next_due_sim`）；注入后取代
      `STAR_EVERY_N_TICKS` 最小兜底块（缺省保留旧行为，供既有测试）。
    - rng_seed = 本 tick 序号（04 §5.2 events.rng_seed 规则骰子口径，可复现）。
    """
    while not stop.is_set():
        if notify is not None:
            notify.clear()
            if len(queue) == 0:
                try:
                    await asyncio.wait_for(notify.wait(), timeout=0.5)
                except TimeoutError:
                    pass
        if stop.is_set():
            break
        tick = clock.current_tick
        items = queue.pop_due(tick)
        if not items:
            if drained is not None:
                drained.set()
            continue
        agents_due: list[str] = []
        clock_seen = False
        for it in items:
            if it.kind == WORLD_EVENT:
                enter = it.payload.get("enter_batch")
                if enter is not None:
                    if batch_summarize is None:
                        log.warning("enter_batch 到达但无 batch_summarize 注入，跳过")
                        continue
                    try:
                        await batch_advance(
                            clock, agent_ids, float(enter["sim_hours"]),
                            summarize=batch_summarize, director_preempt=director_preempt,
                        )
                    except (ChainExhausted, ProviderUnavailable) as exc:
                        log.error("batch 段 LLM 链路不可用（%s）→ 本批 LLM 摘要跳过（03 §6 D45 护栏）", exc)
            elif it.kind == WAKEUP:
                if it.agent_id and it.agent_id not in agents_due:
                    agents_due.append(it.agent_id)
            elif it.kind == CLOCK_TICK:
                clock_seen = True
        if clock_seen:
            if lod is not None:
                for aid in await lod.collect_due(tick=tick, sim_now=clock.sim_of_tick(tick)):
                    if aid not in agents_due:
                        agents_due.append(aid)
            elif tick % STAR_EVERY_N_TICKS == 0:
                rows = await pool.fetch("SELECT id FROM agents WHERE cognition_tier='star' ORDER BY id")
                for r in rows:
                    if r["id"] not in agents_due:
                        agents_due.append(r["id"])
        if agents_due:
            sim_now = clock.sim_of_tick(tick)
            try:
                await pipeline.run_tick(tick=tick, sim_now=sim_now, agent_ids=agents_due, rng_seed=tick)
            except (ChainExhausted, ProviderUnavailable) as exc:
                # M2 真接入崩溃护栏（03 §6 D43/D45）：结算/对话路径链尽不拖垮主循环；
                # pause_clock 等末端动作的消费接线归 08 T-OPS-03。
                log.error("LLM 链路不可用（%s）→ 本 tick 剩余结算跳过", exc)
        if after_tick is not None and not clock.batch_mode:
            try:
                await after_tick(tick, clock.now_sim())
            except (ChainExhausted, ProviderUnavailable) as exc:
                log.error("after_tick LLM 链路不可用（%s）→ 本 tick 收尾 LLM 作业跳过（03 §6 D45 护栏）", exc)
        if drained is not None:
            drained.set()
    log.info("adjudication_loop 退出（stop）")
