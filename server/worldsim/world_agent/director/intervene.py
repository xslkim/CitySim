"""干预框架 L0~L2（04 T-DIR-03；01 §6.3 分级定义/超线只许 L0；源方案 §4.8 KPI；06 §1.1/§1.2）。

- **全部编剧动作唯一入口** `intervene(level, action, arc_id=?, reason, params)`：
  - L0 环境扰动 → 调用 T-WA-09/T-WA-05 扰动/公告/stock_shock 同一实现，仅 `trigger='director'` 不同
    （06 §1.1；事件本体即干预记录）；
  - L1 场景排期 → 排期原语（团建/加班加场/同厨同组同班标记，复用 T-WA-08 排期表；01 §11.2 发生器点火
    与 01 §9 调参手册入口）+ `world.company_crisis`（季度频次上限读 01 §6.1 口径，配置化）；
  - L2 动机助推 → 「世界观察」记忆注入（经 T-MEM `insert_memory` 唯一写径；tension 修复助推 =
    01 §6.3 指定的唯一合法阻尼路径——注入缓和观察、是否道歉仍由人格决策）。
- **L3 永禁**：本模块不暴露任何改关系/需求数值、强制台词的 API（01 §6.3）；数值结算只经 T-ADJ。
- 落库口径（D-17 防重复计数）：每个干预动作**恰一条** `trigger='director'` 事件（L0=world.* 事件
  本体；L1/L2=`director.intervene`，payload `{level, arc_id?, reason}` 逐字 06 §1.2）+ 一条
  `interventions` 行（`event_seq` 互指，04 §5.2）。L1 排期产生的下游世界事件 trigger='world'
  且 `payload.caused_by`=干预事件 seq（不计入干预率分子，防双计）。
- 干预率滑窗：`intervention_rate_7d()` 与审计④（04 §10.1 ④）同 SQL 口径同数据源；上限读
  `config/models.yaml` `thresholds.intervention_rate_cap`（01 T-CFG-02 唯一载体）；超线 L1/L2 拒绝、
  仅放行 L0（01 §6.3）。弧线配额：`intervention_budget` 耗尽即拒（预算数据经 world_state 共享，
  04 §3 依赖环澄清）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from ..calendar import CalendarEngine, queue_extra_overtime
from ..disturb import fire_complaint, fire_illness, fire_lucky, fire_weather
from ..economy import queue_stock_shock

log = logging.getLogger(__name__)

LEVELS = ("L0", "L1", "L2")  # L3 永禁（01 §6.3）——枚举内根本不存在

COMPANY_CRISIS_KEY = "company_crisis.last_at"   # 季度频次上限状态（01 §6.1：季度 ≤1）
CO_SCHEDULE_KEY = "schedule.co_location"        # 同厨/同组/同班排期标记（02 决策侧消费，D-29）


class InterventionError(RuntimeError):
    """干预拒绝基类。"""


class RateCapLockdown(InterventionError):
    """干预率 7 日滑窗超上限：L1/L2 拒绝、仅放行 L0（01 §6.3）。"""


class BudgetExhausted(InterventionError):
    """弧线 intervention_budget 配额耗尽（01 §6.2；拒绝方 = 本模块，R3 裁定）。"""


class UnknownAction(InterventionError):
    pass


async def intervention_rate_7d(pool: Any, sim_now: dt.datetime) -> float:
    """干预率 = 干预事件数 / 总事件数，滚动 7 模拟日滑窗（04 §10.1 ④ 同口径同数据源：
    干预审计只看 trigger 字段，00 §4 红线 13）。"""
    row = await pool.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE trigger='director')::float AS d, COUNT(*)::float AS t
        FROM events WHERE sim_time > $1
        """, sim_now - dt.timedelta(days=7))
    total = float(row["t"])
    return float(row["d"]) / total if total > 0 else 0.0


