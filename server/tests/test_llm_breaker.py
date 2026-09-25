"""T-LLM-06 降级链/撞墙判定/`system.llm.failover` 验收（03 验收 1~3；04 §8.2，06 §1.2）。

- 撞墙三条件分别构造命中 + 冷却 30 分钟（fake clock）后探测成功切回；
- failover 事件 payload 键集精确 = {task_type, from_provider, to_provider, reason} 且不含 text_display；
- 降级期调用行 `llm_calls.fallback_from` 非空，恢复后新行为 NULL。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import asyncpg
import pytest

from worldsim.llm_gateway import ChainExhausted, LLMGateway
from worldsim.llm_gateway.breaker import COOLDOWN_S, FailoverBreaker
from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.llm_gateway.providers.zhipu import RateLimited, ServerError
from worldsim.llm_gateway.router import ModelRouter

pytestmark = pytest.mark.asyncio


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Scripted:
    def __init__(self, name: str, script: list[Exception | str]) -> None:
        self.name = name
        self._script = list(script)
        self.calls = 0

    async def chat(self, task_type, messages, gen_params=None, *, seed=None) -> ChatResult:
        self.calls += 1
        item = self._script.pop(0) if self._script else "ok"
        if isinstance(item, Exception):
            raise item
        return ChatResult(text=item, prompt_tokens=1, completion_tokens=1,
                          latency_ms=1, request_id="r", provider=self.name, model="m1")


async def test_trip_on_each_condition_and_recover() -> None:
    """三条件分别命中（04 §8.2 逐字）；冷却 30 分钟后探测成功切回（03 验收 1）。"""
    # 条件①：连续 3 次 429/5xx（重试耗尽后计）
    clock = _Clock()
    b = FailoverBreaker(clock)
    for _ in range(2):
        assert b.record_attempt("zhipu", kind="429", latency_ms=10, rpm_limit=100) is None
    assert b.record_attempt("zhipu", kind="5xx", latency_ms=10, rpm_limit=100) == "consecutive_429_5xx"
    assert b.is_tripped("zhipu")
    assert not b.due_for_probe("zhipu")
    clock.advance(COOLDOWN_S)
    assert b.due_for_probe("zhipu")
    assert b.probe_result("zhipu", ok=False) is False  # 探测失败 → 冷却重计
    assert not b.due_for_probe("zhipu")
    clock.advance(COOLDOWN_S)
    assert b.probe_result("zhipu", ok=True) is True
    assert not b.is_tripped("zhipu")

    # 条件②：滑动 5 分钟窗口 RPM 使用率 >90%（rpm_limit=10 → 窗口容量 50，>45 触发）
    clock = _Clock()
    b = FailoverBreaker(clock)
    reason = None
    for _ in range(50):
        reason = b.record_attempt("zhipu", kind="ok", latency_ms=10, rpm_limit=10) or reason
    assert reason == "rpm_usage_over_90pct"

    # 条件③：滑动 5 分钟窗口 p95 延迟 >8s
    clock = _Clock()
    b = FailoverBreaker(clock)
    assert b.record_attempt("zhipu", kind="ok", latency_ms=9000, rpm_limit=10**6) is None  # 单样本不触发
    assert b.record_attempt("zhipu", kind="ok", latency_ms=9000, rpm_limit=10**6) == "p95_latency_over_8s"

    # 成功调用重置条件①连续计数
    clock = _Clock()
    b = FailoverBreaker(clock)
    b.record_attempt("zhipu", kind="429", latency_ms=10, rpm_limit=100)
    b.record_attempt("zhipu", kind="429", latency_ms=10, rpm_limit=100)
    b.record_attempt("zhipu", kind="ok", latency_ms=10, rpm_limit=100)
    b.record_attempt("zhipu", kind="429", latency_ms=10, rpm_limit=100)
    b.record_attempt("zhipu", kind="5xx", latency_ms=10, rpm_limit=100)
    assert not b.is_tripped("zhipu")


async def test_entry_level_trip_disable() -> None:
    """条目级停用（03 §6 D42）：`rpm_usage_trip_pct=0` 停用条件②、`p95_trip_ms=0` 停用条件③；
    缺省（None）回落全局口径（RPM >90% / p95 >8s，04 §8.2），条件①不受条目级影响。"""
    # 条件②条目级停用：窗口打满桶容量 100% 也不 trip（缺省口径下 >90% 即 trip，见上）
    clock = _Clock()
    b = FailoverBreaker(clock)
    for _ in range(50):
        assert b.record_attempt("zhipu/glm-4.5-flash", kind="ok", latency_ms=10, rpm_limit=10,
                                rpm_usage_trip_pct=0) is None
    assert not b.is_tripped("zhipu/glm-4.5-flash")

    # 条件③条目级停用：p95 远超 8s 默认阈也不 trip
    clock = _Clock()
    b = FailoverBreaker(clock)
    assert b.record_attempt("zhipu/glm-4-flash", kind="ok", latency_ms=60_000, rpm_limit=10**6,
                            p95_trip_ms=0) is None
    assert b.record_attempt("zhipu/glm-4-flash", kind="ok", latency_ms=60_000, rpm_limit=10**6,
                            p95_trip_ms=0) is None
    assert not b.is_tripped("zhipu/glm-4-flash")

    # 条目级停用不影响条件①：连续 3 次 429 仍 trip
    for _ in range(2):
        assert b.record_attempt("zhipu/glm-4-flash", kind="429", latency_ms=10, rpm_limit=10**6,
                                p95_trip_ms=0) is None
    assert b.record_attempt("zhipu/glm-4-flash", kind="429", latency_ms=10, rpm_limit=10**6,
                            p95_trip_ms=0) == "consecutive_429_5xx"


_ROUTED_CFG = {
    "providers": {
        "p1": {"type": "openai_compatible", "rpm_limit": 1000, "chat": [{"id": "m1", "input_price": 1.0, "output_price": 2.0}]},
        "p2": {"type": "openai_compatible", "rpm_limit": 1000, "chat": [{"id": "m2", "input_price": 3.0, "output_price": 4.0}]},
    },
    "task_routes": {
        "secondary": {
            "primary": {"provider": "p1", "model": "m1"},
            "fallback": [{"provider": "p2", "model": "m2"}, "pause_clock"],
        },
        **{t: {"primary": {"provider": "p1", "model": "m1"}, "fallback": ["pause_clock"]}
           for t in ("star_decision", "dialogue", "bgsummary", "reflection", "director", "world_copy", "safety", "embed")},
    },
    "generation_params": {t: {"temperature": 0.7} for t in
                          ("star_decision", "dialogue", "secondary", "bgsummary", "reflection", "director", "safety")},
    "thresholds": {},
}


def _routed_gateway(pool, clock: _Clock, p1: _Scripted, p2: _Scripted) -> tuple[LLMGateway, FailoverBreaker]:
    router = ModelRouter(_ROUTED_CFG)
    breaker = FailoverBreaker(clock)
    gw = LLMGateway(
        pool, _ROUTED_CFG,
        providers={"p1": p1, "p2": p2},
        router=router, breaker=breaker, clock=clock,
    )
    return gw, breaker


async def test_failover_event_payload_contract(test_db_dsn: str) -> None:
    """撞墙 → 降级切换落 `system.llm.failover`，payload 键集精确四键且无 text_display（03 验收 2）。"""
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        clock = _Clock()
        p1 = _Scripted("p1", [ServerError("500", status=500)] * 20)
        p2 = _Scripted("p2", ["ok"])
        gw, _ = _routed_gateway(pool, clock, p1, p2)
        sim_now = dt.datetime.now(dt.timezone.utc)
        r = await gw.chat("secondary", [{"role": "user", "content": "x"}], seed=1, agent_id="A01", sim_time=sim_now, tick=1)
        assert r.provider == "p2"
        row = await pool.fetchrow(
            "SELECT type, source, trigger, visibility, payload FROM events WHERE type='system.llm.failover' ORDER BY seq DESC LIMIT 1"
        )
        assert row is not None
        assert (row["source"], row["trigger"], row["visibility"]) == ("system", "system", "internal")
        payload = json.loads(row["payload"])
        assert set(payload) == {"task_type", "from_provider", "to_provider", "reason"}
        assert "text_display" not in payload and "text_raw" not in payload
        assert payload["task_type"] == "secondary" and payload["from_provider"] == "p1" and payload["to_provider"] == "p2"
    finally:
        await pool.close()


async def test_fallback_from_recorded(test_db_dsn: str) -> None:
    """降级期调用行 `fallback_from` 非空；冷却探测恢复后新行为 NULL（03 验收 3，04 §8.2）。"""
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        clock = _Clock()
        p1 = _Scripted("p1", [ServerError("500", status=500)] * 20)
        p2 = _Scripted("p2", ["ok"] * 20)
        gw, breaker = _routed_gateway(pool, clock, p1, p2)
        sim_now = dt.datetime.now(dt.timezone.utc)
        msgs = [{"role": "user", "content": "x"}]

        await gw.chat("secondary", msgs, seed=1, agent_id="A01", sim_time=sim_now, tick=1)
        row = await pool.fetchrow(
            "SELECT provider, status, fallback_from FROM llm_calls ORDER BY id DESC LIMIT 1"
        )
        assert row["provider"] == "p2" and row["status"] == "fallback" and row["fallback_from"] == "p1"
        # 降级期后续调用（breaker 仍 tripped）同样带 fallback_from
        await gw.chat("secondary", msgs, seed=2, agent_id="A01", sim_time=sim_now, tick=2)
        row = await pool.fetchrow(
            "SELECT provider, status, fallback_from FROM llm_calls ORDER BY id DESC LIMIT 1"
        )
        assert row["provider"] == "p2" and row["fallback_from"] == "p1"

        # 冷却 30 分钟后探测恢复：p1 恢复可用 → failover 恢复事件 + 新行 fallback_from 为 NULL
        p1._script = ["ok"] * 5
        clock.advance(COOLDOWN_S)
        r = await gw.chat("secondary", msgs, seed=3, agent_id="A01", sim_time=sim_now, tick=3)
        assert r.provider == "p1" and not breaker.is_tripped("p1")
        row = await pool.fetchrow(
            "SELECT provider, status, fallback_from FROM llm_calls ORDER BY id DESC LIMIT 1"
        )
        assert row["provider"] == "p1" and row["status"] == "ok" and row["fallback_from"] is None
        ev = await pool.fetchval(
            "SELECT payload->>'reason' FROM events WHERE type='system.llm.failover' ORDER BY seq DESC LIMIT 1"
        )
        assert ev == "recovered"
    finally:
        await pool.close()


async def test_success_path_trip_emits_failover(test_db_dsn: str) -> None:
    """回归：成功路径上窗口条件③撞墙（p95 延迟超阈）也必须落 `system.llm.failover`（04 §8.2 切换留痕）。

    2026-09-25 真跑实发缺陷：ok 调用喂入的延迟触发 trip 但事件未落库，后续调用静默走尽降级链。
    """
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        clock = _Clock()

        class _Slow(_Scripted):
            async def chat(self, task_type, messages, gen_params=None, *, seed=None):
                clock.advance(20.0)  # 模拟 20s 慢调用（默认 p95 阈 8s，04 §8.2）
                return await super().chat(task_type, messages, gen_params, seed=seed)

        p1 = _Slow("p1", ["ok"] * 10)
        p2 = _Scripted("p2", ["ok"] * 10)
        gw, breaker = _routed_gateway(pool, clock, p1, p2)
        sim_now = dt.datetime.now(dt.timezone.utc)
        msgs = [{"role": "user", "content": "x"}]
        await gw.chat("secondary", msgs, seed=1, agent_id="A01", sim_time=sim_now, tick=1)  # 单样本不触发
        r2 = await gw.chat("secondary", msgs, seed=2, agent_id="A01", sim_time=sim_now, tick=2)  # p95 超阈 → trip
        assert r2.provider == "p1"  # 本次仍由 p1 服务（调用已成功）
        assert breaker.is_tripped("p1/m1")
        row = await pool.fetchrow(
            "SELECT payload FROM events WHERE type='system.llm.failover' ORDER BY seq DESC LIMIT 1"
        )
        payload = json.loads(row["payload"])
        assert payload["reason"] == "p95_latency_over_8s"
        assert payload["from_provider"] == "p1" and payload["to_provider"] == "p2"
        r3 = await gw.chat("secondary", msgs, seed=3, agent_id="A01", sim_time=sim_now, tick=3)
        assert r3.provider == "p2"  # 后续调用走降级跳
    finally:
        await pool.close()


async def test_p95_trip_ms_entry_override(test_db_dsn: str) -> None:
    """回归：模型条目 `p95_trip_ms` 覆盖必须对 ok 路径同样生效（2026-09-25 真跑实发：
    _breaker_feed_ok 漏传覆盖值回落默认 8s 阈，免费档正常延迟 8~15s 被误 trip）。"""
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        import copy

        cfg = copy.deepcopy(_ROUTED_CFG)
        cfg["providers"]["p1"]["chat"][0]["p95_trip_ms"] = 60000
        clock = _Clock()

        class _Slow(_Scripted):
            async def chat(self, task_type, messages, gen_params=None, *, seed=None):
                clock.advance(20.0)  # 20s 慢调用：低于条目阈 60s、高于默认阈 8s
                return await super().chat(task_type, messages, gen_params, seed=seed)

        router = ModelRouter(cfg)
        breaker = FailoverBreaker(clock)
        gw = LLMGateway(pool, cfg, providers={"p1": _Slow("p1", ["ok"] * 5), "p2": _Scripted("p2", ["ok"])},
                        router=router, breaker=breaker, clock=clock)
        sim_now = dt.datetime.now(dt.timezone.utc)
        before = await pool.fetchval("SELECT count(*) FROM events WHERE type='system.llm.failover'")
        for i in range(3):
            r = await gw.chat("secondary", [{"role": "user", "content": "x"}], seed=i, agent_id="A01", sim_time=sim_now, tick=i)
            assert r.provider == "p1"
        assert not breaker.is_tripped("p1/m1")
        n = await pool.fetchval("SELECT count(*) FROM events WHERE type='system.llm.failover'")
        assert n == before, "条目级 p95 阈内不得误 trip"
    finally:
        await pool.close()


async def test_chain_exhausted_raises_special() -> None:
    """链尽行为上抛 ChainExhausted（04 §8.1 末端动作；消费接线归内核/08）。"""
    clock = _Clock()
    p1 = _Scripted("p1", [ServerError("500", status=500)] * 20)
    router = ModelRouter(_ROUTED_CFG)
    gw = LLMGateway(
        None, _ROUTED_CFG, providers={"p1": p1},  # p2 未注册 → 链尽
        router=router, breaker=FailoverBreaker(clock), clock=clock,
    )
    with pytest.raises(ChainExhausted) as ei:
        await gw.chat("secondary", [{"role": "user", "content": "x"}], seed=1)
    assert ei.value.special == "pause_clock"
