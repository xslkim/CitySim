"""19 动作校验器与执行结算（02 T-ADJ-03；04 §6.2 校验表 + 通用校验规则表、01 §4 动作空间唯一持有方、
01 §4.1 负向动作细则、06 §1.2/§1.4 事件类型与 payload 口径）。

收编口径（评审 R3 / 02 文档 v1.2.1）：`borrow_money`/`repay_money` 前置校验（未结清 `debts` 行存在性、
额度、冷却）与 `debts` 行写入/核销收编本模块（六步管道 step5 动作结算，04 §5.2 设计口径）；
仅逾期日结算扫描归 04 T-WA-04（M3，02 文档 D2）。

结构：
- `check(obs, action_type, args)`：通用规则表（状态/冷却/可达性/时间占用/经济，04 §6.2）先行，
  然后逐动作前置校验（01 §4 前置校验列照抄）。非法 → (False, 原因)；管道侧写"想做 X 但被现实阻止"
  记忆（04 §6.1 step4，既有 `_write_blocked_memory`）。
- `settle(obs, decision, ...)`：事件落库（类型域归属 06 §1.4 末行）+ 缓存列结算（位置/余额/持仓
  只由系统改，00 §4 红线 6）+ 关系/需求结算（经 T-REL-01/02 引擎与 StateAggregator 并入聚合事件，
  04 §6.5）+ 记忆写入（T-ADJ-05 单源/目击投影原语）+ 对象唤醒入队（04 §2.2 事件唤醒）。
- 金额一律 `payload.amount_cents`（分，正入负出，06 §1.3）。

工程默认（登记 02 文档偏差表）：sim_cost 区间内取数经 rng_seed 确定性采样（D25）；"占用中"判定 =
进程内 busy 表（单裁决协程前提，D26）；borrow 成败由决策 args.result 携带（对方响应链路 M2 接，D27）；
borrow 的 affinity 阈读 B→A 边（放款人对借款人，D27）；私密场所 = 自己房间或天台（D27）；
"显式求助"判定 M1 预留（D27）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
from typing import Any, Awaitable, Callable

from ..invite.state_machine import InviteMachine
from ..memory import store
from ..relations.cooldown import CooldownEngine
from ..relations.needs import NeedsEngine
from ..relations.relations import RelationEngine
from ..scheduler.residence import ResidenceEngine, enterable_for_tenant
from .state_events import StateAggregator

log = logging.getLogger(__name__)

# 19 项动作枚举（01 §4 唯一定义处，逐字；04 §6.2 同序）
ACTIONS: tuple[str, ...] = (
    "move", "chat", "send_message", "invite", "work", "rest", "eat", "shop", "trade_stock", "think",
    "give_gift", "help", "borrow_money", "repay_money", "argue", "gossip", "refuse", "confess", "apologize",
)

# 动作 → 事件类型（06 §1.4 末行域归属）
EVENT_TYPE: dict[str, str] = {
    "move": "agent.move", "work": "agent.work", "rest": "agent.rest", "eat": "agent.eat",
    "shop": "agent.shop", "trade_stock": "agent.trade_stock", "think": "agent.think",
    "chat": "dialogue.chat", "argue": "dialogue.argue", "gossip": "dialogue.gossip",
    "confess": "dialogue.confess", "apologize": "dialogue.apologize",
    "send_message": "social.send_message", "invite": "social.invite", "give_gift": "social.give_gift",
    "help": "social.help", "borrow_money": "social.borrow_money", "repay_money": "social.repay_money",
    "refuse": "social.refuse",
}

# 有对象的动作 → 对象参数键（冷却/可达性/唤醒用）
_TARGET_KEY: dict[str, str] = {
    "chat": "target", "send_message": "target", "invite": "target", "give_gift": "target",
    "help": "target", "borrow_money": "target", "repay_money": "target", "argue": "target",
    "gossip": "target_listener", "refuse": "target", "confess": "target", "apologize": "target",
}

# sim_cost 区间（01 §4 sim_cost 列，单位模拟分钟；区间内取数经 rng_seed 确定性采样，D25）
SIM_COST_RANGE: dict[str, tuple[int, int]] = {
    "move": (5, 15), "chat": (15, 45), "send_message": (5, 5), "invite": (10, 10),
    "work": (30, 120), "rest": (30, 480), "eat": (20, 60), "shop": (30, 60), "trade_stock": (10, 10),
    "think": (10, 20), "give_gift": (15, 15), "help": (30, 90), "borrow_money": (15, 15),
    "repay_money": (10, 10), "argue": (15, 30), "gossip": (15, 30), "refuse": (5, 5),
    "confess": (30, 30), "apologize": (15, 15),
}

BORROW_MAX_CENTS = 200_000          # borrow 单笔上限 ¥2,000（01 §4；拓扑预设初始债务豁免，评审 P2-10）
DEBT_DUE_DAYS = 14                  # 还款期限 = 放款起 14 模拟日（01 §3.2/§4，06 §3 登记）
DEEP_CHAT_AFFINITY = 20             # 深聊需 affinity≥20（01 §4 chat 行）
CONFESS_AFFINITY = 40               # confess 需 affinity(A→B)≥40（01 §4）
BORROW_AFFINITY_MIN = 10            # borrow_money 需 affinity≥10（01 §4；读 B→A 边，D27）
ARGUE_TENSION_MIN = 20              # argue 门槛 tension≥20 或情绪≤30（01 §4.1）
ARGUE_MOOD_MAX = 30
APOLOGIZE_TENSION_MIN = 20          # apologize 需 tension≥20（01 §4）
DIALOGUE_BUSY_WINDOW_MIN = 45       # "target 未在对话"判定窗（chat sim_cost 上限；同 T-REL-05 D20 口径）
SHOP_STORES = ("便利店", "商超", "服饰店", "家居店", "书店", "礼品店")  # 01 §4 shop 行场所枚举（评审 P2-10）
PRIVATE_PLACE_KINDS = ("room", "apt.roof")  # 私密场所 = 自己房间或天台（工程默认，D27）
GIFT_CRUSH_LABEL = "暗恋"           # 288 档以上暗恋额外 +2（01 §3.2 礼物行）
N_HIGH = 70                         # Big Five 高档阈（01 §3.1 档位口径）

# eat venue → （价格键， 开放节点/时段）；开放口径：cook=厨房开放时段，其余全天（工程默认，D27）
EAT_VENUES = ("cook", "bento", "canteen", "restaurant", "coffee")

WakeupFn = Callable[[int, str], None]
DialogueSettleFn = Callable[..., Awaitable[list[int]]]


def sim_cost_min(action: str, rng_seed: int, agent_id: str) -> int:
    """sim_cost 区间内确定性取数（01 §4 sim_cost 列；D25）。"""
    lo, hi = SIM_COST_RANGE[action]
    if lo == hi:
        return lo
    return random.Random(f"simcost:{rng_seed}:{agent_id}:{action}").randint(lo, hi)


class ActionValidator:
    """19 动作校验 + step5 结算。引擎/配置注入；单实例仅运行于裁决协程（串行前提，00 §4 红线 10）。"""

    def __init__(
        self,
        pool: Any,
        *,
        world: dict[str, Any],
        needs_engine: NeedsEngine,
        cooldown: CooldownEngine,
        relations: RelationEngine,
        relations_cfg: dict[str, Any],
        agg: StateAggregator,
        gateway: Any = None,
        invite: InviteMachine | None = None,
        residence: ResidenceEngine | None = None,
        dialogue_settle: DialogueSettleFn | None = None,
        wakeup: WakeupFn | None = None,
        grader: Any = None,
    ) -> None:
        self._pool = pool
        self._world = world or {}
        self._needs = needs_engine
        self._cooldown = cooldown
        self._relations = relations
        self._action_rows = (relations_cfg or {}).get("action_rows", {})
        self._agg = agg
        self._gw = gateway
        self._invite = invite
        self._residence = residence
        self._dialogue_settle = dialogue_settle
        self._wakeup = wakeup
        self._grader = grader  # T-ADJ-07：grade 初值唯一实现挂接（None = 不打分）
        self._busy: dict[str, dt.datetime] = {}  # 占用表（进程内，D26）

    # ==================================================================
    # step4 校验（通用规则表 + 逐动作前置校验）
    # ==================================================================

    async def check(self, obs: Any, action_type: str, args: dict[str, Any]) -> tuple[bool, str | None]:
        if action_type not in ACTIONS:
            return False, f"未知动作 {action_type!r}（01 §4 全集 19 项）"
        sim_now = obs.sim_time
        # 通用规则① 状态（04 §6.2 状态行，P2-3 修正后口径：低值=匮乏）
        forced = self._needs.forced_action(obs.needs)
        if forced == "rest" and action_type != "rest":
            return False, "精力 <10：强制 rest（04 §6.2 状态行）"
        if forced == "eat" and action_type != "eat":
            return False, "饥饿 <10：强制 eat（04 §6.2 状态行）"
        # 通用规则② 冷却（intent_cooldown until_sim > now → 拦截，01 §3.4）
        target = args.get(_TARGET_KEY.get(action_type, ""))
        if isinstance(target, str):
            ok, reason = await self._cooldown.check_action(
                agent_id=obs.agent_id, action=action_type, target=target, sim_now=sim_now,
            )
            if not ok:
                return ok, reason
        # 通用规则③ 可达性
        ok, reason = await self._check_reachability(obs, action_type, args)
        if not ok:
            return ok, reason
        # 通用规则④ 时间/占用（sim_cost 执行期间不可被其他动作占用，01 §4；约定冲突 → 拒绝）
        busy_until = self._busy.get(obs.agent_id)
        if busy_until is not None and busy_until > sim_now:
            return False, f"动作执行中（占用至 {busy_until.isoformat()}，01 §4 sim_cost 行）"
        # 通用规则⑤ 经济（涉钱动作余额/持仓只由系统结算，06 §1.3）
        ok, reason = await self._check_economy(obs, action_type, args)
        if not ok:
            return ok, reason
        # 逐动作前置校验（01 §4 前置校验列照抄）
        return await self._check_action_specific(obs, action_type, args)

    async def _check_reachability(self, obs: Any, action_type: str, args: dict[str, Any]) -> tuple[bool, str | None]:
        sim_now = obs.sim_time
        if action_type == "move":
            target = args.get("to")
            if not isinstance(target, str) or not target:
                return False, "move 缺 args.to"
            if not enterable_for_tenant(target):
                return False, f"租客不可 move 进入 {target}（01 §1.6 可达性行）"
            if target == obs.position:
                return False, "已在目标节点"
            if target not in obs.exits:
                return False, f"目标不可达或未开放: {target!r}（04 §6.2 可达性行）"
            if await self._appointment_conflict(obs.agent_id, sim_now):
                return False, "与进行中约定冲突（01 §4 move 行）"
            return True, None
        if action_type in ("chat", "argue", "gossip", "give_gift", "help", "borrow_money",
                           "repay_money", "confess", "apologize"):
            key = _TARGET_KEY[action_type]
            target = args.get(key)
            if not isinstance(target, str):
                return False, f"{action_type} 缺 {key}"
            tpos = await self._pool.fetchval("SELECT position FROM agents WHERE id=$1", target)
            if tpos is None:
                return False, f"对象 {target} 不存在"
            if tpos != obs.position:
                return False, f"非同节点（{action_type} 须同地点，01 §4/04 §6.2 可达性行）"
        if action_type == "invite":
            target = args.get("target")
            location = args.get("location")
            if isinstance(target, str) and isinstance(location, str) and self._residence is not None:
                if not await self._residence.invite_location_check(
                    agent_id=target, location_id=location, sim_now=sim_now,
                ):
                    return False, "校外 NPC 邀约地点限外部场所或公寓公共区（01 §1.6 可达性行）"
        return True, None

    async def _check_economy(self, obs: Any, action_type: str, args: dict[str, Any]) -> tuple[bool, str | None]:
        if action_type == "shop":
            budget = int(args.get("budget") or 0)
            if budget <= 0:
                return False, "shop 预算须 >0"
            if obs.balance_cents < budget:
                return False, "余额不足预算（01 §4 shop 行）"
        elif action_type == "give_gift":
            tier = int(args.get("tier") or 0)
            prices = self._gift_prices()
            if tier not in (1, 2, 3):
                return False, "give_gift tier 限 1/2/3（01 §4）"
            if obs.balance_cents < prices[tier - 1]:
                return False, "余额不足礼物档位（01 §4 give_gift 行）"
        elif action_type == "trade_stock":
            amount = int(args.get("amount") or 0)
            side = args.get("side")
            symbol = args.get("symbol")
            if amount <= 0 or side not in ("buy", "sell"):
                return False, "trade_stock 需 side=buy/sell 且 amount>0"
            if symbol not in self._stock_symbols():
                return False, f"未知标的 {symbol!r}（01 §1.5 三只）"
            if side == "buy":
                fee = self._trade_fee(amount)
                if obs.balance_cents < amount + fee:
                    return False, "余额不足（含手续费，01 §1.5）"
            else:
                holdings = await self._holdings(obs.agent_id)
                price = await self._stock_price(symbol)
                shares = float((holdings or {}).get(symbol, 0))
                if price <= 0 or shares * price < amount:
                    return False, "持仓不足（01 §4 trade_stock 行）"
        elif action_type == "borrow_money":
            amount = int(args.get("amount") or 0)
            if amount <= 0 or amount > BORROW_MAX_CENTS:
                return False, f"borrow_money 单笔上限 ¥{BORROW_MAX_CENTS // 100}（01 §4）"
        elif action_type == "repay_money":
            amount = int(args.get("amount") or 0)
            if amount <= 0:
                return False, "repay_money 金额须 >0"
            if obs.balance_cents < amount:
                return False, "余额不足（01 §4 repay_money 行）"
        return True, None

    async def _check_action_specific(self, obs: Any, action_type: str, args: dict[str, Any]) -> tuple[bool, str | None]:
        sim_now = obs.sim_time
        aid = obs.agent_id
        if action_type == "think":
            return True, None
        if action_type == "move":
            return True, None  # 前置校验全部由通用可达性/约定冲突覆盖（01 §4 move 行）
        if action_type == "chat":
            target = args["target"]
            if await self._in_dialogue(target, sim_now):
                return False, "target 在对话中（01 §4 chat 行）"
            if self._needs.is_sleeping(sim_now) and await self._position(target) == await self._own_room(target):
                return False, "target 睡眠中（01 §4 chat 行）"
            if args.get("mode") == "deep":
                aff = await self._affinity(aid, target)
                if aff < DEEP_CHAT_AFFINITY:
                    return False, f"深聊需 affinity≥{DEEP_CHAT_AFFINITY}（01 §4 chat 行）"
            return True, None
        if action_type == "send_message":
            if not await self._exists(args.get("target")):
                return False, "对象不存在"
            return True, None
        if action_type == "invite":
            for k in ("target", "activity", "time", "location"):
                if not args.get(k):
                    return False, f"invite 缺 {k}（01 §4 参数 schema）"
            return True, None
        if action_type == "work":
            if not self._needs.is_working(sim_now):
                return False, "非工作日工作时段（01 §4 work 行）"
            if not str(obs.position).startswith("corp."):
                return False, "须位于公司（01 §4 work 行）"
            return True, None
        if action_type == "rest":
            if args.get("mode") not in ("nap", "sleep"):
                return False, "rest 需 mode: nap/sleep（06 §1.2 agent.rest）"
            if obs.position != await self._own_room(aid):
                return False, "须位于自己房间（01 §4 rest 行）"
            return True, None
        if action_type == "eat":
            venue = args.get("venue")
            if venue not in EAT_VENUES:
                return False, f"venue 限 {EAT_VENUES}（01 §1.5 物价表）"
            if venue == "cook" and "apt.kitchen" not in obs.exits and obs.position != "apt.kitchen":
                return False, "厨房未开放（01 §1.2 开放时段）"
            with_ids = args.get("with") or []
            for other in with_ids:
                if await self._position(other) != obs.position and not await self._has_appointment(aid, other, sim_now):
                    return False, "多人用餐需均在场或已约定（01 §4 eat 行）"
            return True, None
        if action_type == "shop":
            if args.get("store") not in SHOP_STORES:
                return False, f"store 限场所枚举 {SHOP_STORES}（01 §4，评审 P2-10）"
            return True, None
        if action_type == "trade_stock":
            start, end = (self._world.get("stocks", {}).get("trading_hours") or ["09:30", "15:00"])
            m = sim_now.hour * 60 + sim_now.minute
            if not (_hhmm(start) <= m < _hhmm(end)):
                return False, "非交易时段 9:30~15:00（01 §4 trade_stock 行）"
            return True, None
        if action_type == "give_gift":
            return True, None  # 同节点/余额已由通用规则覆盖；72h 冷却由通用规则②覆盖
        if action_type == "help":
            target = args["target"]
            tneeds = await self._needs_of(target)
            if not any(self._needs.zone(float(v)) != "normal" for v in tneeds.values()):
                return False, "target 未处于需求驱动区（01 §4 help 行；显式求助判定 M1 预留，D27）"
            return True, None
        if action_type == "borrow_money":
            aff = await self._affinity(args["target"], aid)  # B→A 边（放款人对借款人，D27）
            if aff < BORROW_AFFINITY_MIN:
                return False, f"affinity≥{BORROW_AFFINITY_MIN} 才可开口（01 §4 borrow_money 行）"
            tpos_balance = await self._balance_of(args["target"])
            if tpos_balance < int(args["amount"]):
                return False, "对方余额不足出借（系统结算前提，D27）"
            return True, None
        if action_type == "repay_money":
            debt = await self.open_debt(self._pool, borrower_id=aid, lender_id=args["target"])
            if debt is None:
                return False, "存在对 target 的未结清 debts 行为前提（01 §4 repay_money 行）"
            remaining = int(debt["amount_cents"]) - int(debt["repaid_cents"])
            if int(args["amount"]) > remaining:
                return False, "还款额不得超过未还余额（01 §4 repay_money 行）"
            return True, None
        if action_type == "argue":
            tension = await self._tension(aid, args["target"])
            mood = float(obs.needs.get("mood", 100))
            if tension < ARGUE_TENSION_MIN and mood > ARGUE_MOOD_MAX:
                return False, "argue 需 tension≥20 或情绪≤30（01 §4.1 硬门槛）"
            return True, None
        if action_type == "gossip":
            cites = args.get("cites") or []
            try:
                ids = await store.validate_cites(self._pool, agent_id=aid, cites=cites)
            except ValueError as e:
                return False, str(e)
            if max(1, await gossip_hop(self._pool, ids)) > store.GOSSIP_DEPTH_CAP:
                return False, "传播深度上限 4 手（01 §4.1）"
            return True, None
        if action_type == "refuse":
            ref = args.get("request_ref")
            if not isinstance(ref, str) or not ref.isdigit():
                return False, "refuse 需 request_ref（裸 seq 数字字符串，06 §2）"
            row = await self._pool.fetchrow("SELECT actors, payload FROM events WHERE seq=$1", int(ref))
            if row is None or aid not in list(row["actors"]):
                return False, "存在待响应请求为前提（01 §4 refuse 行）"
            return True, None
        if action_type == "confess":
            aff = await self._affinity(aid, args["target"])
            if aff < CONFESS_AFFINITY:
                return False, f"confess 需 affinity(A→B)≥{CONFESS_AFFINITY}（01 §4）"
            own_room = await self._own_room(aid)
            if obs.position != own_room and obs.position != "apt.roof":
                return False, "confess 需私密场所（01 §4；私密场所 = 自己房间或天台，D27）"
            return True, None
        if action_type == "apologize":
            if not args.get("for_event_ref"):
                return False, "apologize 需 for_event_ref（06 §1.2）"
            tension = await self._tension(aid, args["target"])
            if tension < APOLOGIZE_TENSION_MIN:
                return False, f"apologize 需 tension≥{APOLOGIZE_TENSION_MIN}（01 §4）"
            return True, None
        return False, f"动作 {action_type} 未实现"

    # ==================================================================
    # step5 执行结算（事件 + 缓存列 + 关系/需求 + 记忆 + 唤醒）
    # ==================================================================

    async def settle(self, obs: Any, d: Any, *, tick: int, sim_now: dt.datetime, rng_seed: int) -> list[int]:
        """结算入口：按动作分发；返回落库事件 seq 列表（含次级事件）。占用表随结算推进（D26）。"""
        action = d.action_type
        args = d.action_args
        cost = sim_cost_min(action, rng_seed, obs.agent_id)
        self._busy[obs.agent_id] = sim_now + dt.timedelta(minutes=cost)
        handler = getattr(self, f"_settle_{action}")
        seqs: list[int] = await handler(obs, args, tick=tick, sim_now=sim_now, rng_seed=rng_seed, cost=cost, decision=d)
        # 交互唤醒：有对象的动作 → 对象收 wakeup 下一裁决点处理（04 §2.2；send_message 不唤醒，01 §4）
        target = args.get(_TARGET_KEY.get(action, ""))
        if isinstance(target, str) and self._wakeup is not None and seqs and action != "send_message":
            self._wakeup(tick + 1, target)
        return seqs

    # ---- 移动与生理状态类 -------------------------------------------------

    async def _settle_move(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target = args["to"]
        await self._pool.execute("UPDATE agents SET position=$2 WHERE id=$1", obs.agent_id, target)
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="agent.move", actors=[obs.agent_id],
                                 location_id=target, visibility="public", rng_seed=rng_seed,
                                 payload={"from": obs.position, "to": target, "sim_cost_min": cost})
        return [seq]

    async def _settle_think(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="agent.think", actors=[obs.agent_id],
                                 location_id=obs.position, visibility="internal", rng_seed=rng_seed,
                                 payload={"topic_hint": args.get("topic_hint", decision.intent[:20])})
        importance = 1 + (rng_seed + int(obs.agent_id[1:])) % 3  # 低重要性 1~3（04 §6.2 think 行）
        content = decision.intent if not decision.degraded else "走神了（LLM 输出解析失败降级，04 §6.1 step3）"
        await store.insert_memory(self._pool, self._gw, agent_id=obs.agent_id, sim_time=sim_now,
                                  kind="reflection", content=content, importance=importance,
                                  source_event_seq=seq, rng_seed=rng_seed)
        # think 情绪 +2（N 高者 -2，01 §4 think 行；镜像 needs.yaml satisfy.mood）
        bf = (obs.persona or {}).get("big_five") or {}
        key = "think_n_high" if float(bf.get("neuroticism", 0)) >= N_HIGH else "think_normal"
        await self._agg.apply_needs_delta(
            agent_id=obs.agent_id, need="mood",
            delta=self._needs.satisfy_amount("mood", key), cause=store.caused_by(seq),
        )
        return [seq]

    async def _settle_rest(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        mode = args["mode"]
        if mode == "nap":
            gain = self._needs.satisfy_amount("energy", "nap_30min")  # 小睡 30min +10（01 §3.1）
        else:
            gain = self._needs.satisfy_amount("energy", "sleep_per_hour") * 8  # 睡眠 +9.0/h × 8h（D27 工程口径）
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="agent.rest", actors=[obs.agent_id],
                                 location_id=obs.position, visibility="public", rng_seed=rng_seed,
                                 payload={"mode": mode})
        await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="energy", delta=gain,
                                          cause=store.caused_by(seq))
        return [seq]

    async def _settle_work(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        payload: dict[str, Any] = {"sim_cost_min": cost}
        if args.get("task_id"):
            payload["task_id"] = str(args["task_id"])
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="agent.work", actors=[obs.agent_id],
                                 location_id=obs.position, visibility="public", rng_seed=rng_seed, payload=payload)
        await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="achievement",
                                          delta=self._needs.satisfy_amount("achievement", "task_done"),
                                          cause=store.caused_by(seq))
        return [seq]

    # ---- 经济交易类 -------------------------------------------------

    async def _settle_eat(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        venue = args["venue"]
        price_row = self._world["economy"]["prices"][venue]
        with_ids = [x for x in (args.get("with") or [])]
        per_person = bool(price_row.get("per_person"))
        amount = int(price_row["amount_cents"]) * (len(with_ids) + 1 if per_person else 1)
        await self._charge(obs.agent_id, -amount)
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="agent.eat",
                                 actors=[obs.agent_id, *with_ids], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"venue": venue, "with": with_ids, "amount_cents": -amount})
        cause = store.caused_by(seq)
        for need, delta in (price_row.get("need") or {}).items():
            await self._agg.apply_needs_delta(agent_id=obs.agent_id, need=need, delta=float(delta), cause=cause)
        await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="wealth",
                                          delta=self._wealth_expense_delta(amount), cause=cause)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"吃了顿{venue}，花了 {amount / 100:.0f} 块"},
            importance=2, rng_seed=rng_seed,
        )
        return [seq]

    async def _settle_shop(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        amount = int(args["budget"])
        await self._charge(obs.agent_id, -amount)
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="agent.shop", actors=[obs.agent_id],
                                 location_id=obs.position, visibility="public", rng_seed=rng_seed,
                                 payload={"store": args["store"], "item_category": str(args.get("item_category") or ""),
                                          "amount_cents": -amount})
        cause = store.caused_by(seq)
        await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="mood",
                                          delta=self._needs.satisfy_amount("mood", "shop"), cause=cause)  # 购物疗法 +4（01 §4）
        await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="wealth",
                                          delta=self._wealth_expense_delta(amount), cause=cause)
        return [seq]

    async def _settle_trade_stock(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        symbol, side, amount = args["symbol"], args["side"], int(args["amount"])
        price = await self._stock_price(symbol)  # M1 结算价 = world_state 最新价（02 文档 D3）
        fee = self._trade_fee(amount)
        holdings = dict(await self._holdings(obs.agent_id))
        shares = float(holdings.get(symbol, 0.0))
        if side == "buy":
            cash = -(amount + fee)
            holdings[symbol] = round(shares + amount / price, 6)
            wealth_delta = 0.0
        else:
            cash = amount - fee
            holdings[symbol] = round(shares - amount / price, 6)
            wealth_delta = 0.0  # 盈亏口径随 M3 股价链路接（01 §1.5 ±(盈亏额/500) 封顶 ±10）
        await self._charge(obs.agent_id, cash)
        await self._pool.execute("UPDATE agents SET holdings=$2::jsonb WHERE id=$1",
                                 obs.agent_id, json.dumps(holdings))
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="agent.trade_stock", actors=[obs.agent_id],
                                 location_id=obs.position, visibility="public", rng_seed=rng_seed,
                                 payload={"symbol": symbol, "side": side, "price": price, "amount_cents": cash})
        if wealth_delta:
            await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="wealth", delta=wealth_delta,
                                              cause=store.caused_by(seq))
        return [seq]

    async def _settle_give_gift(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target, tier = args["target"], int(args["tier"])
        prices = self._gift_prices()
        amount = prices[tier - 1]
        await self._charge(obs.agent_id, -amount)
        gift_row = self._relations.matrix_row("gift")
        deltas = gift_row["tiers"][tier - 1]
        labels = await self._labels(obs.agent_id, target)
        bonus = int(gift_row.get("crush_bonus_affinity", 0)) if tier >= 2 and GIFT_CRUSH_LABEL in labels else 0
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="social.give_gift",
                                 actors=[obs.agent_id, target], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"from": obs.agent_id, "to": target, "tier": tier, "amount_cents": -amount},
                                 rel_hit=abs(int(deltas["delta_affinity"]) + bonus) >= 5, followups=True)
        cause = store.caused_by(seq)
        await self._agg.apply_relation_delta(a_id=obs.agent_id, b_id=target,
                                             delta_affinity=int(deltas["delta_affinity"]) + bonus,
                                             delta_tension=int(deltas["delta_tension"]), cause=cause)
        await self._agg.apply_relation_delta(a_id=target, b_id=obs.agent_id,
                                             delta_affinity=int(deltas["delta_affinity"]) + bonus,
                                             delta_tension=int(deltas["delta_tension"]), cause=cause)
        await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="wealth",
                                          delta=self._wealth_expense_delta(amount), cause=cause)
        names = await self._names(obs.agent_id, target)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"我送了{names[target]}一份礼物（{amount / 100:.0f} 块）",
                          target: f"{names[obs.agent_id]}送了我一份礼物"},
            importance=5, rng_seed=rng_seed,
        )
        await self._write_witnesses(seq, "social.give_gift", obs.position, [obs.agent_id, target], sim_now, 5, rng_seed)
        return [seq]

    # ---- 对话社交类（chat 归 T-ADJ-04 对话引擎；argue/gossip/confess/apologize 此处结算） ---------

    async def _settle_chat(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        if self._dialogue_settle is None:
            raise RuntimeError("chat 结算未接线（T-ADJ-04 对话引擎注入 dialogue_settle）")
        return await self._dialogue_settle(obs, args, tick=tick, sim_now=sim_now, rng_seed=rng_seed, cost=cost, decision=decision)

    async def _settle_send_message(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target = args["target"]
        hint = str(args.get("content_hint") or decision.intent[:20])
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="social.send_message",
                                 actors=[obs.agent_id, target], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"from": obs.agent_id, "to": target, "content_hint": hint,
                                          "text_display": hint})
        cause = store.caused_by(seq)
        row = self._action_rows.get("send_message", {"delta_affinity": 1, "delta_tension": 0})
        for x, y in ((obs.agent_id, target), (target, obs.agent_id)):  # 双向（scope=both，D24）
            await self._agg.apply_relation_delta(a_id=x, b_id=y,
                                                 delta_affinity=int(row["delta_affinity"]),
                                                 delta_tension=int(row["delta_tension"]), cause=cause)
        await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="social",
                                          delta=self._needs.satisfy_amount("social", "send_message"), cause=cause)
        names = await self._names(obs.agent_id, target)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"我给{names[target]}捎了句话：{hint}",
                          target: f"{names[obs.agent_id]}捎话来：{hint}"},  # 不唤醒对方决策（01 §4）
            importance=3, rng_seed=rng_seed,
        )
        return [seq]

    async def _settle_argue(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target = args["target"]
        names = await self._names(obs.agent_id, target)
        # argue 行结算（01 §3.2）；对方 N≥70 视为"被侮辱"档（工程默认，D27）
        bf = (await self._persona(target)).get("big_five") or {}
        kind = "argue_insulted" if float(bf.get("neuroticism", 0)) >= N_HIGH else "argue"
        arg_row = self._relations.matrix_row(kind)
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="dialogue.argue",
                                 actors=[obs.agent_id, target], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"participants": [obs.agent_id, target],
                                          "reason_hint": str(args.get("reason_hint") or decision.intent[:20]),
                                          "lines": [], "witnesses": []},
                                 rel_hit=abs(int(arg_row["delta_affinity"])) >= 5, followups=True)
        cause = store.caused_by(seq)
        await self._relations.settle(self._agg, kind=kind, a_id=obs.agent_id, b_id=target, cause=cause)
        await self._relations.settle(self._agg, kind=kind, a_id=target, b_id=obs.agent_id, cause=cause)
        await self._cooldown.write_cooldown(agent_id=obs.agent_id, trigger_kind="argue_reapproach",
                                            target=target, sim_now=sim_now)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"我和{names[target]}吵了一架",
                          target: f"{names[obs.agent_id]}和我吵了一架"},
            importance=6, rng_seed=rng_seed,
        )
        await self._write_witnesses(seq, "dialogue.argue", obs.position, [obs.agent_id, target], sim_now, 6, rng_seed,
                                    summary="激烈争论")
        return [seq]

    async def _settle_gossip(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        listener, about = args["target_listener"], args["about"]
        cites = [str(c) for c in (args.get("cites") or [])]
        cite_ids = await store.validate_cites(self._pool, agent_id=obs.agent_id, cites=cites)
        hop = max(1, await gossip_hop(self._pool, cite_ids))  # 本手手数（无源记忆 = 一手转述）
        fidelity = store.fidelity_at_hop(hop)
        distortion = store.roll_distortion(
            store.distortion_rng(rng_seed, cite_ids[0], hop),
            neuroticism=float(((obs.persona or {}).get("big_five") or {}).get("neuroticism", 0)),
        )
        names = await self._names(obs.agent_id, listener, about)
        text = f"{names[obs.agent_id]}跟{names[listener]}说起{names[about]}的事"
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="dialogue.gossip",
                                 actors=[obs.agent_id, listener], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"teller": obs.agent_id, "listener": listener, "about": about,
                                          "cites": cites, "lines": [], "text_display": text,
                                          "caused_by": store.caused_by(cite_ids[0] and int(cite_ids[0]) or 0)},
                                 followups=True)
        # 失真骰结果仅内核侧留痕日志（fidelity/distortion 不进 payload、不出站，00 §4 红线 8）
        log.debug("gossip 失真链：hop=%d fidelity=%.2f tier=%s distortion=%s", hop, fidelity,
                  store.detail_tier(fidelity), distortion)
        cause = store.caused_by(seq)
        await self._relations.settle(self._agg, kind="gossip_bond", a_id=listener, b_id=obs.agent_id, cause=cause)
        await self._cooldown.write_cooldown(agent_id=obs.agent_id, trigger_kind="gossip_same_target",
                                            target=about, sim_now=sim_now)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"我跟{names[listener]}聊了{names[about]}的事",
                          listener: f"{names[obs.agent_id]}跟我说了{names[about]}的事（第 {hop} 手）"},
            importance=4, rng_seed=rng_seed,
        )
        await self._write_witnesses(seq, "dialogue.gossip", obs.position, [obs.agent_id, listener], sim_now, 4, rng_seed)
        return [seq]

    async def _settle_confess(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target = args["target"]
        result = str(args.get("result") or "accepted")  # 对方判定链路 M2 接（D27）
        names = await self._names(obs.agent_id, target)
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="dialogue.confess",
                                 actors=[obs.agent_id, target], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"participants": [obs.agent_id, target], "result": result,
                                          "lines": [], "text_display": f"{names[obs.agent_id]}向{names[target]}表明了心意"},
                                 rel_hit=True, followups=True)  # ±15/-5 与"恋人"标签（04 §6.6 R2）
        cause = store.caused_by(seq)
        kind = "confess_accepted" if result == "accepted" else "confess_rejected"
        scope = "both" if result == "accepted" else None
        labels = ["恋人"] if result == "accepted" else None
        await self._relations.settle(self._agg, kind=kind, a_id=obs.agent_id, b_id=target, cause=cause,
                                     scope=scope, labels_added=labels)
        if result != "accepted":
            await self._cooldown.write_cooldown(agent_id=obs.agent_id, trigger_kind="confess_rejected",
                                                target=target, sim_now=sim_now)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"我向{names[target]}表明了心意（{result}）",
                          target: f"{names[obs.agent_id]}向我表明了心意（{result}）"},
            importance=8, rng_seed=rng_seed,
        )
        await self._write_witnesses(seq, "dialogue.confess", obs.position, [obs.agent_id, target], sim_now, 8, rng_seed)
        return [seq]

    async def _settle_apologize(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target = args["target"]
        ref = str(args["for_event_ref"])
        result = str(args.get("result") or "accepted")
        names = await self._names(obs.agent_id, target)
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="dialogue.apologize",
                                 actors=[obs.agent_id, target], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"participants": [obs.agent_id, target], "for_event_ref": ref,
                                          "result": result,
                                          "lines": [], "text_display": f"{names[obs.agent_id]}向{names[target]}道了歉"},
                                 followups=True)
        cause = store.caused_by(seq)
        kind = "apologize_accepted" if result == "accepted" else "apologize_rejected"
        await self._relations.settle(self._agg, kind=kind, a_id=obs.agent_id, b_id=target, cause=cause)
        if result != "accepted":
            await self._cooldown.write_cooldown(agent_id=obs.agent_id, trigger_kind="apologize_refused",
                                                target=target, sim_now=sim_now)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"我为之前的事向{names[target]}道了歉（{result}）",
                          target: f"{names[obs.agent_id]}向我道了歉（{result}）"},
            importance=5, rng_seed=rng_seed,
        )
        return [seq]

    # ---- 邀约与协助类 -------------------------------------------------

    async def _settle_invite(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        if self._invite is None:
            raise RuntimeError("invite 结算未接线（T-REL-05 InviteMachine 注入）")
        at_sim = dt.datetime.fromisoformat(str(args["time"]))
        outcome = await self._invite.send(
            a_id=obs.agent_id, b_id=args["target"], activity=str(args["activity"]),
            at_sim=at_sim, location=str(args["location"]), sim_now=sim_now, tick=tick, rng_seed=rng_seed,
        )
        return outcome.event_seqs

    async def _settle_help(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target = args["target"]
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="social.help",
                                 actors=[obs.agent_id, target], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"from": obs.agent_id, "to": target,
                                          "matter": str(args.get("matter") or decision.intent[:20])},
                                 rel_hit=True, followups=True)  # help +6（04 §6.6 R2）
        cause = store.caused_by(seq)
        await self._relations.settle(self._agg, kind="help", a_id=target, b_id=obs.agent_id, cause=cause)
        await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="achievement",
                                          delta=self._needs.satisfy_amount("achievement", "help"), cause=cause)
        names = await self._names(obs.agent_id, target)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"我帮了{names[target]}一把",
                          target: f"{names[obs.agent_id]}雪中送炭帮了我"},
            importance=5, rng_seed=rng_seed,
        )
        return [seq]

    async def _settle_borrow_money(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target, amount = args["target"], int(args["amount"])
        result = str(args.get("result") or "accepted")  # 对方判定链路 M2 接（D27）
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="social.borrow_money",
                                 actors=[obs.agent_id, target], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"from": obs.agent_id, "to": target, "amount_cents": amount,
                                          "result": result},
                                 followups=True)
        cause = store.caused_by(seq)
        names = await self._names(obs.agent_id, target)
        if result == "accepted":
            # 写 debts 行（a_id=债主/b_id=欠款人，round2 §A.18 口径）+ 转账结算（系统直落，00 §4 红线 6）
            await self.write_debt(self._pool, lender_id=target, borrower_id=obs.agent_id,
                                  amount_cents=amount, due_sim=sim_now + dt.timedelta(days=DEBT_DUE_DAYS),
                                  tick=tick)
            await self._charge(target, -amount)
            await self._charge(obs.agent_id, amount)
            await self._agg.apply_needs_delta(agent_id=obs.agent_id, need="wealth",
                                              delta=self._wealth_income_delta(amount), cause=cause)
            contents = {obs.agent_id: f"我向{names[target]}借了 {amount / 100:.0f} 块，14 天内还",
                        target: f"我借给{names[obs.agent_id]} {amount / 100:.0f} 块"}
            importance = 6
        else:
            row = self._action_rows.get("borrow_refused", {"delta_affinity": -4, "delta_tension": 3})
            await self._agg.apply_relation_delta(a_id=obs.agent_id, b_id=target,
                                                 delta_affinity=int(row["delta_affinity"]),
                                                 delta_tension=int(row["delta_tension"]), cause=cause)
            await self._cooldown.write_cooldown(agent_id=obs.agent_id, trigger_kind="borrow_refused",
                                                target=target, sim_now=sim_now)
            contents = {obs.agent_id: f"我向{names[target]}开口借钱，被回绝了",
                        target: f"{names[obs.agent_id]}找我借钱，我没答应"}
            importance = 5
        await store.write_event_memories(self._pool, self._gw, event_seq=seq, sim_time=sim_now,
                                         perspectives=contents, importance=importance, rng_seed=rng_seed)
        return [seq]

    async def _settle_repay_money(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target, amount = args["target"], int(args["amount"])
        debt = await self.open_debt(self._pool, borrower_id=obs.agent_id, lender_id=target)
        assert debt is not None  # check 已保证
        repaid, cleared = await self.settle_repay(self._pool, debt_id=int(debt["id"]), amount_cents=amount)
        on_time = cleared and sim_now <= debt["due_sim"]  # 按期全额结清 → lend_repaid_ontime（01 §3.2）
        await self._charge(obs.agent_id, -amount)
        await self._charge(target, amount)
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="social.repay_money",
                                 actors=[obs.agent_id, target], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"from": obs.agent_id, "to": target, "amount_cents": -amount,
                                          "debt_ref": str(debt["id"])},
                                 rel_hit=on_time, followups=True)  # 按期结清 +8（04 §6.6 R2）
        cause = store.caused_by(seq)
        if on_time:
            # 按期全额结清（放款起 14 模拟日内）→ 借钱被借方 +8/-5（01 §3.2）
            await self._relations.settle(self._agg, kind="lend_repaid_ontime", a_id=target, b_id=obs.agent_id,
                                         cause=cause)
        names = await self._names(obs.agent_id, target)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"我还了{names[target]} {amount / 100:.0f} 块" + ("，这笔结清了" if cleared else ""),
                          target: f"{names[obs.agent_id]}还了我 {amount / 100:.0f} 块" + ("，这笔结清了" if cleared else "")},
            importance=5, rng_seed=rng_seed,
        )
        return [seq]

    async def _settle_refuse(self, obs, args, *, tick, sim_now, rng_seed, cost, decision) -> list[int]:
        target = args["target"]
        politeness = int(args.get("politeness") or 0)
        seq = await self._insert(obs, tick=tick, sim_now=sim_now, type_="social.refuse",
                                 actors=[obs.agent_id, target], location_id=obs.position,
                                 visibility="public", rng_seed=rng_seed,
                                 payload={"from": obs.agent_id, "to": target,
                                          "request_ref": str(args["request_ref"]), "politeness": politeness},
                                 followups=True)
        cause = store.caused_by(seq)
        row = self._relations.matrix_row("refuse")
        factor = float(row.get("polite_factor", 1.0)) if politeness else 1.0
        await self._relations.settle(self._agg, kind="refuse", a_id=target, b_id=obs.agent_id,
                                     cause=cause, factor=factor)
        names = await self._names(obs.agent_id, target)
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={obs.agent_id: f"我拒绝了{names[target]}的请求",
                          target: f"{names[obs.agent_id]}拒绝了我的请求"},
            importance=4, rng_seed=rng_seed,
        )
        return [seq]

    # ==================================================================
    # debts 写入/核销（04 §5.2 debts 表；收编口径见模块头）
    # ==================================================================

    @staticmethod
    async def open_debt(pool: Any, *, borrower_id: str, lender_id: str) -> Any | None:
        """未结清债务边（b_id=欠款人 / a_id=债主，round2 §A.18 口径）。"""
        return await pool.fetchrow(
            """
            SELECT id, amount_cents, repaid_cents, due_sim FROM debts
            WHERE b_id=$1 AND a_id=$2 AND repaid_cents < amount_cents ORDER BY id LIMIT 1
            """,
            borrower_id, lender_id,
        )

    @staticmethod
    async def write_debt(pool: Any, *, lender_id: str, borrower_id: str, amount_cents: int,
                         due_sim: dt.datetime, tick: int) -> int:
        """borrow_money 成功写 debts 行（还款期限 = 放款起 14 模拟日由调用方给足，01 §4）。"""
        return await pool.fetchval(
            """
            INSERT INTO debts (a_id, b_id, amount_cents, due_sim, created_tick)
            VALUES ($1, $2, $3, $4, $5) RETURNING id
            """,
            lender_id, borrower_id, amount_cents, due_sim, tick,
        )

    @staticmethod
    async def settle_repay(pool: Any, *, debt_id: int, amount_cents: int) -> tuple[int, bool]:
        """repay_money 核销：`repaid_cents += 还款额`；返回（累计已还， 是否全额结清）。"""
        row = await pool.fetchrow(
            """
            UPDATE debts SET repaid_cents = repaid_cents + $2
            WHERE id=$1 AND repaid_cents + $2 <= amount_cents
            RETURNING repaid_cents, amount_cents
            """,
            debt_id, amount_cents,
        )
        if row is None:
            raise ValueError("还款额超过未还余额（01 §4 repay_money 行，前置校验应已拦截）")
        return int(row["repaid_cents"]), int(row["repaid_cents"]) >= int(row["amount_cents"])

    # ==================================================================
    # 内部工具
    # ==================================================================

    async def _insert(self, obs, *, tick, sim_now, type_, actors, location_id, visibility, rng_seed,
                      payload, rel_hit: bool = False, mood_hit: bool = False, followups: bool = False) -> int:
        ui: dict[str, Any] | None = None
        if self._grader is not None:
            ui = {"grade": await self._grader.grade(
                type_=type_, actors=actors, payload=payload, sim_now=sim_now,
                rel_hit=rel_hit, mood_hit=mood_hit, followups=followups,
            )}  # T-ADJ-07：grade 初值唯一实现，同事务随 INSERT 写入（04 §6.6；信号口径 D30）
        return await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, location_id, actors, rng_seed, visibility, payload, ui)
            VALUES ($1, $2, $3, $4, 'autonomous', $5, $6, $7, $8, $9::jsonb, $10::jsonb)
            RETURNING seq
            """,
            tick, sim_now, type_, f"agent:{obs.agent_id}", location_id, actors, rng_seed, visibility,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(ui, ensure_ascii=False) if ui else None,
        )

    async def _write_witnesses(self, seq, type_, location_id, actors, sim_now, base_importance, rng_seed,
                               summary: str = "") -> None:
        await store.write_witness_projections(
            self._pool, self._gw, event_seq=seq, event_type=type_, location_id=location_id,
            actors=actors, sim_time=sim_now, base_importance=base_importance, rng_seed=rng_seed,
            summary=summary,
        )

    async def _charge(self, agent_id: str, delta_cents: int) -> None:
        """余额只由系统按事件结算（00 §4 红线 6；金额一律分，正入负出）。"""
        await self._pool.execute("UPDATE agents SET balance_cents = balance_cents + $2 WHERE id=$1",
                                 agent_id, delta_cents)

    def _wealth_expense_delta(self, amount_cents: int) -> float:
        m = self._world["economy"]["wealth_need_mapping"]["expense"]
        return float(m["sign"]) * max(m["clamp_min"], min(m["clamp_max"], (amount_cents / 100.0) / float(m["divisor"])))

    def _wealth_income_delta(self, amount_cents: int) -> float:
        m = self._world["economy"]["wealth_need_mapping"]["income"]
        return float(m["sign"]) * max(m["clamp_min"], min(m["clamp_max"], (amount_cents / 100.0) / float(m["divisor"])))

    def _gift_prices(self) -> list[int]:
        return [int(x) for x in self._world["economy"]["prices"]["gift"]["tiers_cents"]]

    def _stock_symbols(self) -> set[str]:
        return {s["id"] for s in self._world.get("stocks", {}).get("symbols", [])}

    def _trade_fee(self, amount_cents: int) -> int:
        fee = self._world["stocks"]["fee"]
        return max(int(fee["min_cents"]), int(amount_cents * float(fee["rate"])))

    async def _stock_price(self, symbol: str) -> int:
        """M1 结算价 = world_state 最新价、缺省回退 01 §1.5 初始价（02 文档 D3）。"""
        raw = await self._pool.fetchval("SELECT value FROM world_state WHERE key='economy.stocks'")
        stocks = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        if symbol in stocks:
            return int(stocks[symbol])
        for s in self._world.get("stocks", {}).get("symbols", []):
            if s["id"] == symbol:
                return int(s["initial_price_cents"])
        raise KeyError(f"未知标的 {symbol!r}")

    async def _holdings(self, agent_id: str) -> dict[str, Any]:
        raw = await self._pool.fetchval("SELECT holdings FROM agents WHERE id=$1", agent_id)
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})

    async def _persona(self, agent_id: str) -> dict[str, Any]:
        raw = await self._pool.fetchval("SELECT persona FROM agents WHERE id=$1", agent_id)
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})

    async def _position(self, agent_id: str) -> str | None:
        return await self._pool.fetchval("SELECT position FROM agents WHERE id=$1", agent_id)

    async def _exists(self, agent_id: Any) -> bool:
        return isinstance(agent_id, str) and bool(
            await self._pool.fetchval("SELECT count(*) FROM agents WHERE id=$1", agent_id))

    async def _own_room(self, agent_id: str) -> str | None:
        room = await self._pool.fetchval("SELECT room_no FROM agents WHERE id=$1", agent_id)
        return f"apt.L{int(str(room)[:-2])}.{room}" if room else None

    async def _needs_of(self, agent_id: str) -> dict[str, float]:
        return await self._agg.read_needs(agent_id)

    async def _balance_of(self, agent_id: str) -> int:
        return int(await self._pool.fetchval("SELECT balance_cents FROM agents WHERE id=$1", agent_id))

    async def _affinity(self, a_id: str, b_id: str) -> int:
        v = await self._pool.fetchval("SELECT affinity FROM relations WHERE a_id=$1 AND b_id=$2", a_id, b_id)
        return int(v) if v is not None else 0

    async def _tension(self, a_id: str, b_id: str) -> int:
        v = await self._pool.fetchval("SELECT tension FROM relations WHERE a_id=$1 AND b_id=$2", a_id, b_id)
        return int(v) if v is not None else 0

    async def _labels(self, a_id: str, b_id: str) -> list[str]:
        v = await self._pool.fetchval("SELECT labels FROM relations WHERE a_id=$1 AND b_id=$2", a_id, b_id)
        return list(v or [])

    async def _names(self, *ids: str) -> dict[str, str]:
        rows = await self._pool.fetch("SELECT id, name FROM agents WHERE id = ANY($1::text[])", list(ids))
        return {r["id"]: r["name"] for r in rows}

    async def _in_dialogue(self, agent_id: str, sim_now: dt.datetime) -> bool:
        return bool(await self._pool.fetchval(
            """
            SELECT count(*) FROM events
            WHERE type LIKE 'dialogue.%%' AND $1 = ANY(actors)
              AND sim_time > $2::timestamptz - ($3 || ' minutes')::interval
            """,
            agent_id, sim_now, str(DIALOGUE_BUSY_WINDOW_MIN),
        ))

    async def _appointment_conflict(self, agent_id: str, sim_now: dt.datetime) -> bool:
        """进行中约定冲突（01 §4 move 行）：执行窗 [at_sim, at_sim+15min) 覆盖当前时刻的约定。"""
        return bool(await self._pool.fetchval(
            """
            SELECT count(*) FROM events
            WHERE type='social.appointment.created' AND $1 = ANY(actors)
              AND payload->>'at_sim' <= $2::text
              AND (payload->>'at_sim')::timestamptz > $2::timestamptz - interval '15 minutes'
              AND (payload->>'at_sim')::timestamptz <= $2::timestamptz
            """,
            agent_id, sim_now.isoformat(),
        ))

    async def _has_appointment(self, a_id: str, b_id: str, sim_now: dt.datetime) -> bool:
        return bool(await self._pool.fetchval(
            """
            SELECT count(*) FROM events
            WHERE type='social.appointment.created' AND actors @> $1::text[]
              AND (payload->>'at_sim')::timestamptz > $2::timestamptz - interval '1 day'
            """,
            sorted([a_id, b_id]), sim_now,
        ))


