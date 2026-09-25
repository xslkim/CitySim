"""随机扰动四发生器 `world.disturb.*`（04 T-WA-09；01 §6.1 概率与模板；06 §1.2/§1.1）。

- 每日按日历判定（roll 时点 = 07:00，早于 8:00 打卡内部结算使 illness 请假当日生效；工程默认）；
  全部骰子 seed 落 `events.rng_seed`（04 §5.3：seed = 结算 tick 派生，可复现）。
- 导演 L0 复用同一实现、仅 `trigger='director'` 不同（06 §1.1；T-DIR-03 经 `fire_*` 入口传参指定）。
- lucky 含金额必有 `amount_cents`（正入账）且仅此时打 economy 审计标记（06 §1.2）；金额入账
  （正）+ 财富/情绪变更（映射公式 01 §1.5/§6.1，读配置）落 `state.needs_delta`。
- illness 请假标记 `world_state` `leave.<agent_id>`（D-06），供校验器/排期豁免工作约束（01 §1.4）
  与 T-WA-02 打卡出勤消费；weather 的 `effect` = 效果码+短文案（设计未定义，工程默认 D-14）。
"""

from __future__ import annotations

import datetime as dt
import logging
import random
from typing import Any

from .calendar import CalendarEngine
from .economy import wealth_income_delta

log = logging.getLogger(__name__)

DISTURB_ROLL_TIME = "07:00"  # 每日扰动判定时点（工程默认；早于 8:00 打卡使 illness 当日生效）

_LEAVE_KEY = "leave.{}"


# ---- 各发生器点火（导演/测试直调入口；trigger 口径 06 §1.1） ----------------------


async def fire_illness(cal: CalendarEngine, sim_time: dt.datetime, *, agent_id: str,
                       severity: int, days: int, trigger: str = "world") -> int:
    """`world.disturb.illness`：payload `{agent_id, severity, days}` 逐字 06 §1.2；写请假标记。"""
    seed = cal.clock.tick_of(sim_time)
    seq = await cal.insert_event(
        type_="world.disturb.illness",
        payload={"agent_id": agent_id, "severity": severity, "days": days},
        sim_time=sim_time, actors=[agent_id], rng_seed=seed, trigger=trigger,
    )
    until = sim_time.date() + dt.timedelta(days=days - 1)
    await cal.set_state(_LEAVE_KEY.format(agent_id), {"until": until.isoformat(), "for_event": str(seq)})
    log.info("illness：%s severity=%d 请假至 %s", agent_id, severity, until)
    return seq


async def fire_weather(cal: CalendarEngine, sim_time: dt.datetime, *, kind: str,
                       trigger: str = "world") -> int:
    """`world.disturb.weather`：payload `{kind, effect}`；effect = 效果码+短文案（D-14），
    邀约取消联动由 02 文档决策侧消费。"""
    effect_map = {  # 效果码 → 短文案（工程默认 D-14；文案非阈值数字）
        "暴雨": "rain:邀约取消率上升，可能困在公司",
        "大雪": "snow:通勤受阻，晚间留守增多",
        "高温": "heat:外出意愿下降，室内聚集",
    }
    seed = cal.clock.tick_of(sim_time)
    return await cal.insert_event(
        type_="world.disturb.weather",
        payload={"kind": kind, "effect": effect_map.get(kind, f"other:{kind}影响")},
        sim_time=sim_time, rng_seed=seed, trigger=trigger,
    )


async def fire_complaint(cal: CalendarEngine, sim_time: dt.datetime, *, floor: int, issue: str,
                         trigger: str = "world") -> int:
    """`world.disturb.complaint`：payload `{floor, issue}`（随机 + 编剧 L0 入口，后者 trigger='director'）。"""
    seed = cal.clock.tick_of(sim_time)
    return await cal.insert_event(
        type_="world.disturb.complaint",
        payload={"floor": floor, "issue": issue},
        sim_time=sim_time, rng_seed=seed, trigger=trigger,
    )


