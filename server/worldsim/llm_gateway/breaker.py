"""撞墙判定/降级链熔断 + 成本熔断两档（03 文档 T-LLM-06/T-LLM-08；04 §8.2/§8.4，06 §1.2）。

- `FailoverBreaker`（T-LLM-06）：撞墙三条件任一满足即沿路由表降级链切换（04 §8.2 逐字）——
  ①连续 3 次 HTTP 429/5xx（重试耗尽后计，即 WallSignal）；②滑动 5 分钟窗口 RPM 使用率 >90%；
  ③滑动 5 分钟 p95 延迟 >8s。冷却 30 分钟后单请求探测，成功即切回。
  切换与恢复各落 `system.llm.failover`（source/trigger=system、internal、payload 四键
  `{task_type, from_provider, to_provider, reason}` 逐字 06 §1.2，不含 text_display）。
- `ThrottleState` + `CostBreaker`（T-LLM-08，判级器唯一实现，评审 R1 §A.5）：滚动 7 模拟日
  ¥/模拟日 ÷ 基线（`WSIM_COST_LIMIT_CNY_PER_SIMDAY`）越线分级 alarm/throttle/recover，
  迁移落 `system.llm.throttle`（payload 仅 `{level, ratio}` 两键，06 §1.2）；
  基线未回填期统一**只告警不动作**（与 08 文档口径一致）。降速五动作的读取点接线归 08 T-OPS-02。
- 本模块不 import scheduler/adjudicator/world_agent（04 §2.1 单向依赖）；事件落库自写最小 INSERT。
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque

from .clients import Clock, RealClock

log = logging.getLogger(__name__)

# 撞墙判定阈值（04 §8.2 逐字；数字唯一持有方 = 04 §8.2，此处为工程镜像）
TRIP_CONSECUTIVE_ERRORS = 3        # ① 连续 3 次 429/5xx（重试耗尽后计）
WINDOW_S = 300.0                   # ②③ 滑动 5 分钟窗口
RPM_USAGE_TRIP = 0.9               # ② RPM 使用率 >90%
P95_LATENCY_TRIP_MS = 8000         # ③ p95 延迟 >8s
COOLDOWN_S = 1800.0                # 冷却 30 分钟后单请求探测恢复

COST_LIMIT_ENV = "WSIM_COST_LIMIT_CNY_PER_SIMDAY"
SIM_DAY_S = 86400.0                # 持续条件 1 模拟日（04 §8.4）按模拟秒
THROTTLE_PAUSE_REAL_H = 24.0       # 降速档 24 真实小时仍超线 → 转暂停时钟（04 §8.4；消费归 08 T-OPS-03）


async def insert_system_event(
    db: Any,
    *,
    tick: int,
    sim_time: Any,
    event_type: str,
    payload: dict[str, Any],
    rng_seed: int,
) -> int | None:
    """system 域 internal 事件最小落库（06 §1.2：source/trigger=system、不携带展示文本）。"""
    if db is None:
        return None
    return await db.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, rng_seed, visibility, payload)
        VALUES ($1, $2, $3, 'system', 'system', '{}', $4, 'internal', $5::jsonb)
        RETURNING seq
        """,
        tick, sim_time, event_type, rng_seed, json.dumps(payload, ensure_ascii=False),
    )


