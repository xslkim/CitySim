"""六需求衰减引擎（02 T-REL-01；01 §3.1 数值唯一持有方 / 04 §6.2 状态行 / 04 §6.5 聚合事件）。

- 值域/驱动区/强制行为阈、六需求衰减率与满足量数值一律读 `config/needs.yaml`（01 §3.1 镜像），
  代码零硬编码（00 §7 DoD 4）。
- **衰减只读 sim_time**：按经过的模拟时长积分（与 tick 数/压缩比解耦，00 §4 红线 11）；
  睡眠窗 0:30~6:30、工作时段工作日 9:00~18:00（01 §1.4/§5.1，needs.yaml `schedule` 段）。
- Big Five 修正系数（01 §3.1 P1 档）：M1 仅中性档 1.0 与接口（02 文档 D9；`bigfive_factor` 预留）。
- 变更经 `StateAggregator` 并入本 tick `state.needs_delta`（04 §6.5），`cause` 指引发事件或系统结算事件。
- 状态行接口（供波次 2b T-ADJ-03 校验器消费）：`forced_action`（精力<10 强制 rest / 饥饿<10 强制 eat）、
  `action_weight`（饥饿 <15 非进食动作降权，倍率见 needs.yaml state_rule，设计未给值 = 工程默认，D14）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import yaml

from ..adjudicator.state_events import NEED_KEYS, StateAggregator

MINUTES_PER_DAY = 24 * 60

_EAT_ACTIONS = {"eat"}  # 进食动作（非进食动作 = 19 动作全集减本集；饥饿降权判定用）


def load_needs_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("thresholds", "decay_per_sim_hour", "schedule", "state_rule"):
        if key not in cfg:
            raise ValueError(f"needs.yaml 缺 {key} 段（01 §3.1 镜像）")
    return cfg


def _hhmm_to_min(s: str) -> int:
    hh, mm = str(s).split(":")
    return int(hh) * 60 + int(mm)


class NeedsEngine:
    """六需求衰减/满足/驱动区判定。`cfg` = needs.yaml 字典。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._cfg = cfg
        self._decay = cfg["decay_per_sim_hour"]
        self._th = cfg["thresholds"]
        self._rule = cfg["state_rule"]
        sleep = cfg["schedule"]["sleep"]
        self._sleep = (_hhmm_to_min(sleep[0]), _hhmm_to_min(sleep[1]))
        work = cfg["schedule"]["work"]
        self._work = (_hhmm_to_min(work["start"]), _hhmm_to_min(work["end"]), frozenset(work["weekdays"]))

    # ---- 时段判定（只读 sim_time） ---------------------------------------------

    def is_sleeping(self, sim_now: dt.datetime) -> bool:
        m = sim_now.hour * 60 + sim_now.minute
        start, end = self._sleep
        return start <= m < end if start < end else (m >= start or m < end)  # 0:30~6:30 跨零点

    def is_working(self, sim_now: dt.datetime) -> bool:
        start, end, weekdays = self._work
        return sim_now.weekday() in weekdays and start <= sim_now.hour * 60 + sim_now.minute < end

    # ---- 衰减积分 ---------------------------------------------------------------

    def decay_rate(self, need: str, sim_now: dt.datetime) -> float:
        """某需求在 sim_now 时刻的瞬时衰减率（/模拟小时，负值 = 衰减）。"""
        if need not in NEED_KEYS:
            raise ValueError(f"未知需求键 {need!r}（01 §3.1 六需求）")
        sleeping = self.is_sleeping(sim_now)
        if need == "hunger":
            return float(self._decay["hunger"]["sleeping" if sleeping else "awake"])
        if need == "energy":
            if sleeping:
                return float(self._decay["energy"]["sleeping"])  # 睡眠恢复经 rest 满足量结算，不在被动衰减
            rate = float(self._decay["energy"]["awake"])
            if self.is_working(sim_now):
                rate += float(self._decay["energy"]["working_extra"])
            return rate
        if need == "mood":
            return float(self._decay["mood"]["base"])
        if need == "social":
            return float(self._decay["social"]["base"])
        if need == "achievement":
            return float(self._decay["achievement"]["workday_daytime"]) if self.is_working(sim_now) else 0.0
        return float(self._decay["wealth"]["base"])  # wealth：不衰减，事件驱动

    def integrate(self, need: str, from_sim: dt.datetime, to_sim: dt.datetime, *, big_five: dict[str, Any] | None = None) -> float:
        """[from_sim, to_sim) 的衰减量积分（负值）；逐小时分段积分跨睡眠/工作边界。

        Big Five 修正（P1）：M1 中性档 1.0（`bigfive_factor`），接口预留。
        """
        if to_sim < from_sim:
            raise ValueError("to_sim 不得早于 from_sim（sim_time 域，00 §4 红线 11）")
        total = 0.0
        cursor = from_sim
        while cursor < to_sim:
            # 步进至下一 :00/:30 网格（睡眠窗 0:30/6:30 与工作时段 9:00/18:00 边界均落在半点网格上）
            base = cursor.replace(second=0, microsecond=0)
            nxt = base.replace(minute=0) + dt.timedelta(hours=1)
            if base.minute < 30:
                half = base.replace(minute=30)
                if half > cursor:
                    nxt = half
            nxt = min(nxt, to_sim)
            hours = (nxt - cursor).total_seconds() / 3600
            total += self.decay_rate(need, cursor) * hours
            cursor = nxt
        return total * self.bigfive_factor(need, big_five)

    def bigfive_factor(self, need: str, big_five: dict[str, Any] | None) -> float:
        """Big Five 衰减修正系数（01 §3.1 P1 档）：M1 中性档 1.0（02 文档 D9；系数表读取路径预留）。"""
        return 1.0

    def satisfy_amount(self, need: str, method: str, *, big_five: dict[str, Any] | None = None) -> float:
        """满足量查询（01 §3.1 满足量列；±20% Big Five 修正 P1，M1 中性 1.0）。"""
        table = self._cfg.get("satisfy", {}).get(need, {})
        if method not in table:
            raise KeyError(f"needs.yaml satisfy.{need} 无方法 {method!r}")
        return float(table[method]) * (self.bigfive_factor(need, big_five) if method != "criticized" else 1.0)

    # ---- 结算（经聚合器并入本 tick state.needs_delta，04 §6.5） -------------------

    async def settle_decay(
        self, agg: StateAggregator, *, agent_id: str, from_sim: dt.datetime, to_sim: dt.datetime, cause: str,
        big_five: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """对 agent 结算 [from,to) 的六需求被动衰减；返回实际变更记录列表。"""
        changes: list[dict[str, Any]] = []
        for need in NEED_KEYS:
            delta = self.integrate(need, from_sim, to_sim, big_five=big_five)
            if delta == 0.0:
                continue
            change = await agg.apply_needs_delta(agent_id=agent_id, need=need, delta=delta, cause=cause)
            if change is not None:
                changes.append(change)
        return changes

    # ---- 驱动区 / 强制行为 / 意图降权（04 §6.2 状态行；T-ADJ-03 消费面） -----------

    def zone(self, value: float) -> str:
        """需求档位：'forced'（<10 强制行为）/ 'drive'（<25 驱动区）/ 'normal'。"""
        if value < self._th["forced_below"]:
            return "forced"
        if value < self._th["drive_zone_below"]:
            return "drive"
        return "normal"

    def forced_action(self, needs: dict[str, float]) -> str | None:
        """强制行为方向（低值 = 匮乏，评审二轮 P2-3 口径）：精力 <10 → rest 优先，饥饿 <10 → eat。"""
        if float(needs.get("energy", 100)) < self._rule["energy_forced_rest_below"]:
            return "rest"
        if float(needs.get("hunger", 100)) < self._rule["hunger_forced_eat_below"]:
            return "eat"
        return None

    def action_weight(self, needs: dict[str, float], action_type: str) -> float:
        """意图权重修正（饥饿 <15 非进食动作降权，04 §6.2 状态行；倍率设计未给值 = 工程默认，D14）。"""
        if action_type in _EAT_ACTIONS:
            return 1.0
        if float(needs.get("hunger", 100)) < self._rule["hunger_downweight_below"]:
            return float(self._rule["non_eat_downweight_factor"])
        return 1.0
