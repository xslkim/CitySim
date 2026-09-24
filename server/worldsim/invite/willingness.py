"""willingness 意愿分计算（02 T-REL-05；01 §3.5 唯一持有方 / R2 §A.9 计算点）。

意愿分为**策划口径——供 prompt 注入与观测，不做判定**（01 §3.5：邀约/请求接受率由 LLM 人格驱动，
系统不硬改判）。公式各项系数、人格/场合/活动偏好修正与 clamp 钳位区间一律读
`config/relations.yaml` `willingness:` 段（01 §3.5 镜像），代码零硬编码（00 §7 DoD 4/6）：

    W_raw = base + affinity_coef×(affinity/100) + tension_coef×(tension/100)
          + social_coef×(B社交/100) + mood_coef×(B情绪/100)
          + 人格修正(A高/E高/N高) + 场合修正(周末节假日/工作日深夜) + 活动偏好匹配(±)
    意愿分 = clamp(W_raw × 100, 0, 100)   # 钳位 [0,100]（评审 P2-11）

被放鸽 48h 内接受先验 -0.15（01 §5.3）经 `prior_penalty` 由调用方（invite 状态机）从事件流注入。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

_LATE_NIGHT_DEFAULT = ("22:00", "24:00")  # "深夜"设计未给区间时的兜底（yaml 已镜像，D15）


@dataclass(frozen=True)
class Willingness:
    """意愿分结果（score = clamp 后 0~100；raw = W_raw；factors 供 prompt 注入与观测拆解）。"""

    score: float
    raw: float
    factors: dict[str, float] = field(default_factory=dict)


def _hhmm(s: str) -> int:
    hh, mm = str(s).split(":")
    return int(hh) * 60 + int(mm)


def compute_willingness(
    cfg: dict[str, Any],
    *,
    affinity: float,
    tension: float,
    social_need: float,
    mood: float,
    big_five: dict[str, Any] | None = None,
    sim_now: dt.datetime | None = None,
    activity_match: float = 0.0,
    prior_penalty: float = 0.0,
) -> Willingness:
    """意愿分计算（逐项系数读 cfg=relations.yaml willingness 段，01 §3.5）。

    - `activity_match` ∈ [-1, 1]（活动偏好匹配度，映射 ±activity_match 系数）；
    - `prior_penalty` ≤ 0（如被放鸽 48h 内 -0.15，01 §5.3，由 invite 状态机计算注入）；
    - `sim_now` 仅用于场合修正（周末/工作日深夜；只读 sim_time，00 §4 红线 11）。
    """
    p = cfg["persona"]
    o = cfg["occasion"]
    factors: dict[str, float] = {}
    raw = float(cfg["base"])
    factors["affinity"] = float(cfg["affinity_coef"]) * (affinity / 100.0)
    factors["tension"] = float(cfg["tension_coef"]) * (tension / 100.0)
    factors["social"] = float(cfg["social_coef"]) * (social_need / 100.0)
    factors["mood"] = float(cfg["mood_coef"]) * (mood / 100.0)
    raw += factors["affinity"] + factors["tension"] + factors["social"] + factors["mood"]
    if big_five:
        high = float(p["high_threshold"])
        persona_delta = 0.0
        if float(big_five.get("agreeableness", 0)) >= high:
            persona_delta += float(p["agreeableness_high"])
        if float(big_five.get("extraversion", 0)) >= high:
            persona_delta += float(p["extraversion_high"])
        if float(big_five.get("neuroticism", 0)) >= high:
            persona_delta += float(p["neuroticism_high"])
        factors["persona"] = persona_delta
        raw += persona_delta
    if sim_now is not None:
        occasion_delta = 0.0
        minute = sim_now.hour * 60 + sim_now.minute
        late = o.get("late_night", _LATE_NIGHT_DEFAULT)
        if sim_now.weekday() >= 5:  # 周末/节假日 +0.10（节假日日历 M3 接 world_agent，M1 周末口径）
            occasion_delta += float(o["weekend_or_holiday"])
        elif _hhmm(late[0]) <= minute < _hhmm(late[1]):  # 工作日深夜 -0.10
            occasion_delta += float(o["workday_late_night"])
        factors["occasion"] = occasion_delta
        raw += occasion_delta
    match_delta = max(-1.0, min(1.0, activity_match)) * float(cfg["activity_match"])
    factors["activity_match"] = match_delta
    factors["prior_penalty"] = min(0.0, prior_penalty)
    raw += match_delta + factors["prior_penalty"]
    lo, hi = float(cfg["clamp"][0]), float(cfg["clamp"][1])
    return Willingness(score=max(lo, min(hi, raw * 100.0)), raw=raw, factors=factors)
