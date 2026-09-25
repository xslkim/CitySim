"""T-LLM-05 RPM 桶/优先级抢占/退避验收（03 验收 1~4；04 §8.5/§8.2）。

全部用例走注入 fake clock（确定性、零真实等待、零网络）：
- `AutoClock`：单任务场景，sleep 立即推进虚拟时间并让出事件循环；
- `ManualClock`：并发抢占场景，sleep 真实阻塞到测试侧 `advance()` 推进（保 waiter 入队顺序可控）。
"""

from __future__ import annotations

import asyncio
import random

import pytest

from worldsim.llm_gateway.clients import RateLimitedClient, WallSignal
from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.llm_gateway.providers.zhipu import RateLimited, ServerError

pytestmark = pytest.mark.asyncio


class AutoClock:
    """注入时钟：sleep 立即推进虚拟时间并让出事件循环（03 T-LLM-05 实现要点）。"""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


class ManualClock:
    """注入时钟：sleep 阻塞至 `advance()` 推进到唤醒点（并发用例的确定性调度）。"""

    def __init__(self) -> None:
        self.t = 0.0
        self._waiters: list[tuple[float, asyncio.Event]] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        ev = asyncio.Event()
        self._waiters.append((self.t + seconds, ev))
        await ev.wait()

    def advance(self, seconds: float) -> None:
        self.t += seconds
        for wake_at, ev in self._waiters:
            if wake_at <= self.t:
                ev.set()


class ScriptedProvider:
    """脚本化 provider：按队列抛出异常或返回结果，记录调用序号时刻。"""

    name = "scripted"

    def __init__(self, script: list[Exception | str], clock) -> None:
        self._script = list(script)
        self._clock = clock
        self.call_times: list[float] = []

    async def chat(self, task_type, messages, gen_params=None, *, seed=None) -> ChatResult:
        self.call_times.append(self._clock.now())
        item = self._script.pop(0) if self._script else "ok"
        if isinstance(item, Exception):
            raise item
        return ChatResult(text=item, prompt_tokens=1, completion_tokens=1,
                          latency_ms=1, request_id="r", provider="scripted", model="m")


def _client(script: list, clock, *, rpm_limit: int = 10**6, rng_seed: int = 7) -> tuple[RateLimitedClient, ScriptedProvider]:
    p = ScriptedProvider(script, clock)
    c = RateLimitedClient(p, rpm_limit=rpm_limit, clock=clock, rng=random.Random(rng_seed))
    return c, p


async def test_bucket_rate_limit() -> None:
    """fake clock 下 N 次调用耗时 ≥ 桶容量推算下界（03 验收 1）。

    rpm_limit=2 → 容量 2、补充速率 1 个/30s；5 次顺序调用：前 2 次取初始令牌，
    后 3 次各等 ≥30s → 虚拟总耗时 ≥90s。
    """
    clock = AutoClock()
    c, p = _client(["ok"] * 5, clock, rpm_limit=2)
    for _ in range(5):
        r = await c.call("secondary", [{"role": "user", "content": "x"}])
        assert not isinstance(r, WallSignal)
    assert clock.t >= 90.0, f"5 次调用虚拟耗时 {clock.t}s 低于桶容量下界 90s"
    assert len(p.call_times) == 5


