"""目标系统与受阻挫败规则（02 T-REL-03；01 §3.3 数值唯一持有方 / 04 §5.2 goals 表）。

- 36 条周目标库读 `config/goals.yaml`（01 §3.3 逐字镜像，已入 00 §2 配置清单）；目标注入 step1 obs
  经 T-ADJ-02 管道既有 goals 读取（`Pipeline._build_obs`），本模块不重复实现注入。
- 每 agent 活跃周目标数按层（star 3 / secondary 2 / background 1），每周日晚批量刷新；抽取权重
  = 库权重 × 人格偏向倍乘（偏向判定的 M1 工程口径见 `_bias_multiplier`，P1 校准前默认，D18）；
  抽取骰子 = `random.Random(f"{week}:{agent_id}")` 确定性（可回放）。
- 受阻/挫败七条硬数字逐行按 01 §3.3 表（goals.yaml `frustration_rules` 镜像）；挫败值持久化于
  `goals.frustration`。挫败事件的情绪 -12 经 `StateAggregator` 并入本 tick `state.needs_delta`。
- 迁怒（≥30）：关系结算经 `agg`、argue/gossip 权重 ×3 经 T-REL-04 `CooldownEngine.set_grudge`；
  换策略（≥50）/放弃（≥80）的"生成反思"经 T-MEM-02 `write_generated_reflection` 挂接（`reflect_fn` 注入）。
- 完成条件判定器实现可编程子集（P0：chat 场次数/邀约成功/债务结清等事件计数类，02 文档 D9；
  库行 `check.tier=P1` 的目标 M1 不判定）。
- 周目标库与 goals 行的连接 = goal 文本精确匹配（04 §5.2 goals 表无库 id 列，工程口径 D18）。
"""

from __future__ import annotations

import datetime as dt
import json
import random
from typing import Any, Awaitable, Callable

import yaml

from ..adjudicator.state_events import StateAggregator
from .cooldown import CooldownEngine
from .relations import RelationEngine

DEFAULT_ANCHOR_DATE = dt.date(2026, 10, 12)  # 叙事起点（周一；seed clock.anchor 同日）

_DIM_MAP = {"E": "extraversion", "A": "agreeableness", "C": "conscientiousness", "N": "neuroticism", "O": "openness"}
_EDGE_LABELS = {"有暗恋边": ("暗恋",), "有师徒边": ("师徒",), "有对手边": ("职场对手",), "有情敌边": ("情敌",), "有老乡组": ("老乡",)}

# 生成反思挂接签名（T-MEM-02 Reflector.write_generated_reflection 提供）
ReflectFn = Callable[[str, str], Awaitable[None]]


