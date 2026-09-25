"""弧线状态机引擎（04 T-DIR-01；01 §6.2 schema/并发上限/冷却/fail-forward；01 §6.4 无 A 级自动启弧线）。

- `config/arcs.yaml` = 声明式弧线模板唯一载体（schema 逐字段承接 01 §6.2：`arc_id/title/
  stage_machine/fail_forward/intervention_budget/actors/min_days/max_days/payoff_beat`；
  `payoff_beat.type` 限 01 §11.3 八类枚举）。
- 条件 DSL = 预注册 predicate 表 + YAML `{pred, args}` 声明式引用，**禁自由文本**（工程默认 D-16）。
- 实例状态/并发/冷却持久化 `world_state`（DDL 无弧线表，D-06）：`arc.inst.<arc_id>`、
  `arcs.active`、`arc.cooldown.<arc_id>`、`arcs.audit_log`；关联事件经 `events.arc_id`（04 §5.2）。
- 弧线发起干预先扣 `intervention_budget`，统一经 T-DIR-03 `intervene()` 落库（注入 seam；
  配额校验拒绝方 = T-DIR-03，预算数据经 world_state 共享，04 §3 依赖环澄清）。
- 每日检查（`daily_check`）：连续无 A 级达阈值（01 §6.4，阈值读 arcs.yaml meta）→ 自动启动
  休眠弧线（优先久未推进者）并落审计日志。
- batch 段协调：`daily_check`/`tick` 均可作 `batch_advance` 的 `director_preempt` 回调（04 §3.3）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Awaitable, Callable

import yaml
from pydantic import BaseModel, ValidationError, field_validator

log = logging.getLogger(__name__)

# 01 §11.3 爽点八类枚举（封闭集）
PAYOFF_TYPES = ("打脸", "逆袭", "告白成功", "真相大白", "误会解除", "复仇", "神反转", "吃瓜围观")

ACTIVE_KEY = "arcs.active"
AUDIT_LOG_KEY = "arcs.audit_log"


class ArcSchemaError(ValueError):
    """arcs.yaml schema 校验失败（拒绝加载）。"""


# ---- schema（pydantic；数值/枚举唯一持有方 = 01 §6.2/§11.3，此处只验结构） -------------


class CondSpec(BaseModel):
    pred: str
    args: dict[str, Any] = {}


class StageSpec(BaseModel):
    stage: str
    exit: CondSpec | None = None      # 末态可无 exit（收尾驻留）
    hooks: list[dict[str, Any]] = []  # 进入钩子：[{intervene: {level, action, params?}}]


class FailForwardSpec(BaseModel):
    at: str
    if_blocked: CondSpec
    degrade_to: str                    # 目标 stage 或 'done'


class PayoffBeat(BaseModel):
    type: str
    setup_days: tuple[int, int]
    burst_event: str
    aftermath: str = ""

    @field_validator("type")
    @classmethod
    def _payoff_enum(cls, v: str) -> str:
        if v not in PAYOFF_TYPES:
            raise ValueError(f"payoff_beat.type {v!r} 不在 01 §11.3 八类枚举内")
        return v

    @field_validator("setup_days")
    @classmethod
    def _setup_ordered(cls, v: tuple[int, int]) -> tuple[int, int]:
        if len(v) != 2 or v[0] < 1 or v[0] > v[1]:
            raise ValueError("payoff_beat.setup_days 须为 [min, max] 且 1≤min≤max（01 §11.3）")
        return v


class ArcTemplate(BaseModel):
    arc_id: str
    title: str
    stage_machine: list[StageSpec]
    fail_forward: list[FailForwardSpec]      # 必填（验收 1：缺 fail_forward 拒绝加载）
    intervention_budget: dict[str, int]      # {L1: n, L2: n}（L0 不限额，01 §6.2）
    actors: dict[str, list[str]] = {"main": [], "support": []}
    min_days: int
    max_days: int
    payoff_beat: PayoffBeat

    @field_validator("stage_machine")
    @classmethod
    def _stages_nonempty(cls, v: list[StageSpec]) -> list[StageSpec]:
        if len(v) < 2:
            raise ValueError("stage_machine 至少两个状态（铺垫→爆发）")
        return v

    @field_validator("intervention_budget")
    @classmethod
    def _budget_levels(cls, v: dict[str, int]) -> dict[str, int]:
        for k in v:
            if k not in ("L0", "L1", "L2"):
                raise ValueError(f"intervention_budget 级别 {k!r} 非法（L3 永禁，01 §6.3）")
            if int(v[k]) < 0:
                raise ValueError("intervention_budget 不得为负")
        return v


class ArcsFile(BaseModel):
    meta: dict[str, Any]
    arcs: list[ArcTemplate]


def load_arcs(path: str | None = None) -> dict[str, Any]:
    """加载并 schema 校验 arcs.yaml；失败抛 ArcSchemaError（拒绝启动口径同 T-WA-01）。"""
    from pathlib import Path

    p = Path(path) if path else Path(__file__).resolve().parents[3] / "config" / "arcs.yaml"
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise ArcSchemaError(f"arcs.yaml 读取失败：{p}（{e}）") from e
    try:
        parsed = ArcsFile.model_validate(raw)
    except ValidationError as e:
        raise ArcSchemaError(f"arcs.yaml schema 校验失败：\n{e}") from e
    # predicate 白名单校验（D-16：YAML 只许 {pred, args} 声明式引用预注册 predicate）
    for tpl in parsed.arcs:
        conds = [s.exit for s in tpl.stage_machine if s.exit] + [f.if_blocked for f in tpl.fail_forward]
        for c in conds:
            if c.pred not in PREDICATES:
                raise ArcSchemaError(f"{tpl.arc_id} 引用未注册 predicate {c.pred!r}（D-16 白名单）")
        stage_names = {s.stage for s in tpl.stage_machine}
        for f in tpl.fail_forward:
            if f.at not in stage_names:
                raise ArcSchemaError(f"{tpl.arc_id} fail_forward.at={f.at!r} 不在 stage_machine 内")
            if f.degrade_to != "done" and f.degrade_to not in stage_names:
                raise ArcSchemaError(f"{tpl.arc_id} fail_forward.degrade_to={f.degrade_to!r} 非法")
    return raw


# ---- predicate 表（D-16；async fn(engine, args) -> bool，只读 DB/配置） -----------------


async def _pred_count_events(eng: "ArcEngine", args: dict[str, Any]) -> bool:
    """{type, within_days?, min, actor?}：窗口内事件计数 ≥ min。"""
    q = "SELECT count(*) FROM events WHERE type=$1"
    params: list[Any] = [args["type"]]
    if args.get("within_days"):
        q += f" AND sim_time > ${len(params) + 1}"
        params.append(eng._clock.now_sim() - dt.timedelta(days=int(args["within_days"])))
    if args.get("actor"):
        q += f" AND ${len(params) + 1} = ANY(actors)"
        params.append(args["actor"])
    n = await eng._pool.fetchval(q, *params)
    return int(n) >= int(args.get("min", 1))


async def _pred_relation_delta(eng: "ArcEngine", args: dict[str, Any]) -> bool:
    """{a, b, affinity_below?/affinity_above?/tension_above?}：关系边现状判定。"""
    row = await eng._pool.fetchrow(
        "SELECT affinity, tension FROM relations WHERE a_id=$1 AND b_id=$2", args["a"], args["b"])
    aff, ten = (int(row["affinity"]), int(row["tension"])) if row else (0, 0)
    if "affinity_below" in args and not aff < int(args["affinity_below"]):
        return False
    if "affinity_above" in args and not aff > int(args["affinity_above"]):
        return False
    if "tension_above" in args and not ten > int(args["tension_above"]):
        return False
    return True


async def _pred_need_below(eng: "ArcEngine", args: dict[str, Any]) -> bool:
    """{agent, need, below}：需求缓存值低于阈值（含 wealth——A4 债主资金紧张口径）。"""
    needs = await eng._pool.fetchval("SELECT needs FROM agents WHERE id=$1", args["agent"])
    needs = json.loads(needs) if isinstance(needs, str) else dict(needs or {})
    return float(needs.get(args["need"], 0.0)) < float(args["below"])


async def _pred_debt_overdue(eng: "ArcEngine", args: dict[str, Any]) -> bool:
    """{}：存在逾期未结清债务（A4 点火条件；debts 04 §5.2）。"""
    n = await eng._pool.fetchval(
        "SELECT count(*) FROM debts WHERE due_sim < $1 AND repaid_cents < amount_cents",
        eng._clock.now_sim())
    return int(n) > 0


async def _pred_frustration_above(eng: "ArcEngine", args: dict[str, Any]) -> bool:
    """{agent, above}：挫败值超阈（A6 连续挫败口径，goals.frustration）。"""
    n = await eng._pool.fetchval(
        "SELECT coalesce(max(frustration), 0) FROM goals WHERE agent_id=$1 AND status='active'",
        args["agent"])
    return int(n) >= int(args["above"])


async def _pred_stage_age_days(eng: "ArcEngine", args: dict[str, Any], _inst: dict | None = None) -> bool:
    """{min}：当前 stage 驻留 ≥ min 模拟日（if_blocked 时间维兜底）。"""
    inst = _inst or {}
    entered = dt.datetime.fromisoformat(str(inst.get("stage_entered_at", inst.get("started_at"))))
    return (eng._clock.now_sim() - entered).days >= int(args["min"])


PREDICATES: dict[str, Callable[..., Awaitable[bool]]] = {
    "count_events": _pred_count_events,
    "relation_delta": _pred_relation_delta,
    "need_below": _pred_need_below,
    "debt_overdue": _pred_debt_overdue,
    "frustration_above": _pred_frustration_above,
    "stage_age_days": _pred_stage_age_days,
}


# ---- 运行时 -------------------------------------------------------------------

InterveneFn = Callable[..., Awaitable[int]]
"""T-DIR-03 统一入口签名：intervene(level, action, arc_id=?, reason, params) → event seq。"""


class ArcEngine:
    """弧线状态机运行时。`intervene` 注入 T-DIR-03 唯一入口（M3 接线前可传 None=纯状态机）。"""

    def __init__(self, pool: Any, arcs_cfg: dict[str, Any], clock: Any, *,
                 intervene: InterveneFn | None = None) -> None:
        self._pool = pool
        self._cfg = arcs_cfg
        self._clock = clock
        self._intervene = intervene
        self._templates = {a["arc_id"]: a for a in arcs_cfg.get("arcs", [])}
        meta = arcs_cfg.get("meta", {})
        self._concurrency_cap = int(meta.get("concurrency_cap", 3))      # 活跃弧线 ≤3（01 §6.2）
        self._cooldown_days = int(meta.get("cooldown_days", 14))         # 同模板冷却 ≥2 模拟周
        self._a_drought_days = int(meta.get("a_drought_days", 2))        # 连续无 A 级阈值（01 §6.4）

    # ---- world_state 持久化（D-06） ---------------------------------------------

    async def _get(self, key: str, default: Any = None) -> Any:
        row = await self._pool.fetchrow("SELECT value FROM world_state WHERE key=$1", key)
        if row is None:
            return default
        v = row["value"]
        return json.loads(v) if isinstance(v, str) else v

    async def _set(self, key: str, value: Any) -> None:
        await self._pool.execute(
            """
            INSERT INTO world_state (key, value, updated_tick) VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_tick=$3, updated_at=now()
            """, key, json.dumps(value, ensure_ascii=False, default=str), self._clock.current_tick)

    async def _audit(self, action: str, arc_id: str, detail: dict[str, Any] | None = None) -> None:
        log_ = list(await self._get(AUDIT_LOG_KEY, []) or [])
        log_.append({"at": self._clock.now_sim().isoformat(), "action": action, "arc_id": arc_id,
                     **(detail or {})})
        await self._set(AUDIT_LOG_KEY, log_)
        log.info("弧线审计：%s %s %s", action, arc_id, detail or "")

    # ---- 生命周期 ---------------------------------------------------------------

    async def active_instances(self) -> list[dict[str, Any]]:
        return list(await self._get(ACTIVE_KEY, []) or [])

    async def get_instance(self, arc_id: str) -> dict[str, Any] | None:
        return await self._get(f"arc.inst.{arc_id}")

    async def start_arc(self, arc_id: str, *, reason: str = "manual") -> dict[str, Any] | None:
        """启动弧线实例：并发上限排队（返回 None+排队审计）/冷却期内同模板拒绝重启（01 §6.2）。"""
        if arc_id not in self._templates:
            raise KeyError(f"弧线模板 {arc_id!r} 不在 arcs.yaml")
        now = self._clock.now_sim()
        active = await self.active_instances()
        if any(i["arc_id"] == arc_id for i in active):
            return None  # 已在活跃
        cooldown = await self._get(f"arc.cooldown.{arc_id}")
        if cooldown and dt.datetime.fromisoformat(str(cooldown)) > now:
            await self._audit("start_rejected_cooldown", arc_id, {"until": cooldown})
            return None
        if len(active) >= self._concurrency_cap:
            await self._audit("queued_over_concurrency", arc_id, {"active": len(active)})
            return None  # 排队（下次 daily_check 再试，01 §6.2 超过即排队）
        tpl = self._templates[arc_id]
        inst = {
            "arc_id": arc_id, "stage": tpl["stage_machine"][0]["stage"],
            "started_at": now.isoformat(), "stage_entered_at": now.isoformat(),
            "budget_used": {"L0": 0, "L1": 0, "L2": 0}, "reason": reason,
            "last_progress_at": now.isoformat(), "status": "active",
        }
        await self._set(f"arc.inst.{arc_id}", inst)
        active.append({"arc_id": arc_id, "started_at": now.isoformat()})
        await self._set(ACTIVE_KEY, active)
        await self._audit("start", arc_id, {"reason": reason})
        await self._run_hooks(inst, tpl["stage_machine"][0])
        return inst

    async def _run_hooks(self, inst: dict[str, Any], stage: dict[str, Any]) -> None:
        for hook in stage.get("hooks", []):
            spec = hook.get("intervene")
            if spec is None:
                raise ArcSchemaError(f"{inst['arc_id']} hook 仅支持 intervene 形态（统一经 T-DIR-03 落库）")
            if self._intervene is None:
                log.warning("弧线 %s hook 跳过：intervene 未接线", inst["arc_id"])
                continue
            await self._intervene(
                spec["level"], spec["action"], arc_id=inst["arc_id"],
                reason=spec.get("reason", f"{inst['arc_id']} {stage['stage']} 钩子"),
                params=spec.get("params", {}))

    async def tick(self, sim_now: dt.datetime | None = None) -> None:
        """每 tick（或 batch 段插队）评估全部活跃弧线：exit 条件推进 / fail_forward / max_days 收尾。"""
        now = sim_now or self._clock.now_sim()
        for ref in await self.active_instances():
            inst = await self.get_instance(ref["arc_id"])
            if not inst or inst.get("status") != "active":
                continue
            await self._eval_instance(inst, now)

    @staticmethod
    def _setup_window(tpl: dict[str, Any]) -> tuple[int, int]:
        """铺垫区间：payoff_beat.setup_days（01 §11.3 爽点节拍权威）为操作口径；
        min_days/max_days（01 §6.2）为其部署镜像。"""
        sd = tpl["payoff_beat"]["setup_days"]
        return int(sd[0]), int(sd[1])

    async def _eval_instance(self, inst: dict[str, Any], now: dt.datetime) -> None:
        tpl = self._templates[inst["arc_id"]]
        stages = tpl["stage_machine"]
        idx = next(i for i, s in enumerate(stages) if s["stage"] == inst["stage"])
        stage = stages[idx]
        min_days, max_days = self._setup_window(tpl)
        days_active = (now - dt.datetime.fromisoformat(str(inst["started_at"]))).days
        # max_days 强制 fail-forward 收尾（01 §6.2：超 max_days 未推进 → 强制收尾，防死锁 R4）
        if days_active > max_days:
            await self._force_close(inst, tpl, reason="max_days_exceeded")
            return
        # exit 优先于 fail_forward：exit 条件满足 = 未阻塞（if_blocked 语义，01 §6.2）
        if stage.get("exit") and await self._eval_cond(stage["exit"], inst):
            is_last = idx == len(stages) - 1
            if is_last:
                await self._complete(inst, tpl, now)
            elif days_active >= min_days or idx < len(stages) - 2:
                # 中间 stage 自由推进；进入末态前须过 min_days 铺垫门禁（01 §11.3 铺垫不足不爽）
                await self._jump(inst, stages, stages[idx + 1]["stage"], now, via="advance")
            return
        # fail_forward：当前 stage 有降级分支且阻塞条件成立 → degrade_to
        ff = next((f for f in tpl["fail_forward"] if f["at"] == inst["stage"]), None)
        if ff is not None and await self._eval_cond(ff["if_blocked"], inst):
            await self._jump(inst, stages, ff["degrade_to"], now, via="fail_forward")
            return

    async def _eval_cond(self, cond: dict[str, Any], inst: dict[str, Any]) -> bool:
        fn = PREDICATES[cond["pred"]]
        if cond["pred"] == "stage_age_days":
            return await fn(self, cond.get("args", {}), _inst=inst)
        return await fn(self, cond.get("args", {}))

    async def _jump(self, inst: dict[str, Any], stages: list[dict], target: str, now: dt.datetime,
                    *, via: str) -> None:
        if target == "done":
            await self._audit(via, inst["arc_id"], {"to": "done"})
            await self._complete(inst, self._templates[inst["arc_id"]], now, via=via)
            return
        inst["stage"] = target
        inst["stage_entered_at"] = now.isoformat()
        inst["last_progress_at"] = now.isoformat()
        await self._set(f"arc.inst.{inst['arc_id']}", inst)
        await self._audit(via, inst["arc_id"], {"to": target})
        stage = next(s for s in stages if s["stage"] == target)
        await self._run_hooks(inst, stage)

    async def _complete(self, inst: dict[str, Any], tpl: dict[str, Any], now: dt.datetime,
                        *, via: str = "advance") -> None:
        inst["status"] = "done"
        inst["completed_at"] = now.isoformat()
        inst["via"] = via
        await self._set(f"arc.inst.{inst['arc_id']}", inst)
        active = [i for i in await self.active_instances() if i["arc_id"] != inst["arc_id"]]
        await self._set(ACTIVE_KEY, active)
        # 同模板冷却（01 §6.2：≥2 模拟周不得换皮重演）
        until = now + dt.timedelta(days=self._cooldown_days)
        await self._set(f"arc.cooldown.{inst['arc_id']}", until.isoformat())
        await self._audit("complete", inst["arc_id"], {"via": via, "cooldown_until": until.isoformat()})

    async def _force_close(self, inst: dict[str, Any], tpl: dict[str, Any], *, reason: str) -> None:
        """超 max_days 强制 fail-forward 收尾：沿 degrade_to 链走到 done（全程落审计，01 §6.2）。"""
        ff = next((f for f in tpl["fail_forward"] if f["at"] == inst["stage"]), None)
        target = ff["degrade_to"] if ff else "done"
        await self._audit("force_fail_forward", inst["arc_id"], {"from": inst["stage"], "reason": reason})
        await self._jump(inst, tpl["stage_machine"], target, self._clock.now_sim(), via="fail_forward")
        if target != "done":  # degrade 到中间态后立即按收尾处理（防二次驻留死锁）
            inst2 = await self.get_instance(inst["arc_id"])
            if inst2 and inst2.get("status") == "active":
                await self._complete(inst2, tpl, self._clock.now_sim(), via="fail_forward")

    # ---- 每日检查：连续无 A 级自动启弧线（01 §6.4） ---------------------------------

    async def a_drought_days(self) -> int:
        """连续无 A 级模拟日数（有效 grade 口径 04 §6.6：ui.grade ⊕ 最新 grade_revise）。"""
        now = self._clock.now_sim()
        days = 0
        d = now.date()
        while True:
            n = await self._pool.fetchval(
                """
                SELECT count(*) FROM events e
                WHERE e.sim_time::date = $1 AND coalesce(
                  (SELECT payload->>'new_grade' FROM events r
                    WHERE r.type='director.grade_revise' AND r.payload->>'target_seq' = e.seq::text
                    ORDER BY r.seq DESC LIMIT 1), e.ui->>'grade') = 'A'
                """, d)
            if int(n) > 0:
                break
            days += 1
            d -= dt.timedelta(days=1)
            if days > 30:
                break  # 兜底防全空库死循环
        return days

    async def daily_check(self) -> dict[str, Any] | None:
        """连续无 A 级达阈值 → 自动启动休眠弧线（优先久未推进者），落审计日志（01 §6.4）。"""
        if await self.a_drought_days() < self._a_drought_days:
            return None
        active_ids = {i["arc_id"] for i in await self.active_instances()}
        now = self._clock.now_sim()
        candidates: list[tuple[str, dt.datetime]] = []
        for arc_id in self._templates:
            if arc_id in active_ids:
                continue
            cooldown = await self._get(f"arc.cooldown.{arc_id}")
            if cooldown and dt.datetime.fromisoformat(str(cooldown)) > now:
                continue
            inst = await self.get_instance(arc_id)
            last = dt.datetime.fromisoformat(str(inst["last_progress_at"])) if inst else dt.datetime.min.replace(tzinfo=now.tzinfo)
            candidates.append((arc_id, last))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1])  # 久未推进者优先
        picked = candidates[0][0]
        inst = await self.start_arc(picked, reason="a_drought_auto")
        if inst:
            await self._audit("auto_start_on_a_drought", picked, {"drought_days": await self.a_drought_days()})
        return inst