async def test_priority_preemption() -> None:
    """桶空时 safety 请求先于排队中的 bgsummary 发出（03 验收 2；优先级序 04 §8.5）。"""
    clock = ManualClock()
    c, p = _client(["ok"] * 4, clock, rpm_limit=1)  # 容量 1，1 个令牌/60s
    first = await c.call("star_decision", [{"role": "user", "content": "warm"}])  # 取走唯一令牌
    assert not isinstance(first, WallSignal)

    order: list[str] = []
    orig_chat = p.chat

    async def tag_chat(task_type, messages, gen_params=None, *, seed=None):
        order.append(task_type)
        return await orig_chat(task_type, messages, gen_params, seed=seed)

    p.chat = tag_chat  # type: ignore[method-assign]

    low = asyncio.create_task(c.call("bgsummary", [{"role": "user", "content": "low"}]))
    await asyncio.sleep(0)  # 低优先级请求先入队并阻塞待发
    high = asyncio.create_task(c.call("safety", [{"role": "user", "content": "high"}]))
    await asyncio.sleep(0)  # 高优先级请求随后入队（抢占点：令牌补给时应先发给 safety）
    for _ in range(4):      # 逐段推进虚拟时间：令牌补给 → safety 抢占 → bgsummary 补到下一令牌
        clock.advance(60.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    await asyncio.gather(low, high)
    assert order == ["safety", "bgsummary"], f"高优先级 safety 未抢占排队中的 bgsummary（实测序 {order}）"


async def test_backoff_sequence_and_retry_after() -> None:
    """429 带 Retry-After: 7 → 等 7s；500 → 1s/2s/4s ±20% 抖动；第 4 次不再重试返回撞墙（03 验收 3）。"""
    # 429 Retry-After 优先于退避序列
    clock = AutoClock()
    c, p = _client([RateLimited("429", retry_after_s=7.0), RateLimited("429", retry_after_s=7.0), "ok"], clock)
    r = await c.call("secondary", [{"role": "user", "content": "x"}])
    assert not isinstance(r, WallSignal)
    gaps = [b - a for a, b in zip(p.call_times, p.call_times[1:])]
    assert gaps == [7.0, 7.0], f"429 必须按 Retry-After 等待（实测 {gaps}）"

    # 500：1/2/4 ±20% 抖动区间
    clock = AutoClock()
    c, p = _client([ServerError("500", status=500)] * 3 + ["ok"], clock, rng_seed=7)
    r = await c.call("secondary", [{"role": "user", "content": "x"}])
    assert not isinstance(r, WallSignal)
    gaps = [b - a for a, b in zip(p.call_times, p.call_times[1:])]
    for gap, base in zip(gaps, (1.0, 2.0, 4.0)):
        assert base * 0.8 <= gap <= base * 1.2, f"退避 {gap}s 不在 {base}s±20% 区间"

    # 第 4 次失败不再重试 → WallSignal
    clock = AutoClock()
    c, p = _client([ServerError("500", status=500)] * 10, clock)
    r = await c.call("secondary", [{"role": "user", "content": "x"}])
    assert isinstance(r, WallSignal)
    assert len(p.call_times) == 4, f"最多 3 次重试（总尝试 4 次），实测 {len(p.call_times)}"


async def test_concurrent_tracking_no_cross_contamination() -> None:
    """回归：并发调用共享客户端时，逐调用 attempts 归属不得串档（2026-09-25 真跑实发：
    从共享 records 切片导致重复落库/重复计量；call_tracked 按调用返回私有清单）。"""
    clock = AutoClock()

    class _Slow:
        name = "slow"

        async def chat(self, task_type, messages, gen_params=None, *, seed=None) -> ChatResult:
            await asyncio.sleep(0)  # 让出制造交错
            return ChatResult(text="ok", prompt_tokens=1, completion_tokens=1,
                              latency_ms=1, request_id="r", provider="slow", model="m")

    c = RateLimitedClient(_Slow(), rpm_limit=10**6, clock=clock)
    results = await asyncio.gather(*[c.call_tracked("secondary", [{"role": "user", "content": "x"}]) for _ in range(20)])
    for outcome, attempts in results:
        assert not isinstance(outcome, WallSignal)
        assert len(attempts) == 1 and attempts[0].status == "ok", "并发下 attempts 串档"
    assert len(c.records) == 20


async def test_every_attempt_recorded() -> None:
    """3 次重试产生 3 条 status=retry 记录 + 终态 1 条（03 验收 4；04 §5.2 status 枚举）。"""
    clock = AutoClock()
    c, _ = _client([ServerError("500", status=500)] * 3 + ["ok"], clock)
    r = await c.call("dialogue", [{"role": "user", "content": "x"}])
    assert not isinstance(r, WallSignal)
    assert [a.status for a in c.records] == ["retry", "retry", "retry", "ok"]

    clock = AutoClock()
    c, _ = _client([ServerError("500", status=500)] * 10, clock)
    r = await c.call("dialogue", [{"role": "user", "content": "x"}])
    assert isinstance(r, WallSignal)
    assert [a.status for a in c.records] == ["retry", "retry", "retry", "failed"]
    assert len(r.attempts) == 4
