"""经济日历结算：工资/房租/水电（04 T-WA-03；01 §1.5/§6.1；06 §1.2/§1.3）。

- 三类固定日历经济事件注册进 T-WA-02 日历回调表（`register_economy_jobs`），触发日历读
  `world.yaml` `economy.*.bill_day/payday`（01 §6.1 镜像），代码零硬编码数字。
- 金额一律 `payload.amount_cents`（分，正入负出，06 §1.3/00 §4 红线 6）；财富/情绪需求变更
  经 `StateAggregator` 落 `state.needs_delta`（`cause` = 本事件 seq 裸数字字符串，04 §6.5）。
- 余额不足不扣负，转欠费链路（T-WA-04，`settle_bill` 的 `on_shortfall` 分支）。
- 每人月薪只读 `world_state` `economy.salary`（M0 seed 按档抽定，D-06）；涨薪后读最新值
  （T-WA-07 写入同一键）；通勤通讯包随工资代扣（01 §1.5，同月 1 日）。
"""

from __future__ import annotations

import datetime as dt
import logging
import random
from typing import Any, Awaitable, Callable

from .calendar import CalendarEngine

log = logging.getLogger(__name__)

SALARY_KEY = "economy.salary"           # world_state 键：每人月薪（分），M0 seed 抽定（D-06）
UTILITY_LAST_KEY = "economy.utility_last_cents"  # 水电随机游走状态（01 §1.5 ±20%）