class FailoverBreaker:
    """GLM 撞墙判定机（04 §8.2）：三条件命中 → trip；冷却 30min → 探测恢复。

    判定键 = 端点（`provider别名/model`，03 §6 D45）：429/RPM 撞墙实测为账号级（1302），
    但 p95 延迟是型号级指标；端点级键控避免明星档慢调用误伤次要档（两型号独立 trip/探测恢复，
    账号级撞墙时两端点各自 trip、各自探测，语义等价）。
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or RealClock()
        # provider → 窗口内尝试流 (t, kind, latency_ms)；kind ∈ ok/429/5xx
        self._window: dict[str, Deque[tuple[float, str, int]]] = {}
        self._consec_wall: dict[str, int] = {}
        self._tripped_at: dict[str, float] = {}  # provider → trip 时刻（在 tripped 集合中即降级中）

    # ---- 数据流输入（T-LLM-05 调用记录流） ---------------------------------

    def record_attempt(self, provider: str, *, kind: str, latency_ms: int, rpm_limit: int | None = None,
                       p95_trip_ms: int | None = None, rpm_usage_trip_pct: int | None = None) -> str | None:
        """记录一次尝试并做撞墙判定；命中三条件之一 → trip 并返回 reason，否则 None。

        `p95_trip_ms`：条件③阈值条目级覆盖（models.yaml 模型条目 `p95_trip_ms`；缺省 8s = 04 §8.2 口径；
        **0 = 条目级停用条件③**（免费档延迟抖动为服务端排队所致，误 trip 实见 2026-09-25，03 §6 D42）。
        `rpm_usage_trip_pct`：条件②阈值条目级覆盖（百分数；缺省 90 = 04 §8.2 口径；**0 = 条目级停用
        条件②**——桶容量即提供方实测上限时，打满桶是限流器本职非提供方撞墙，03 §6 D42）。
        """
        now = self._clock.now()
        w = self._window.setdefault(provider, deque())
        w.append((now, kind, latency_ms))
        while w and now - w[0][0] > WINDOW_S:
            w.popleft()
        if provider in self._tripped_at:
            return None  # 已降级中，探测路径走 probe_result
        if kind in ("429", "5xx"):
            self._consec_wall[provider] = self._consec_wall.get(provider, 0) + 1
        elif kind == "ok":
            self._consec_wall[provider] = 0
        reason = self._check_trip(provider, rpm_limit, p95_trip_ms=p95_trip_ms, rpm_usage_trip_pct=rpm_usage_trip_pct)
        if reason:
            self._tripped_at[provider] = now
        return reason

    def _check_trip(self, provider: str, rpm_limit: int | None, *, p95_trip_ms: int | None = None,
                    rpm_usage_trip_pct: int | None = None) -> str | None:
        if self._consec_wall.get(provider, 0) >= TRIP_CONSECUTIVE_ERRORS:
            return "consecutive_429_5xx"  # 条件①
        w = self._window.get(provider)
        if not w:
            return None
        usage_trip = RPM_USAGE_TRIP if rpm_usage_trip_pct is None else rpm_usage_trip_pct / 100.0
        if rpm_limit and usage_trip > 0:  # 条件②：窗口 RPM 使用率 >90%（0 = 条目级停用，03 §6 D42）
            usage = len(w) / (rpm_limit * WINDOW_S / 60.0)
            if usage > usage_trip:
                return "rpm_usage_over_90pct"
        if p95_trip_ms == 0:
            return None  # 条目级停用条件③（D42）
        p95_limit = p95_trip_ms if p95_trip_ms is not None else P95_LATENCY_TRIP_MS
        lat = sorted(lat for _, _, lat in w)
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        if len(lat) >= 2 and p95 > p95_limit:  # 条件③：窗口 p95 超阈（04 §8.2；条目级覆盖 D42）
            return "p95_latency_over_8s"
        return None

    # ---- 状态查询 -----------------------------------------------------------

    def is_tripped(self, provider: str) -> bool:
        return provider in self._tripped_at

    def tripped_providers(self) -> list[str]:
        return list(self._tripped_at)

    def due_for_probe(self, provider: str) -> bool:
        """冷却 30 分钟满 → 允许单请求探测（04 §8.2）。"""
        at = self._tripped_at.get(provider)
        return at is not None and self._clock.now() - at >= COOLDOWN_S

    def probe_result(self, provider: str, *, ok: bool) -> bool:
        """探测结果：成功 → 切回（返回 True）；失败 → 重新起冷却。"""
        if provider not in self._tripped_at:
            return False
        if ok:
            del self._tripped_at[provider]
            self._consec_wall[provider] = 0
            self._window.pop(provider, None)
            return True
        self._tripped_at[provider] = self._clock.now()  # 探测失败，冷却重计
        return False

    async def emit_failover(
        self,
        db: Any,
        *,
        task_type: str,
        from_provider: str,
        to_provider: str,
        reason: str,
        tick: int,
        sim_time: Any,
        rng_seed: int,
    ) -> int | None:
        """切换/恢复各落一条 `system.llm.failover`（payload 四键逐字 06 §1.2，不含 text_display）。"""
        seq = await insert_system_event(
            db, tick=tick, sim_time=sim_time, event_type="system.llm.failover",
            payload={
                "task_type": task_type,
                "from_provider": from_provider,
                "to_provider": to_provider,
                "reason": reason,
            },
            rng_seed=rng_seed,
        )
        log.warning("LLM 降级切换：%s %s → %s（%s）", task_type, from_provider, to_provider, reason)
        return seq


# ---------------------------------------------------------------------------
# T-LLM-08 成本熔断两档（判级器 + ThrottleState + system.llm.throttle 唯一归此，评审 R1 §A.5）
# ---------------------------------------------------------------------------


@dataclass
class ThrottleState:
    """降速五动作开关对象（04 §8.4；读取点接线归 08 T-OPS-02，本侧只保证状态机正确）。"""

    level: str = "normal"               # normal / alarm / throttle
    secondary_force_lowest: bool = False  # 动作① secondary/bgsummary 强制最低档别名（开发期适配，03 §6 D1）
    director_call_factor: float = 1.0     # 动作② 编剧调用减半 → 0.5
    dialogue_daily_cap_factor: float = 1.0  # 动作③ 每模拟日对话场次上限 ×60%
    reflection_threshold: int = 20        # 动作④ 深度反思阈值 20→35（基值镜像 models.yaml thresholds.reflection）
    alarm_escalated: bool = False         # 动作⑤ 告警升级
    needs_clock_pause: bool = False       # 降速 24 真实小时仍超线 → 转 clock.pause（消费归 08 T-OPS-03）


class CostBreaker:
    """成本熔断判级器（04 §8.4；基线 env `WSIM_COST_LIMIT_CNY_PER_SIMDAY`，倍数镜像 models.yaml）。

    `evaluate(ratio, sim_now)` 由滚动窗口聚合（T-LLM-07）驱动；级别迁移落
    `system.llm.throttle`（payload 仅 `{level, ratio}`，06 §1.2）。基线未配置 → 只告警不动作。
    """

    def __init__(
        self,
        db: Any,
        thresholds: dict[str, Any],
        *,
        clock: Clock | None = None,
        baseline_cny: float | None = None,
        reflection_base: int = 20,
    ) -> None:
        cb = (thresholds or {}).get("cost_breaker", {})
        self._alarm_ratio = float(cb.get("alarm_ratio", 1.5))
        self._throttle_ratio = float(cb.get("throttle_ratio", 2.0))
        self._recover_ratio = float(cb.get("recover_ratio", 1.3))
        self._db = db
        self._clock = clock or RealClock()
        if baseline_cny is None:
            raw = os.environ.get(COST_LIMIT_ENV)
            baseline_cny = float(raw) if raw else None
        self._baseline = baseline_cny
        self._reflection_base = reflection_base
        self.state = ThrottleState(reflection_threshold=reflection_base)
        self._band_since: dict[str, float] = {}  # band → 进入时刻（持续 1 模拟日判定）
        self._throttle_since_real: float | None = None
        self._warned_no_baseline = False

    # ---- 判级 ---------------------------------------------------------------

    async def evaluate(
        self,
        cny_per_simday: float,
        *,
        sim_now: Any,
        tick: int = 0,
        rng_seed: int = 0,
    ) -> str:
        """输入滚动 ¥/模拟日，驱动 alarm → throttle → recover 状态机；返回当前 level。"""
        if not self._baseline or self._baseline <= 0:
            if not self._warned_no_baseline:
                self._warned_no_baseline = True
            log.warning(
                "成本熔断基线 %s 未配置：只告警不动作（当前 ¥/模拟日 %.6f；03 T-LLM-08/08 文档口径）",
                COST_LIMIT_ENV, cny_per_simday,
            )
            return self.state.level
        ratio = cny_per_simday / self._baseline
        now_s = float(sim_now.timestamp()) if hasattr(sim_now, "timestamp") else float(sim_now)

        if ratio >= self._throttle_ratio:
            if self._persisted("throttle", now_s):
                await self._transition("throttle", ratio, sim_now=sim_now, tick=tick, rng_seed=rng_seed)
        elif ratio >= self._alarm_ratio:
            self._band_since.pop("throttle", None)
            if self._persisted("alarm", now_s):
                await self._transition("alarm", ratio, sim_now=sim_now, tick=tick, rng_seed=rng_seed)
        else:
            self._band_since.clear()
        if ratio < self._recover_ratio and self.state.level != "normal":
            # 回落至恢复线下 → 逐级撤销（04 §8.4 恢复行）
            await self._transition("recover", ratio, sim_now=sim_now, tick=tick, rng_seed=rng_seed)
        # 降速档 24 真实小时仍超降速线 → 置暂停时钟标志（04 §8.4；消费归 08 T-OPS-03）
        if self.state.level == "throttle":
            if self._throttle_since_real is None:
                self._throttle_since_real = self._clock.now()
            if self._clock.now() - self._throttle_since_real >= THROTTLE_PAUSE_REAL_H * 3600 and ratio >= self._throttle_ratio:
                self.state.needs_clock_pause = True
        else:
            self._throttle_since_real = None
            self.state.needs_clock_pause = False
        return self.state.level

    def _persisted(self, band: str, now_s: float) -> bool:
        """越线持续 1 模拟日才动作（04 §8.4 触发列）。"""
        since = self._band_since.setdefault(band, now_s)
        return now_s - since >= SIM_DAY_S

    async def _transition(self, target: str, ratio: float, *, sim_now: Any, tick: int, rng_seed: int) -> None:
        s = self.state
        if target == "recover":
            if s.level == "throttle":
                s.level = "alarm"  # 逐级撤销：throttle → alarm
                s.secondary_force_lowest = False
                s.director_call_factor = 1.0
                s.dialogue_daily_cap_factor = 1.0
                s.reflection_threshold = self._reflection_base
                s.alarm_escalated = False
            else:
                s.level = "normal"  # alarm → normal
            s.needs_clock_pause = False
            self._throttle_since_real = None
        elif target == "alarm":
            if s.level in ("normal",):
                s.level = "alarm"  # 内核不变（04 §8.4 报警行）
        elif target == "throttle":
            if s.level != "throttle":
                s.level = "throttle"
                s.secondary_force_lowest = True       # 动作①（开发期 = 强制 GLM 最低档别名，03 §6 D1）
                s.director_call_factor = 0.5          # 动作② 编剧调用减半
                s.dialogue_daily_cap_factor = 0.6     # 动作③ 对话场次上限 ×60%
                s.reflection_threshold = 35           # 动作④ 深度反思阈值 20→35
                s.alarm_escalated = True              # 动作⑤ 告警升级
        else:  # pragma: no cover
            return
        await insert_system_event(
            self._db, tick=tick, sim_time=sim_now, event_type="system.llm.throttle",
            payload={"level": target, "ratio": round(ratio, 4)},  # payload 仅两键（06 §1.2）
            rng_seed=rng_seed,
        )
        log.warning("成本熔断级别迁移 → %s（ratio=%.3f，04 §8.4）", target, ratio)
