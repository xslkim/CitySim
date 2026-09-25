"""RPM 令牌桶、优先级抢占、指数退避与超时（03 文档 T-LLM-05；04 §8.5/§8.2）。

- 每 provider 一个令牌桶，容量 = models.yaml 该 provider `rpm_limit`（核实值，T-LLM-12 回填）。
- 优先级序钉死（04 §8.5）：`safety > star_decision > dialogue > secondary > bgsummary > director`；
  桶空时低优先级排队、高优先级可抢占低优先级**待发**请求（待令牌的 waiter 按优先级出队）。
- 重试（04 §8.5 逐字）：指数退避 1s/2s/4s 最多 3 次、±20% 抖动；429 优先读 `Retry-After`；
  耗尽不抛业务异常，返回 `WallSignal` 撞墙信号供 T-LLM-06 走降级链（04 §8.2）。
- 每次尝试（含失败）产生一条 `AttemptRecord`（status=ok/retry/failed），交 T-LLM-07 落库。
- 时钟/抖动随机源可注入（`clock`/`rng`），保证测试确定性；并发在网关内、落库串行由裁决协程保证
  （00 §4 红线 10）。
"""

from __future__ import annotations

import heapq
import itertools
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .providers.base import ChatProvider, ChatResult, Message
from .providers.zhipu import ClientError, RateLimited, ServerError, ZhipuError

log = logging.getLogger(__name__)

# 任务优先级序（04 §8.5 逐字；数值小 = 优先级高）
PRIORITY: dict[str, int] = {
    "safety": 0, "star_decision": 1, "dialogue": 2,
    "secondary": 3, "bgsummary": 4, "director": 5,
}
DEFAULT_PRIORITY = 3  # reflection/world_copy/embed 等未列入优先级序的 task_type 取中档（工程默认）

BACKOFF_BASE_S: tuple[float, ...] = (1.0, 2.0, 4.0)  # 指数退避序列（04 §8.5）
JITTER = 0.2                                          # ±20% 抖动（04 §8.5）
MAX_RETRIES = 3                                       # 最多重试 3 次（04 §8.5）→ 总尝试 ≤4