def _month_of(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def wealth_expense_delta(cfg: dict[str, Any], amount_cents: int) -> float:
    """消费 → 财富需求映射：Δ = -clamp(金额/200, 1, 6)（01 §1.5 通用公式，配置镜像）。"""
    m = cfg["economy"]["wealth_need_mapping"]["expense"]
    return float(m["sign"]) * max(m["clamp_min"], min(m["clamp_max"], (amount_cents / 100.0) / float(m["divisor"])))


def wealth_income_delta(cfg: dict[str, Any], amount_cents: int) -> float:
    """非工资收入 → 财富需求映射：Δ = +clamp(金额/500, 1, 8)（01 §1.5）。"""
    m = cfg["economy"]["wealth_need_mapping"]["income"]
    return float(m["sign"]) * max(m["clamp_min"], min(m["clamp_max"], (amount_cents / 100.0) / float(m["divisor"])))


def bill_wealth_delta(cfg: dict[str, Any], rng: random.Random) -> float:
    """账单 → 财富需求：-8~-12 区间内取（01 §1.5 特例，配置 bill_range 镜像；骰子可复现）。"""
    lo, hi = (float(x) for x in cfg["economy"]["wealth_need_mapping"]["bill_range"])
    return round(rng.uniform(min(lo, hi), max(lo, hi)), 2)


async def _monthly_salary(pool: Any, agent_id: str) -> int:
    row = await pool.fetchrow("SELECT value FROM world_state WHERE key=$1", SALARY_KEY)
    if row is None:
        raise KeyError(f"world_state 缺 {SALARY_KEY}（每人月薪由 M0 seed 抽定落入，D-06）")
    import json
    table = json.loads(row["value"]) if isinstance(row["value"], str) else dict(row["value"])
    if agent_id not in table:
        raise KeyError(f"{SALARY_KEY} 缺 {agent_id}（M0 seed 口径，D-06）")
    return int(table[agent_id])


async def _apply_needs(cal: CalendarEngine, *, agent_id: str, deltas: dict[str, float], cause: str) -> None:
    """需求变更经聚合器缓冲（04 §6.5；agg 缺省时跳过——纯金额测试口径）。"""
    if cal.agg is None:
        return
    for need, delta in deltas.items():
        if delta:
            await cal.agg.apply_needs_delta(agent_id=agent_id, need=need, delta=delta, cause=cause)


# ---- 发薪（每月 payday 10:00，01 §6.1） -----------------------------------------


async def settle_payroll(cal: CalendarEngine, fire_time: dt.datetime) -> list[int]:
    """`economy.payroll`：全员各一条；金额 = 月薪 − 通勤通讯包（代扣，01 §1.5）；财富 +salary_credit。"""
    cfg = cal.cfg["economy"]
    commute = int(cfg["commute"]["amount_cents"])
    credit = float(cfg["wealth_need_mapping"]["salary_credit"])
    month = _month_of(fire_time.date())
    rows = await cal.pool.fetch("SELECT id, name FROM agents ORDER BY id")
    seqs: list[int] = []
    for r in rows:
        aid = r["id"]
        salary = await _monthly_salary(cal.pool, aid)
        net = salary - commute
        seq = await cal.insert_event(
            type_="economy.payroll",
            payload={"agent_id": aid, "amount_cents": net, "month": month,
                     "text_display": f"{month} 工资到账（{r['name']}）"},
            sim_time=fire_time, actors=[aid], rng_seed=cal.clock.tick_of(fire_time),
        )
        await cal.pool.execute("UPDATE agents SET balance_cents = balance_cents + $2 WHERE id=$1", aid, net)
        await _apply_needs(cal, agent_id=aid, deltas={"wealth": credit}, cause=str(seq))
        seqs.append(seq)
    log.info("发薪结算完成：%s 共 %d 人", month, len(seqs))
    return seqs


# ---- 账单（房租每月 bill_day 9:00 / 水电每月 bill_day 9:00，01 §6.1） --------------

OnShortfall = Callable[[CalendarEngine, dt.datetime, dict[str, Any], str, int, int, str], Awaitable[int]]


def rent_amount_of(cfg: dict[str, Any], room_no: str | None) -> int | None:
    """房租楼层档（01 §1.5）；无房号（校外 NPC）返回 None（不结算房租）。"""
    if not room_no:
        return None
    floor = int(str(room_no)[0])
    for tier in cfg["economy"]["rent"]["by_floor"].values():
        if floor in [int(f) for f in tier["floors"]]:
            return int(tier["amount_cents"])
    raise ValueError(f"房号 {room_no!r} 楼层 {floor} 不在 rent.by_floor 任何档位（01 §1.5）")


async def settle_bill(cal: CalendarEngine, fire_time: dt.datetime, *, kind: str,
                      on_shortfall: OnShortfall | None = None) -> list[int]:
    """账单统一结算：`kind` = 'rent' | 'utility'。

    余额充足 → `economy.bill.<kind>`（amount_cents = -账单额，全额扣）；
    不足 → `on_shortfall` 分支（T-WA-04 欠费链路，实扣至 0 + 差额挂账）；不扣负（01 §1.5 硬规则）。
    """
    cfg = cal.cfg["economy"]
    period = _month_of(fire_time.date())
    rows = await cal.pool.fetch("SELECT id, name, room_no, balance_cents FROM agents ORDER BY id")
    seed = cal.clock.tick_of(fire_time)
    # 水电公摊金额为该计费周期全楼统一值（随机游走一周期的状态只推进一步，01 §1.5）
    utility_amount: int | None = None
    if kind == "utility":
        rng = random.Random(f"{seed}|utility|{fire_time.date().isoformat()}")
        utility_amount = await _utility_amount(cal, rng)
    seqs: list[int] = []
    for r in rows:
        aid = r["id"]
        if kind == "rent":
            amount = rent_amount_of(cal.cfg, r["room_no"])
            if amount is None:
                continue
        else:
            amount = utility_amount
        assert amount is not None
        balance = int(r["balance_cents"])
        if balance >= amount:
            seq = await cal.insert_event(
                type_=f"economy.bill.{kind}",
                payload={"agent_id": aid, "amount_cents": -amount, "due": f"{fire_time.date().day}日"},
                sim_time=fire_time, actors=[aid], rng_seed=seed,
            )
            await cal.pool.execute("UPDATE agents SET balance_cents = balance_cents - $2 WHERE id=$1", aid, amount)
            bill_rng = random.Random(f"{seed}|{aid}|{kind}")
            await _apply_needs(cal, agent_id=aid,
                               deltas={"wealth": bill_wealth_delta(cal.cfg, bill_rng)}, cause=str(seq))
        else:
            if on_shortfall is None:
                raise RuntimeError(f"{aid} 账单余额不足但未接欠费链路（T-WA-04 on_shortfall 未注册）")
            seq = await on_shortfall(cal, fire_time, dict(r), kind, amount, balance, period)
        seqs.append(seq)
    log.info("%s 账单结算完成：%s 共 %d 条", kind, period, len(seqs))
    return seqs


async def _utility_amount(cal: CalendarEngine, rng: random.Random) -> int:
    """水电公摊：配置区间内 ±random_walk_pct 随机游走（01 §1.5），状态存 world_state。"""
    util = cal.cfg["economy"]["utility"]
    lo, hi = int(util["min_cents"]), int(util["max_cents"])
    last = await cal.get_state(UTILITY_LAST_KEY)
    base = int(last) if last is not None else (lo + hi) // 2
    pct = float(util["random_walk_pct"]) / 100.0
    amount = int(round(base * (1 + rng.uniform(-pct, pct))))
    amount = max(lo, min(hi, amount))
    await cal.set_state(UTILITY_LAST_KEY, amount)
    return amount


# ---- 欠费链路（T-WA-04；01 §1.5 欠租/欠费结算规则表；06 §1.2 v1.2） -----------------

OVERDUE_KEY_PREFIX = "overdue."          # world_state 挂账键：overdue.<agent_id>（D-06，不计复利）
OVERDUE_SCAN_TIME = "09:30"              # 每日欠费/债务扫描时点（工程默认，账单 9:00 之后）

OnCrisis = Callable[[CalendarEngine, dt.datetime, str, int], Awaitable[int]]
"""退租危机回调（无空房时）：生产接线 = T-DIR-03 intervene L1（唯一入口，01 §6.3 计干预率）。"""


async def _get_overdue(cal: CalendarEngine, agent_id: str) -> dict[str, Any]:
    return dict(await cal.get_state(f"{OVERDUE_KEY_PREFIX}{agent_id}", {}) or {})


async def _set_overdue(cal: CalendarEngine, agent_id: str, state: dict[str, Any]) -> None:
    if state:
        await cal.set_state(f"{OVERDUE_KEY_PREFIX}{agent_id}", state)
    else:  # 结清清空（挂账归零 = 空对象，不删键，append-only 语义延伸）
        await cal.set_state(f"{OVERDUE_KEY_PREFIX}{agent_id}", {})


async def settle_bill_shortfall(cal: CalendarEngine, fire_time: dt.datetime, agent_row: dict[str, Any],
                                kind: str, amount: int, balance: int, period: str) -> int:
    """账单日余额不足（01 §1.5 硬规则）：实扣至余额归 0，差额挂账（不计复利），落
    `economy.bill.<kind>.overdue`（payload 三键逐字 06 §1.2）；情绪/财富变更（on_overdue 镜像）。"""
    aid = agent_row["id"]
    overdue_cents = amount - balance
    seq = await cal.insert_event(
        type_=f"economy.bill.{kind}.overdue",
        payload={"agent_id": aid, "amount_cents": -balance if balance else 0,
                 "overdue_cents": overdue_cents, "period": period},
        sim_time=fire_time, actors=[aid], rng_seed=cal.clock.tick_of(fire_time),
    )
    if balance > 0:
        await cal.pool.execute("UPDATE agents SET balance_cents = 0 WHERE id=$1", aid)
    on_ov = cal.cfg["economy"]["overdue"]["on_overdue"]
    await _apply_needs(cal, agent_id=aid, deltas={
        "mood": float(on_ov["mood_delta"]), "wealth": float(on_ov["wealth_delta"]),
    }, cause=str(seq))
    # 挂账持久化（world_state KV，D-06；periods 计数供 notice/强制搬迁阶梯判定）
    state = await _get_overdue(cal, aid)
    entry = state.setdefault(kind, {"owed_cents": 0, "periods": []})
    entry["owed_cents"] = int(entry["owed_cents"]) + overdue_cents
    if period not in entry["periods"]:
        entry["periods"].append(period)
    await _set_overdue(cal, aid, state)
    log.warning("%s %s 账单余额不足：实扣 %d 挂账 %d（period=%s）", aid, kind, balance, overdue_cents, period)
    return seq


async def try_settle_overdue(cal: CalendarEngine, fire_time: dt.datetime) -> list[int]:
    """结清扫描：余额充足自动补扣清账（同类型负额结算事件，不新增 payload 键；不还负不计复利）。"""
    rows = await cal.pool.fetch("SELECT id, balance_cents FROM agents ORDER BY id")
    seqs: list[int] = []
    for r in rows:
        aid = r["id"]
        state = await _get_overdue(cal, aid)
        if not state:
            continue
        balance = int(r["balance_cents"])
        changed = False
        for kind in ("rent", "utility"):
            entry = state.get(kind)
            if not entry or int(entry["owed_cents"]) <= 0:
                continue
            owed = int(entry["owed_cents"])
            if balance < owed:
                continue  # 不够不清（部分补扣设计未定义，工程默认足额才扣，D-25）
            seq = await cal.insert_event(
                type_=f"economy.bill.{kind}",
                payload={"agent_id": aid, "amount_cents": -owed, "due": f"{fire_time.date().day}日"},
                sim_time=fire_time, actors=[aid], rng_seed=cal.clock.tick_of(fire_time),
            )
            await cal.pool.execute(
                "UPDATE agents SET balance_cents = balance_cents - $2 WHERE id=$1", aid, owed)
            balance -= owed
            del state[kind]
            changed = True
            seqs.append(seq)
            log.info("%s %s 挂账结清：补扣 %d", aid, kind, owed)
        if changed:
            await _set_overdue(cal, aid, state)
    return seqs


async def settle_overdue_escalation(cal: CalendarEngine, fire_time: dt.datetime, *,
                                    on_crisis: OnCrisis | None = None) -> list[int]:
    """账单周期升级阶梯（仅 rent 链，01 §1.5；utility 链止于 overdue/结清，D-02 销项口径）：
    下个账单周期未结清 → `economy.bill.rent.notice`（快照无新结算，情绪/挫败值变更）；
    连续 2 个账单周期未结清 → 强制搬迁最低价位空房；无空房 → `on_crisis`（T-DIR-03 L1 退租危机）。
    本函数在房租账单日、逐人新账单结算**之前**调用（register 顺序保证）。"""
    cfg = cal.cfg["economy"]
    npu = cfg["overdue"]["next_period_unpaid"]
    period = _month_of(fire_time.date())
    rows = await cal.pool.fetch("SELECT id, room_no FROM agents ORDER BY id")
    seqs: list[int] = []
    for r in rows:
        aid = r["id"]
        state = await _get_overdue(cal, aid)
        entry = state.get("rent")
        if not entry or int(entry["owed_cents"]) <= 0:
            continue
        unpaid = len(entry["periods"])  # 已挂账周期数（含首欠周期）
        if unpaid >= 2:
            # 连续 2 个账单周期未结清（01 §1.5 末行）
            moved = await _force_move(cal, fire_time, aid)
            if not moved:
                if on_crisis is None:
                    raise RuntimeError("退租危机需要 T-DIR-03 L1 入口（on_crisis 未接线）")
                seqs.append(await on_crisis(cal, fire_time, aid, int(entry["owed_cents"])))
            continue
        # 下个账单周期未结清 → notice（无新结算，overdue_cents = 挂账快照）
        seq = await cal.insert_event(
            type_="economy.bill.rent.notice",
            payload={"agent_id": aid, "overdue_cents": int(entry["owed_cents"]), "period": period},
            sim_time=fire_time, actors=[aid], rng_seed=cal.clock.tick_of(fire_time),
        )
        await _apply_needs(cal, agent_id=aid, deltas={"mood": float(npu["mood_delta"])}, cause=str(seq))
        # 挫败值 +8（tension 无对象可挂 → 挫败值，01 §1.5/§3.3）：落 goals.frustration（持久化位）
        await cal.pool.execute(
            "UPDATE goals SET frustration = frustration + $2 WHERE agent_id=$1 AND status='active'",
            aid, int(npu["frustration_delta"]))
        entry["periods"].append(period)
        await _set_overdue(cal, aid, state)
        seqs.append(seq)
        log.warning("%s 欠租约谈通知（period=%s 挂账 %d）", aid, period, entry["owed_cents"])
    return seqs


async def find_vacant_room(cal: CalendarEngine) -> tuple[int, str] | None:
    """最低价位空房查找（01 §1.5 强制搬迁目标；独立函数便于测试替换无空房分支）。"""
    rooms_cfg = cal.cfg["locations"]["apartment"]["rooms"]
    rent_tiers = sorted(cal.cfg["economy"]["rent"]["by_floor"].values(), key=lambda t: int(t["amount_cents"]))
    occupied = {r["room_no"] for r in await cal.pool.fetch("SELECT room_no FROM agents WHERE room_no IS NOT NULL")}
    for tier in rent_tiers:  # 低价位优先
        for floor in sorted(int(f) for f in tier["floors"]):
            for suffix in sorted(int(s) for s in rooms_cfg["rooms_per_floor"]):
                room_no = f"{floor}{suffix:02d}"
                if room_no not in occupied:
                    return floor, room_no
    return None


async def _force_move(cal: CalendarEngine, fire_time: dt.datetime, agent_id: str) -> bool:
    """强制搬迁至最低价位空房（01 §1.5）；成功改 room_no/position 并写记忆（有 gateway 时）。返回是否有空房。"""
    target = await find_vacant_room(cal)
    if target is None:
        return False
    floor, room_no = target
    await cal.pool.execute(
        "UPDATE agents SET room_no=$2, position=$3 WHERE id=$1",
        agent_id, room_no, f"apt.L{floor}.{room_no}")
    log.warning("%s 连续 2 周期欠租：强制搬迁至 %s", agent_id, room_no)
    if cal.gateway is not None:
        from ..memory.store import insert_memory

        await insert_memory(
            cal.pool, cal.gateway, agent_id=agent_id, sim_time=fire_time, kind="event",
            content=f"因连续欠租被房东要求搬到 {room_no}（低价位房）。", importance=7,
            rng_seed=cal.clock.tick_of(fire_time))
    return True


async def settle_debt_overdue(cal: CalendarEngine, fire_time: dt.datetime, *,
                              relations_cfg: dict[str, Any]) -> None:
    """债务逾期日结算（04 §6.2）：扫 `debts` 逾期未结清，按 01 §3.2 逾期行逐日
    （数值读 relations.yaml `lend_overdue_daily` 镜像）落 relation.changed；还清即止。"""
    rule = relations_cfg["matrix"]["lend_overdue_daily"]
    rows = await cal.pool.fetch(
        "SELECT id, a_id, b_id FROM debts WHERE due_sim < $1 AND repaid_cents < amount_cents ORDER BY id",
        fire_time)
    if not rows:
        return
    cause = str(await cal.pool.fetchval("SELECT coalesce(max(seq), 0) FROM events"))
    for r in rows:
        if cal.agg is not None:
            # a_id=债主 / b_id=欠款人（round2 §A.18 口径）：债主对欠款人逐日 -2/+2
            await cal.agg.apply_relation_delta(
                a_id=r["a_id"], b_id=r["b_id"],
                delta_affinity=int(rule["delta_affinity"]), delta_tension=int(rule["delta_tension"]),
                cause=cause)


def register_overdue_jobs(cal: CalendarEngine, *, relations_cfg: dict[str, Any],
                          on_crisis: OnCrisis | None = None) -> None:
    """欠费链路排程注册（T-WA-04）：
    - 房租账单日先跑升级阶梯（notice/搬迁/危机）再结新账（注册序保证）；
    - 每日扫描：结清补扣 + 债务逾期日结算。"""
    rent_day = int(cal.cfg["economy"]["rent"]["bill_day"])

    async def _escalation(c: CalendarEngine, t: dt.datetime) -> None:
        await settle_overdue_escalation(c, t, on_crisis=on_crisis)

    async def _daily(c: CalendarEngine, t: dt.datetime) -> None:
        await try_settle_overdue(c, t)
        await settle_debt_overdue(c, t, relations_cfg=relations_cfg)

    cal.register_job("economy.bill.rent.escalation", "08:55", lambda d: d.day == rent_day, _escalation)
    cal.register_job("economy.overdue.daily", OVERDUE_SCAN_TIME, lambda d: True, _daily)


# ---- 注册 ---------------------------------------------------------------------


def register_economy_jobs(cal: CalendarEngine, *, on_shortfall: OnShortfall | None = None) -> None:
    """注册三类固定日历经济事件进 T-WA-02 回调表（触发日历全部读配置，01 §6.1）。

    `on_shortfall` 缺省 = `settle_bill_shortfall`（T-WA-04 欠费链路）；显式传 None 表示
    不接欠费链（余额不足即报错，仅测试隔离口径）。"""
    if on_shortfall is None:
        on_shortfall = settle_bill_shortfall
    cfg = cal.cfg["economy"]
    payday = int(cfg["salary"]["payday"])
    rent_day = int(cfg["rent"]["bill_day"])
    util_day = int(cfg["utility"]["bill_day"])

    async def _payroll(c: CalendarEngine, t: dt.datetime) -> None:
        await settle_payroll(c, t)

    async def _rent(c: CalendarEngine, t: dt.datetime) -> None:
        await settle_bill(c, t, kind="rent", on_shortfall=on_shortfall)

    async def _utility(c: CalendarEngine, t: dt.datetime) -> None:
        await settle_bill(c, t, kind="utility", on_shortfall=on_shortfall)

    cal.register_job("economy.payroll", "10:00", lambda d: d.day == payday, _payroll)
    cal.register_job("economy.bill.rent", "09:00", lambda d: d.day == rent_day, _rent)
    cal.register_job("economy.bill.utility", "09:00", lambda d: d.day == util_day, _utility)