def load_goals(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    goals = cfg.get("goals")
    if not isinstance(goals, list) or len(goals) != cfg["meta"]["count"]:
        raise ValueError(f"goals.yaml 目标库须恰 {cfg['meta']['count']} 条（01 §3.3），实得 {len(goals or [])}")
    ids = [g["id"] for g in goals]
    if len(set(ids)) != len(ids):
        raise ValueError("goals.yaml 目标 id 重复（01 §3.3 G-XXX-nn 唯一）")
    return cfg


class GoalEngine:
    """目标系统引擎。`pool` = asyncpg pool；`cfg` = goals.yaml 字典。"""

    def __init__(
        self,
        pool: Any,
        cfg: dict[str, Any],
        *,
        cooldown: CooldownEngine | None = None,
        relations: RelationEngine | None = None,
        reflect_fn: Callable[..., Awaitable[None]] | None = None,
        anchor_date: dt.date = DEFAULT_ANCHOR_DATE,
    ) -> None:
        self._pool = pool
        self._lib = {g["id"]: g for g in cfg["goals"]}
        self._by_text = {g["text"]: g for g in cfg["goals"]}
        self._rules = cfg["frustration_rules"]
        self._tier_counts = cfg["meta"]["active_per_tier"]
        self._cooldown = cooldown
        self._relations = relations
        self._reflect = reflect_fn
        self._anchor = anchor_date

    # ---- 周日历 ---------------------------------------------------------------

    def week_of(self, sim_now: dt.datetime) -> int:
        return 1 + (sim_now.date() - self._anchor).days // 7

    def week_start(self, week: int) -> dt.datetime:
        return dt.datetime.combine(self._anchor + dt.timedelta(days=(week - 1) * 7), dt.time(0, 0), tzinfo=dt.timezone(dt.timedelta(hours=8)))

    # ---- 刷新（每周日晚批量；batch 钩子驱动） --------------------------------------

    async def refresh_weekly(self, agg: StateAggregator, *, sim_now: dt.datetime, cause: str) -> dict[str, list[str]]:
        """周日刷新：完成判定 → 失败结算（受阻≥2 +8 / 未受阻 +4）→ 关闭未完成 → 按层抽新目标。

        返回 {agent_id: [新活跃目标库 id]}。幂等：本周已有 active 目标的 agent 不重复抽取。
        """
        week = self.week_of(sim_now)
        agents = await self._pool.fetch("SELECT id, cognition_tier, persona, needs FROM agents ORDER BY id")
        drawn: dict[str, list[str]] = {}
        for agent in agents:
            aid = agent["id"]
            await self.check_completions(agent_id=aid, sim_now=sim_now)
            await self._settle_week_failures(aid, week, agg, sim_now, cause)
            existing = await self._pool.fetchval(
                "SELECT count(*) FROM goals WHERE agent_id=$1 AND sim_week=$2 AND status='active'", aid, week,
            )
            if existing:
                continue
            count = self._tier_counts.get(agent["cognition_tier"], 1)
            picks = await self._draw(agent, week, count)
            for g in picks:
                await self._pool.execute(
                    "INSERT INTO goals (agent_id, sim_week, goal) VALUES ($1, $2, $3)", aid, week, g["text"],
                )
            drawn[aid] = [g["id"] for g in picks]
        return drawn

    async def _settle_week_failures(self, agent_id: str, week: int, agg: StateAggregator, sim_now: dt.datetime, cause: str) -> None:
        """周日晚失败结算：上一周及更早的未完成 active 目标 → 挫败值 += 8（受阻≥2）/ 4（纯没做），关闭行。"""
        rows = await self._pool.fetch(
            "SELECT id, goal, blocked_count, frustration FROM goals WHERE agent_id=$1 AND status='active' AND sim_week<$2",
            agent_id, week,
        )
        r = self._rules
        for row in rows:
            gain = r["weekly_fail_blocked_gain"] if row["blocked_count"] >= r["weekly_fail_blocked_min"] else r["weekly_fail_idle_gain"]
            prev = int(row["frustration"])
            new = prev + gain
            await self._pool.execute(
                "UPDATE goals SET frustration=$2, status='abandoned' WHERE id=$1", row["id"], new,
            )
            await self._apply_frustration_effects(
                agent_id=agent_id, goal_row={"id": row["id"], "goal": row["goal"]},
                prev=prev, new=new, agg=agg, sim_now=sim_now, cause=cause, blocker_id=None,
            )

    async def _draw(self, agent: Any, week: int, count: int) -> list[dict[str, Any]]:
        """按权重 × 人格偏向抽取 count 条不重复目标（确定性骰子：同 week+agent 同结果）。"""
        rng = random.Random(f"goal-draw:{week}:{agent['id']}")
        weighted: list[tuple[dict[str, Any], float]] = []
        for g in self._lib.values():
            w = await self._entry_weight(agent, g)
            if w > 0:
                weighted.append((g, w))
        picks: list[dict[str, Any]] = []
        pool = weighted
        for _ in range(min(count, len(pool))):
            total = sum(w for _, w in pool)
            roll = rng.uniform(0, total)
            acc = 0.0
            for i, (g, w) in enumerate(pool):
                acc += w
                if roll <= acc:
                    picks.append(g)
                    pool = [*pool[:i], *pool[i + 1:]]
                    break
        return picks

    async def _entry_weight(self, agent: Any, entry: dict[str, Any]) -> float:
        base = entry["weight"]
        if base == "按债务边":  # G-MON-03：有未结清欠款（b_id=agent）才有抽取权重（工程默认权重 6，D18）
            open_debt = await self._pool.fetchval(
                "SELECT count(*) FROM debts WHERE b_id=$1 AND repaid_cents < amount_cents", agent["id"],
            )
            return 6.0 if open_debt else 0.0
        return float(base) * await self._bias_multiplier(agent, entry.get("persona_bias") or "")

    async def _bias_multiplier(self, agent: Any, bias: str) -> float:
        """人格偏向倍乘（M1 工程口径，P1 校准前默认，D18）：

        - 维度项（E+/C++/A 低/N 高 等）：满足档（≥70 高 / ≤30 低）×1.5（++ 为 ×2），不满足 ×1；
        - 关系边条件（有暗恋边/有债主边/有师徒边/有对手边/有情敌边/有老乡组）：不满足 → 0（不入选）；
        - 财富<30：needs.wealth <30 → ×1.5；全员/空 → ×1。
        """
        if not bias or bias == "全员":
            return 1.0
        mult = 1.0
        persona = agent["persona"]
        if isinstance(persona, str):
            persona = json.loads(persona)
        big_five = dict((persona or {}).get("big_five") or {})
        needs = agent["needs"]
        if isinstance(needs, str):
            needs = json.loads(needs)
        for token in [t.strip() for t in bias.replace("，", ",").replace("/", ",").split(",") if t.strip()]:
            if token in _EDGE_LABELS:
                labels = _EDGE_LABELS[token]
                hit = await self._pool.fetchval(
                    """
                    SELECT count(*) FROM relations
                    WHERE (a_id=$1 OR b_id=$1) AND labels && $2::text[]
                    """,
                    agent["id"], list(labels),
                )
                if not hit:
                    return 0.0
            elif token == "有债主边":
                hit = await self._pool.fetchval(
                    "SELECT count(*) FROM debts WHERE b_id=$1 AND repaid_cents < amount_cents", agent["id"],
                ) or await self._pool.fetchval(
                    "SELECT count(*) FROM relations WHERE a_id=$1 AND '债主' = ANY(labels)", agent["id"],
                )
                if not hit:
                    return 0.0
            elif token == "财富<30":
                if float((needs or {}).get("wealth", 100)) < 30:
                    mult *= 1.5
            elif len(token) >= 2 and token[0] in _DIM_MAP:
                dim = _DIM_MAP[token[0]]
                value = float(big_five.get(dim, 50))
                if token.endswith("++"):
                    mult *= 2.0 if value >= 70 else 1.0
                elif token.endswith("+"):
                    mult *= 1.5 if value >= 70 else 1.0
                elif token.endswith("低"):
                    mult *= 1.5 if value <= 30 else 1.0
                elif token.endswith("高"):
                    mult *= 1.5 if value >= 70 else 1.0
        return mult

    # ---- 受阻与挫败（01 §3.3 硬数字逐行） ------------------------------------------

    async def record_block(
        self, agg: StateAggregator, *, goal_id: int, sim_now: dt.datetime, cause: str, blocker_id: str | None = None,
    ) -> dict[str, Any]:
        """受阻计数 +1；恰第 3 次触发挫败事件（情绪 -12 并入 needs_delta、挫败值 +10）并应用三档后果。"""
        row = await self._pool.fetchrow(
            "UPDATE goals SET blocked_count=blocked_count+1 WHERE id=$1 AND status='active' RETURNING id, agent_id, goal, blocked_count, frustration",
            goal_id,
        )
        if row is None:
            raise KeyError(f"goals 行 {goal_id} 不存在或非 active")
        out = {"goal_id": row["id"], "blocked_count": row["blocked_count"], "frustrated": False}
        if row["blocked_count"] == self._rules["frustration_trigger_blocks"]:
            r = self._rules
            await agg.apply_needs_delta(agent_id=row["agent_id"], need="mood", delta=float(r["frustration_mood_delta"]), cause=cause)
            prev = int(row["frustration"])
            new = prev + int(r["frustration_gain"])
            await self._pool.execute("UPDATE goals SET frustration=$2 WHERE id=$1", row["id"], new)
            await self._apply_frustration_effects(
                agent_id=row["agent_id"], goal_row={"id": row["id"], "goal": row["goal"]},
                prev=prev, new=new, agg=agg, sim_now=sim_now, cause=cause, blocker_id=blocker_id,
            )
            out["frustrated"] = True
            out["frustration"] = new
        return out

    async def _apply_frustration_effects(
        self, *, agent_id: str, goal_row: dict[str, Any], prev: int, new: int,
        agg: StateAggregator, sim_now: dt.datetime, cause: str, blocker_id: str | None,
    ) -> None:
        """挫败值三档（跨档触发一次）：≥30 迁怒 / ≥50 换策略 / ≥80 放弃。"""
        r = self._rules
        if prev < r["grudge_threshold"] <= new and blocker_id is not None:
            await agg.apply_relation_delta(
                a_id=agent_id, b_id=blocker_id,
                delta_affinity=int(r["grudge_affinity_delta"]), delta_tension=int(r["grudge_tension_delta"]), cause=cause,
            )
            if self._cooldown is not None:
                await self._cooldown.set_grudge(agent_id=agent_id, target=blocker_id, sim_now=sim_now)
        if prev < r["replan_threshold"] <= new:
            await self._pool.execute(
                "UPDATE goals SET goal = goal || '（换策略：降级改写，01 §3.3）' WHERE id=$1", goal_row["id"],
            )
            await self._reflect_once(agent_id, f"目标受阻屡屡不成，换个策略：{goal_row['goal']}", importance=5)
        if prev < r["abandon_threshold"] <= new:
            await self._pool.execute("UPDATE goals SET status='abandoned', frustration=$2 WHERE id=$1", goal_row["id"], r["abandon_reset_to"])
            await self._reflect_once(
                agent_id, f"我放弃了：{goal_row['goal']}。屡屡受挫之后，承认这件事现在做不到。",
                importance=int(r["abandon_reflection_importance"]),
            )

    async def _reflect_once(self, agent_id: str, content: str, *, importance: int) -> None:
        if self._reflect is not None:
            await self._reflect(agent_id, content, importance=importance)

    # ---- 挫败值恢复（每模拟日 -4；A 级正向事件额外 -10 接口预留 T-ADJ-07） -------------

    async def daily_recovery(self, *, agent_id: str | None = None) -> int:
        """自然恢复：active 目标挫败值 -4（下限 0）；返回受影响行数。"""
        return await self._pool.fetchval(
            """
            WITH u AS (UPDATE goals SET frustration=GREATEST(0, frustration-$1)
                       WHERE status='active' AND frustration>0 AND ($2::text IS NULL OR agent_id=$2)
                       RETURNING id)
            SELECT count(*) FROM u
            """,
            int(self._rules["daily_recovery"]), agent_id,
        )

    async def a_level_recovery(self, *, agent_id: str) -> None:
        """A 级正向事件额外恢复 -10（01 §3.3；调用方 = T-ADJ-07 grade 联动，波次 2b 接线）。"""
        await self._pool.execute(
            "UPDATE goals SET frustration=GREATEST(0, frustration-$1) WHERE agent_id=$2 AND status='active'",
            int(self._rules["a_level_recovery"]), agent_id,
        )

    # ---- 完成条件判定（P0 可编程子集，02 文档 D9） ------------------------------------

    async def check_completions(self, *, agent_id: str, sim_now: dt.datetime) -> list[tuple[int, str]]:
        """对 active 目标按库 check 判定（仅 P0 子集）；完成 → status='done'。返回 [(goals.id, 库 id)]。"""
        rows = await self._pool.fetch(
            "SELECT id, goal, sim_week FROM goals WHERE agent_id=$1 AND status='active'", agent_id,
        )
        done: list[tuple[int, str]] = []
        for row in rows:
            entry = self._by_text.get(row["goal"])
            if entry is None:
                continue
            check = entry.get("check") or {}
            if check.get("tier") != "P0":
                continue  # 判定不可编程子集 M1 标 P1 预留（02 文档 D9）
            if await self._check_one(agent_id, row["sim_week"], check):
                await self._pool.execute("UPDATE goals SET status='done' WHERE id=$1", row["id"])
                done.append((row["id"], entry["id"]))
        return done

    async def _check_one(self, agent_id: str, sim_week: int, check: dict[str, Any]) -> bool:
        kind = check["kind"]
        since = self.week_start(sim_week)
        if kind == "event_count":
            payload_filter = check.get("payload_equals")
            return await self._pool.fetchval(
                """
                SELECT count(*) FROM events
                WHERE type=$1 AND $2=ANY(actors) AND sim_time >= $3
                  AND ($4::jsonb IS NULL OR payload @> $4::jsonb)
                """,
                check["event"], agent_id, since,
                json.dumps(payload_filter, ensure_ascii=False) if payload_filter else None,
            ) >= int(check["count"])
        if kind == "memory_count":
            return await self._pool.fetchval(
                "SELECT count(*) FROM memories WHERE agent_id=$1 AND kind=$2 AND sim_time >= $3",
                agent_id, check["memory_kind"], since,
            ) >= int(check["count"])
        if kind == "debt_cleared":
            return bool(
                await self._pool.fetchval(
                    "SELECT count(*) FROM debts WHERE b_id=$1 AND repaid_cents >= amount_cents", agent_id,
                )
            )
        return False