class Clock(Protocol):
    """可注入时钟（测试用 fake clock 保确定性）。"""

    def now(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class AttemptRecord:
    """单次尝试留痕（含失败；status 枚举 = llm_calls DDL 注释 ok/retry/failed/fallback）。"""

    status: str  # ok / retry / failed
    latency_ms: int
    error: str | None = None
    retry_after_s: float | None = None


@dataclass(frozen=True)
class WallSignal:
    """撞墙信号（重试耗尽，04 §8.2）：交降级链，不作业务异常上抛。"""

    reason: str
    attempts: tuple[AttemptRecord, ...]


@dataclass(order=True)
class _Waiter:
    priority: int
    seq: int
    owner: Any = field(compare=False)


class TokenBucket:
    """RPM 令牌桶：容量 = rpm_limit，匀速补充（容量/60 个每秒）。"""

    def __init__(self, rpm_limit: int, clock: Clock) -> None:
        if rpm_limit <= 0:
            raise ValueError("rpm_limit 必须为正（models.yaml providers.*.rpm_limit，T-LLM-12 回填核实值）")
        self.capacity = float(rpm_limit)
        self._rate = rpm_limit / 60.0
        self._clock = clock
        self._tokens = float(rpm_limit)
        self._last = clock.now()

    def _refill(self) -> None:
        now = self._clock.now()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def try_take(self) -> bool:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def seconds_to_next_token(self) -> float:
        self._refill()
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self._rate

    def usage_ratio(self) -> float:
        """滑动口径的补充指标：当前令牌空缺占比（供撞墙条件②的窗口统计参考，04 §8.2）。"""
        self._refill()
        return 1.0 - self._tokens / self.capacity


class RateLimitedClient:
    """provider 调用包装：令牌桶 + 优先级抢占 + 指数退避（04 §8.5）。

    `call()` 返回 `ChatResult`（成功）或 `WallSignal`（重试耗尽撞墙）；4xx 调用侧错误直接上抛
    `ClientError`（不可重试）。全部尝试的 `AttemptRecord` 累积于 `records` 供 ledger 落库。
    """

    def __init__(
        self,
        provider: ChatProvider,
        *,
        rpm_limit: int,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        max_concurrency: int = 100,
    ) -> None:
        self.provider = provider
        self.clock = clock or RealClock()
        self.bucket = TokenBucket(rpm_limit, self.clock)
        self._rng = rng or random.Random()
        self._pending: list[_Waiter] = []
        self._seq = itertools.count()
        self.records: list[AttemptRecord] = []
        import asyncio

        self._sem = asyncio.Semaphore(max_concurrency)  # 在飞并发闸（models.yaml 模型条目 concurrency）

    async def call(
        self,
        task_type: str,
        messages: list[Message],
        gen_params: dict[str, Any] | None = None,
        *,
        seed: int | None = None,
    ) -> ChatResult | WallSignal:
        outcome, _ = await self.call_tracked(task_type, messages, gen_params, seed=seed)
        return outcome

    async def call_tracked(
        self,
        task_type: str,
        messages: list[Message],
        gen_params: dict[str, Any] | None = None,
        *,
        seed: int | None = None,
    ) -> tuple[ChatResult | WallSignal, list[AttemptRecord]]:
        """call + 本次调用自己的尝试清单（并发共享客户端时不得从共享 records 切片——
        他调用的记录会混入导致重复落库，2026-09-25 真跑实发）。"""
        await self._acquire(PRIORITY.get(task_type, DEFAULT_PRIORITY))
        attempts: list[AttemptRecord] = []
        for attempt in range(MAX_RETRIES + 1):
            started = self.clock.now()
            try:
                async with self._sem:  # 在飞并发闸（免费档并发上限实测口径，T-LLM-12）
                    result = await self.provider.chat(task_type, messages, gen_params, seed=seed)
            except RateLimited as exc:
                attempts.append(self._mark(attempt, started, str(exc), retry_after_s=exc.retry_after_s))
                if attempt >= MAX_RETRIES:
                    break
                await self._sleep_before_retry(attempt, retry_after_s=exc.retry_after_s)
            except (ServerError, ZhipuError) as exc:
                if isinstance(exc, ClientError):
                    attempts.append(self._mark(attempt, started, str(exc)))
                    self.records.extend(attempts)
                    raise  # 4xx 调用侧错误不重试
                attempts.append(self._mark(attempt, started, str(exc)))
                if attempt >= MAX_RETRIES:
                    break
                await self._sleep_before_retry(attempt)
            else:
                attempts.append(AttemptRecord(status="ok", latency_ms=self._elapsed(started)))
                self.records.extend(attempts)
                return result, list(attempts)
        self.records.extend(attempts)
        last = attempts[-1]
        return WallSignal(reason=last.error or "retries exhausted", attempts=tuple(attempts)), list(attempts)

    # ---- 内部 -----------------------------------------------------------

    async def _acquire(self, priority: int) -> None:
        """桶空时排队；待发 waiter 按优先级出队（高优先级抢占低优先级待发请求，04 §8.5）。"""
        waiter = _Waiter(priority, next(self._seq), owner=object())
        heapq.heappush(self._pending, waiter)
        try:
            while True:
                if self._pending[0] is waiter and self.bucket.try_take():
                    heapq.heappop(self._pending)
                    return
                await self.clock.sleep(max(self.bucket.seconds_to_next_token(), 1e-6))
        finally:
            if waiter in self._pending:
                self._pending.remove(waiter)
                heapq.heapify(self._pending)

    def _elapsed(self, started: float) -> int:
        return int((self.clock.now() - started) * 1000)

    def _mark(self, attempt: int, started: float, error: str, *, retry_after_s: float | None = None) -> AttemptRecord:
        status = "retry" if attempt < MAX_RETRIES else "failed"
        return AttemptRecord(status=status, latency_ms=self._elapsed(started), error=error, retry_after_s=retry_after_s)

    async def _sleep_before_retry(self, attempt: int, *, retry_after_s: float | None = None) -> None:
        if retry_after_s is not None:
            await self.clock.sleep(retry_after_s)  # 429 优先读 Retry-After（04 §8.5）
            return
        base = BACKOFF_BASE_S[min(attempt, len(BACKOFF_BASE_S) - 1)]
        jitter = base * JITTER * self._rng.uniform(-1.0, 1.0)
        await self.clock.sleep(base + jitter)
