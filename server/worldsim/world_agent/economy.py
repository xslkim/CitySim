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


# ---- 注册 ---------------------------------------------------------------------


def register_economy_jobs(cal: CalendarEngine, *, on_shortfall: OnShortfall | None = None) -> None:
    """注册三类固定日历经济事件进 T-WA-02 回调表（触发日历全部读配置，01 §6.1）。"""
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