class InterventionFramework:
    """编剧干预唯一入口。`cal` = CalendarEngine（WA 实现复用）；`event_lod` 可选（L1 点名升格）。"""

    def __init__(self, pool: Any, cal: CalendarEngine, clock: Any, *, arcs_cfg: dict[str, Any],
                 intervention_rate_cap: float, event_lod: Any = None, gateway: Any = None) -> None:
        self._pool = pool
        self._cal = cal
        self._clock = clock
        self._cap = float(intervention_rate_cap)
        self._event_lod = event_lod
        self._gateway = gateway
        self._arc_templates = {a["arc_id"]: a for a in (arcs_cfg or {}).get("arcs", [])}

    # ---- 主入口 ---------------------------------------------------------------

    async def intervene(self, level: str, action: str, *, arc_id: str | None = None,
                        reason: str = "", params: dict[str, Any] | None = None,
                        sim_now: dt.datetime | None = None) -> int:
        """唯一干预入口。返回 trigger='director' 事件 seq。L3 在类型系统外（枚举拒绝）。"""
        if level not in LEVELS:
            raise InterventionError(f"level {level!r} 非法：干预分级 L0~L2，L3 永禁（01 §6.3）")
        now = sim_now or self._clock.now_sim()
        params = dict(params or {})
        # 超线只许 L0（01 §6.3；与审计④同数据源同阈值配置源）
        if level != "L0" and await self.intervention_rate_7d(now) >= self._cap:
            raise RateCapLockdown(
                f"干预率 7 日滑窗超上限（{self._cap}，models.yaml thresholds）：{level} 拒绝，仅放行 L0")
        # 弧线配额先扣（01 §6.2；L0 不占配额——intervention_budget 只列 L1/L2）
        if arc_id is not None:
            await self._consume_budget(arc_id, level)
        if level == "L0":
            seq = await self._exec_l0(action, now, params)
        elif level == "L1":
            seq = await self._exec_l1(action, now, params, arc_id=arc_id, reason=reason)
        else:
            seq = await self._exec_l2(action, now, params, arc_id=arc_id, reason=reason)
        await self._insert_intervention_row(level, arc_id, reason, params, seq, now)
        log.warning("干预落库：%s %s arc=%s seq=%d reason=%s", level, action, arc_id, seq, reason)
        return seq

    async def intervention_rate_7d(self, sim_now: dt.datetime | None = None) -> float:
        return await intervention_rate_7d(self._pool, sim_now or self._clock.now_sim())

    # ---- 弧线配额（world_state 共享，DIR-01 写入 DIR-03 读取校验，04 §3 澄清） --------

    async def _consume_budget(self, arc_id: str, level: str) -> None:
        tpl = self._arc_templates.get(arc_id)
        inst = await self._cal.get_state(f"arc.inst.{arc_id}")
        if tpl is None or inst is None:
            raise BudgetExhausted(f"弧线 {arc_id!r} 无模板或无活跃实例，配额不可扣")
        quota = int((tpl.get("intervention_budget") or {}).get(level, 0))
        used = int(inst.get("budget_used", {}).get(level, 0))
        if level != "L0" and used >= quota:
            raise BudgetExhausted(f"{arc_id} 配额耗尽：{level} {used}/{quota}（01 §6.2 intervention_budget）")
        inst.setdefault("budget_used", {})[level] = used + 1
        await self._cal.set_state(f"arc.inst.{arc_id}", inst)

    # ---- L0：WA 扰动/公告/股价冲击（事件本体 = 干预记录） ----------------------------

    async def _exec_l0(self, action: str, now: dt.datetime, params: dict[str, Any]) -> int:
        cal = self._cal
        if action == "announce":
            shock = params.get("stock_shock")
            payload = {"title": str(params["title"]), "body": str(params["body"]),
                       "scope": str(params.get("scope", "all"))}
            if shock:
                payload["stock_shock"] = shock
            seq = await cal.insert_event(
                type_="world.announce", payload=payload, sim_time=now, trigger="director",
                rng_seed=self._clock.tick_of(now))
            if shock:  # 编剧发起股价冲击：trigger='director' 计干预率（本事件即记录），下一 tick 生效
                await queue_stock_shock(cal, float(shock))
            return seq
        if action == "disturb_complaint":
            return await fire_complaint(cal, now, floor=int(params["floor"]),
                                        issue=str(params["issue"]), trigger="director")
        if action == "disturb_weather":
            return await fire_weather(cal, now, kind=str(params["kind"]), trigger="director")
        if action == "disturb_lucky":
            return await fire_lucky(cal, now, agent_id=str(params["agent_id"]),
                                    kind=str(params["kind"]),
                                    amount_cents=int(params["amount_cents"]), trigger="director")
        if action == "disturb_illness":
            return await fire_illness(cal, now, agent_id=str(params["agent_id"]),
                                      severity=int(params["severity"]), days=int(params["days"]),
                                      trigger="director")
        if action == "stock_shock":
            await queue_stock_shock(cal, float(params["pct"]), symbol=params.get("symbol"))
            return await cal.insert_event(
                type_="world.announce",
                payload={"title": str(params.get("title", "市场快讯")),
                         "body": str(params.get("body", "股价异动")),
                         "scope": str(params.get("scope", "company")),
                         "stock_shock": float(params["pct"])},
                sim_time=now, trigger="director", rng_seed=self._clock.tick_of(now))
        raise UnknownAction(f"L0 未知动作 {action!r}")

    # ---- L1：场景排期（director.intervene 落库 + 排期原语副作用） ---------------------

    async def _exec_l1(self, action: str, now: dt.datetime, params: dict[str, Any], *,
                       arc_id: str | None, reason: str) -> int:
        cal = self._cal
        known = ("schedule_overtime", "team_building", "company_crisis",
                 "co_kitchen", "co_project", "duty_shift", "nominate")
        if action not in known:
            raise UnknownAction(f"L1 未知动作 {action!r}（可选 {known}）")
        if action == "company_crisis":  # 季度至多 1 次（01 §6.1）——先校验再落事件
            last = await cal.get_state(COMPANY_CRISIS_KEY)
            if last and (now - dt.datetime.fromisoformat(str(last))).days < 90:
                raise InterventionError("world.company_crisis 季度频次上限（01 §6.1）")
        if action == "nominate" and self._event_lod is None:
            raise InterventionError("nominate 需要 EventDrivenLOD 接线（T-LOD-02）")
        seq = await self._director_event("L1", arc_id, reason, now)
        if action == "schedule_overtime":  # 加班加场（复用 T-WA-08 排期表；下游事件 trigger='world'）
            await queue_extra_overtime(cal, dt.date.fromisoformat(str(params["date"])),
                                       str(params["dept"]), str(params.get("reason", "编剧排期")))
        elif action == "team_building":
            from ..calendar import settle_team_building

            await settle_team_building(cal, now, activity=params.get("activity"),
                                       charge_cents=params.get("charge_cents"), trigger="world")
        elif action == "company_crisis":
            await cal.set_state(COMPANY_CRISIS_KEY, now.isoformat())
            await cal.insert_event(
                type_="world.company_crisis",
                payload={"scope": str(params.get("scope", "company")),
                         "severity": str(params.get("severity", "medium")),
                         "caused_by": str(seq)},
                sim_time=now, trigger="world", rng_seed=self._clock.tick_of(now))
        elif action in ("co_kitchen", "co_project", "duty_shift"):
            # 同厨时段/同项目组/值班同班：排期标记（02 决策侧消费；工程默认载体 D-29）
            marks = list(await cal.get_state(CO_SCHEDULE_KEY, []) or [])
            marks.append({"kind": action, "agents": [str(a) for a in params.get("agents", [])],
                          "at": now.isoformat(), "caused_by": str(seq)})
            await cal.set_state(CO_SCHEDULE_KEY, marks)
        else:  # nominate：编剧点名直达 star（04 §4.2 路径二③）
            await self._event_lod.nominate(agent_id=str(params["agent_id"]),
                                           tick=self._clock.tick_of(now), sim_now=now,
                                           caused_by=str(seq))
        return seq

    # ---- L2：世界观察记忆注入（tension 修复助推 = 唯一合法阻尼路径，01 §6.3） -----------

    async def _exec_l2(self, action: str, now: dt.datetime, params: dict[str, Any], *,
                       arc_id: str | None, reason: str) -> int:
        if action not in ("memory_inject", "tension_repair"):
            raise UnknownAction(f"L2 未知动作 {action!r}")
        if self._gateway is None:
            raise InterventionError("L2 记忆注入需要 LLMGateway（embedding 经 T-MEM insert_memory 唯一写径）")
        from ...memory.store import insert_memory

        if action == "memory_inject":
            content = str(params["content"])
            agent_id = str(params["agent_id"])
        else:  # tension_repair
            # 向高 tension 边一方注入缓和观察（01 §6.3 评审 P1-9 指定路径；不碰任何数值）
            agent_id = str(params["agent_id"])
            other = str(params["other"])
            content = f"听说{other}最近提起你时语气缓和了不少，也许他那天也是无心的。"
        seq = await self._director_event("L2", arc_id, reason, now)
        await insert_memory(self._pool, self._gateway, agent_id=agent_id, sim_time=now,
                            kind="event", content=content,
                            importance=int(params.get("importance", 5)),
                            source_event_seq=seq, rng_seed=self._clock.tick_of(now))
        return seq

    # ---- 落库（D-17：恰一条 trigger='director' 事件 + 一条 interventions 行互指） --------

    async def _director_event(self, level: str, arc_id: str | None, reason: str,
                              now: dt.datetime) -> int:
        payload: dict[str, Any] = {"level": level, "reason": reason}
        if arc_id is not None:
            payload["arc_id"] = arc_id
        return await self._cal.insert_event(
            type_="director.intervene", payload=payload, sim_time=now,
            source="director", trigger="director", arc_id=arc_id,
            rng_seed=self._clock.tick_of(now))

    async def _insert_intervention_row(self, level: str, arc_id: str | None, reason: str,
                                       params: dict[str, Any], event_seq: int,
                                       now: dt.datetime) -> int:
        return await self._pool.fetchval(
            """
            INSERT INTO interventions (sim_time, level, arc_id, reason, payload, event_seq)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6) RETURNING id
            """, now, level, arc_id, reason, json.dumps(params, ensure_ascii=False), event_seq)