def _hhmm(s: str) -> int:
    hh, mm = str(s).split(":")
    return int(hh) * 60 + int(mm)


async def gossip_hop(pool: Any, memory_ids: list[int], *, _depth: int = 0) -> int:
    """本次 gossip 的手数 = 1 + 被引记忆源链上的最大 gossip 手数（01 §4.1 失真链；上限 4 手）。

    源为纯反思/摘要类记忆（无 source_event_seq）→ 记 0 手；递归沿 `cites` 链上溯，防环截断。
    """
    if _depth > store.GOSSIP_DEPTH_CAP + 2 or not memory_ids:
        return 0
    rows = await pool.fetch("SELECT id, source_event_seq FROM memories WHERE id = ANY($1::bigint[])", memory_ids)
    best = 0
    for r in rows:
        src = r["source_event_seq"]
        if src is None:
            continue
        ev = await pool.fetchrow("SELECT type, payload FROM events WHERE seq=$1", int(src))
        if ev is None or ev["type"] != "dialogue.gossip":
            best = max(best, 1)
            continue
        payload = ev["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        parent_cites = [int(c) for c in payload.get("cites", []) if str(c).isdigit()]
        best = max(best, 1 + await gossip_hop(pool, parent_cites, _depth=_depth + 1))
    return best