async def fire_lucky(cal: CalendarEngine, sim_time: dt.datetime, *, agent_id: str, kind: str,
                     amount_cents: int, trigger: str = "world") -> int:
    """`world.disturb.lucky`：payload `{agent_id, kind, amount_cents}` 逐字 06 §1.2；
    金额入账（正）+ 财富/情绪变更（01 §1.5 收入映射 + lucky_mood_delta）落 needs_delta。"""
    seed = cal.clock.tick_of(sim_time)
    seq = await cal.insert_event(
        type_="world.disturb.lucky",
        payload={"agent_id": agent_id, "kind": kind, "amount_cents": amount_cents},
        sim_time=sim_time, actors=[agent_id], rng_seed=seed, trigger=trigger,
    )
    await cal.pool.execute("UPDATE agents SET balance_cents = balance_cents + $2 WHERE id=$1",
                           agent_id, amount_cents)
    if cal.agg is not None:
        mood_delta = float(cal.cfg["triggers"]["disturb"]["lucky_mood_delta"])
        await cal.agg.apply_needs_delta(agent_id=agent_id, need="wealth",
                                        delta=wealth_income_delta(cal.cfg, amount_cents), cause=str(seq))
        await cal.agg.apply_needs_delta(agent_id=agent_id, need="mood", delta=mood_delta, cause=str(seq))
    log.info("lucky：%s %s +%d 分", agent_id, kind, amount_cents)
    return seq


# ---- 每日判定（注册进 T-WA-02 日历回调表） ---------------------------------------


async def roll_daily_disturb(cal: CalendarEngine, fire_time: dt.datetime) -> list[int]:
    """四类扰动每日 roll（概率全部读 triggers.disturb，01 §6.1 镜像；骰子 seed 可复现）。"""
    cfg = cal.cfg["triggers"]["disturb"]
    seed = cal.clock.tick_of(fire_time)
    day = fire_time.date()
    seqs: list[int] = []
    rng = random.Random(f"{seed}|disturb|{day.isoformat()}")
    agents = [r["id"] for r in await cal.pool.fetch("SELECT id FROM agents ORDER BY id")]
    # illness：每人每日独立判定（01 §6.1）
    sev_days = {int(k): (int(v[0]), int(v[1])) for k, v in cfg["illness_severity_days"].items()}
    for aid in agents:
        if rng.random() < float(cfg["illness_prob"]):
            severity = rng.choice(sorted(sev_days))
            lo, hi = sev_days[severity]
            seqs.append(await fire_illness(cal, fire_time, agent_id=aid,
                                           severity=severity, days=rng.randint(lo, hi)))
    # weather：每日一次
    if rng.random() < float(cfg["weather_prob"]):
        seqs.append(await fire_weather(cal, fire_time,
                                       kind=rng.choice([str(x) for x in cfg["weather_kinds"]])))
    # complaint：每日一次（另有编剧 L0 入口）
    if rng.random() < float(cfg["complaint_prob"]):
        floors = [int(f) for f in cal.cfg["locations"]["apartment"]["rooms"]["floors"]]
        seqs.append(await fire_complaint(cal, fire_time, floor=rng.choice(floors),
                                         issue=rng.choice([str(x) for x in cfg["complaint_issues"]])))
    # lucky：每日一次，随机一名 agent（金额区间读配置）
    if rng.random() < float(cfg["lucky_prob"]):
        lo, hi = int(cfg["lucky_amount_cents"]["min"]), int(cfg["lucky_amount_cents"]["max"])
        seqs.append(await fire_lucky(cal, fire_time, agent_id=rng.choice(agents),
                                     kind=rng.choice([str(x) for x in cfg["lucky_kinds"]]),
                                     amount_cents=rng.randint(lo, hi)))
    return seqs


def register_disturb_jobs(cal: CalendarEngine) -> None:
    """注册四类扰动每日判定（T-WA-09；导演复用走 fire_* 同一实现）。"""

    async def _roll(c: CalendarEngine, t: dt.datetime) -> None:
        await roll_daily_disturb(c, t)

    cal.register_job("world.disturb.daily", DISTURB_ROLL_TIME, lambda d: True, _roll)
