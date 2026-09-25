"""T-LLM-08 成本熔断两档验收（03 验收 1~3；04 §8.4，06 §1.2；判级器唯一实现，评审 R1 §A.5）。

- 注入成本序列驱动 alarm → throttle → recover 全链路，逐迁移断言 ThrottleState 五开关
  （含反思阈值 20→35、对话上限 ×60%，口径 04 §8.4）；
- `system.llm.throttle` payload 键集精确 = {level, ratio}，level 序列前缀 = alarm/throttle/recover；
- 基线未配置（WSIM_COST_LIMIT_CNY_PER_SIMDAY 缺省）→ 只告警（WARN）不动作。
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import asyncpg
import pytest

from worldsim.llm_gateway.breaker import COST_LIMIT_ENV, CostBreaker

pytestmark = pytest.mark.asyncio

_THRESHOLDS = {"cost_breaker": {"alarm_ratio": 1.5, "throttle_ratio": 2.0, "recover_ratio": 1.3}}
_T0 = dt.datetime(2026, 9, 25, 8, 0, tzinfo=dt.timezone.utc)
_DAY = dt.timedelta(days=1)


async def test_alarm_then_throttle_then_recover(test_db_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """注入成本序列驱动 alarm → throttle → recover 全链路（03 验收 1；SQL 验收 2 同用例）。"""
    monkeypatch.delenv(COST_LIMIT_ENV, raising=False)
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        cb = CostBreaker(pool, _THRESHOLDS, baseline_cny=10.0)  # 基线 ¥10/模拟日
        # 报警档：1.6× 持续 1 模拟日（04 §8.4 触发列"持续 1 模拟日"）
        assert await cb.evaluate(16.0, sim_now=_T0) == "normal"          # 刚进 band，未持续
        assert await cb.evaluate(16.0, sim_now=_T0 + _DAY) == "alarm"    # 持续满 → alarm
        s = cb.state
        assert s.level == "alarm" and not s.secondary_force_lowest       # 报警档内核不变
        assert s.dialogue_daily_cap_factor == 1.0 and s.reflection_threshold == 20
        # 降速档：2.5× 持续 1 模拟日
        assert await cb.evaluate(25.0, sim_now=_T0 + _DAY) == "alarm"    # throttle band 刚进
        assert await cb.evaluate(25.0, sim_now=_T0 + 2 * _DAY) == "throttle"
        s = cb.state
        assert s.secondary_force_lowest is True                          # 动作①（开发期 = GLM 最低档别名，03 §6 D1）
        assert s.director_call_factor == 0.5                             # 动作② 编剧调用减半
        assert s.dialogue_daily_cap_factor == 0.6                        # 动作③ 对话场次上限 ×60%
        assert s.reflection_threshold == 35                              # 动作④ 反思阈值 20→35
        assert s.alarm_escalated is True                                 # 动作⑤ 告警升级
        assert s.needs_clock_pause is False
        # 回落至恢复线下（<1.3×）→ 逐级撤销
        assert await cb.evaluate(12.0, sim_now=_T0 + 3 * _DAY) == "alarm"     # throttle → alarm（撤销五动作）
        s = cb.state
        assert not s.secondary_force_lowest and s.director_call_factor == 1.0
        assert s.dialogue_daily_cap_factor == 1.0 and s.reflection_threshold == 20 and not s.alarm_escalated
        assert await cb.evaluate(12.0, sim_now=_T0 + 4 * _DAY) == "normal"    # alarm → normal
        # SQL：payload 键集精确 = {level, ratio}；level 序列前缀 = alarm/throttle/recover（06 §1.2）
        rows = await pool.fetch(
            "SELECT payload FROM events WHERE type='system.llm.throttle' ORDER BY seq"
        )
        levels = []
        for r in rows:
            payload = json.loads(r["payload"])
            assert set(payload) == {"level", "ratio"}
            levels.append(payload["level"])
        assert levels[:3] == ["alarm", "throttle", "recover"]
        assert "recover" in levels[3:]
        # source/trigger/visibility 契约
        row = await pool.fetchrow(
            "SELECT source, trigger, visibility FROM events WHERE type='system.llm.throttle' ORDER BY seq DESC LIMIT 1"
        )
        assert (row["source"], row["trigger"], row["visibility"]) == ("system", "system", "internal")
    finally:
        await pool.close()


async def test_no_baseline_warn_only(test_db_dsn: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """基线未回填期：只告警不动作（03 验收 3；与 08 文档口径一致）。"""
    monkeypatch.delenv(COST_LIMIT_ENV, raising=False)
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        before = await pool.fetchval("SELECT count(*) FROM events WHERE type='system.llm.throttle'")
        cb = CostBreaker(pool, _THRESHOLDS)  # 无 baseline_cny 且 env 缺省
        with caplog.at_level(logging.WARNING, logger="worldsim.llm_gateway.breaker"):
            level = await cb.evaluate(999.0, sim_now=_T0)
            await cb.evaluate(999.0, sim_now=_T0 + 2 * _DAY)
        assert level == "normal"
        assert cb.state.level == "normal" and cb.state.dialogue_daily_cap_factor == 1.0
        assert any(COST_LIMIT_ENV in r.message for r in caplog.records), "缺基线必须 WARN"
        after = await pool.fetchval("SELECT count(*) FROM events WHERE type='system.llm.throttle'")
        assert after == before, "基线未配置不得落 throttle 事件/不得动作"
    finally:
        await pool.close()


async def test_baseline_from_env(test_db_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """基线读 env WSIM_COST_LIMIT_CNY_PER_SIMDAY（04 §1.6）。"""
    monkeypatch.setenv(COST_LIMIT_ENV, "10")
    cb = CostBreaker(None, _THRESHOLDS)
    assert cb._baseline == 10.0


# ============================================================================
# T-OPS-02 降速动作消费接线（08 文档；判级器不重复实现，本段用例前缀 test_throttle_wiring_）
# ============================================================================

from worldsim.llm_gateway.breaker import BreakerThrottleView  # noqa: E402


async def _drive_to_throttle(pool) -> CostBreaker:
    """驱动 normal → alarm → throttle（基线 ¥10；持续 1 模拟日口径）。"""
    cb = CostBreaker(pool, _THRESHOLDS, baseline_cny=10.0)
    await cb.evaluate(16.0, sim_now=_T0)
    await cb.evaluate(16.0, sim_now=_T0 + _DAY)          # alarm
    await cb.evaluate(25.0, sim_now=_T0 + _DAY)
    assert await cb.evaluate(25.0, sim_now=_T0 + 2 * _DAY) == "throttle"
    return cb


async def test_throttle_wiring_reflect_threshold(test_db_dsn: str) -> None:
    """读取点④：throttle 档反思阈值 20→35；normal 档回默认。"""
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        cb = await _drive_to_throttle(pool)
        view = BreakerThrottleView(cb.state)
        assert view.reflection_threshold(20) == 35
        cb.state.level = "normal"
        assert view.reflection_threshold(20) == 20
    finally:
        await pool.close()


async def test_throttle_wiring_dialogue_cap(test_db_dsn: str) -> None:
    """读取点③：throttle 档对话场次上限 ×60%（42→25）；normal 档回默认。"""
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        cb = await _drive_to_throttle(pool)
        view = BreakerThrottleView(cb.state)
        assert view.dialogue_daily_cap(42) == int(42 * 0.6)
        cb.state.level = "normal"
        assert view.dialogue_daily_cap(42) == 42
    finally:
        await pool.close()


async def test_throttle_wiring_director_halved(test_db_dsn: str) -> None:
    """读取点②：throttle 档 director call_factor 减半；normal 档 1.0。"""
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        cb = await _drive_to_throttle(pool)
        view = BreakerThrottleView(cb.state)
        assert view.director_call_factor() == 0.5
        cb.state.level = "normal"
        assert view.director_call_factor() == 1.0
    finally:
        await pool.close()


async def test_star_floor_untouched(test_db_dsn: str) -> None:
    """04 §8.4 但书：明星层兜底频率不受 ThrottleState 影响（throttle 档每日兜底反思照常）。"""
    from worldsim.llm_gateway.providers.base import ChatResult
    from worldsim.llm_gateway.providers.mock import MockProvider
    from worldsim.memory.reflect import Reflector

    class _GW:
        def __init__(self) -> None:
            self._mock = MockProvider()
            self.calls: list[str] = []

        async def chat(self, task_type, messages, gen_params=None, *, seed=None,
                       agent_id=None, sim_time=None):
            self.calls.append(task_type)
            return ChatResult(text=json.dumps({"diary": "兜底反思", "needs_delta": {},
                                               "mood": "平稳", "tomorrow_plan": "明天"},
                                              ensure_ascii=False),
                              prompt_tokens=1, completion_tokens=1, latency_ms=1,
                              request_id="stub", provider="stub", model="stub")

        async def embed(self, texts, *, seed=None, agent_id=None, sim_time=None):
            return await self._mock.embed(texts, seed=seed)

    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        await pool.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs,
                                balance_cents, position)
            VALUES ('A40', '兜底明星', 'F', 25, 'star', '{"name":"兜底明星"}'::jsonb,
                    '{}'::jsonb, 0, 'apt.lobby')
            ON CONFLICT (id) DO NOTHING
            """)
        cb = await _drive_to_throttle(pool)
        refl = Reflector(pool, _GW(), threshold=20, tick_of=lambda _s: 1,
                         throttle=BreakerThrottleView(cb.state))  # throttle 档注入
        done = await refl.run_due_daily_fallbacks(
            ["A40"], dt.datetime(2026, 9, 25, 23, 30, tzinfo=dt.timezone.utc), rng_seed=1)
        assert "A40" in done, "明星层每日兜底不受降速影响（04 §8.4 但书）"
        assert await pool.fetchval(
            "SELECT count(*) FROM memories WHERE agent_id='A40' AND kind='reflection'") == 1
        await pool.execute("DELETE FROM memories WHERE agent_id='A40'")
        await pool.execute("DELETE FROM agents WHERE id='A40'")
    finally:
        await pool.close()


async def test_throttle_wiring_alerts(test_db_dsn: str, tmp_path) -> None:
    """迁移序列（含恢复）经 T-OPS-01 落行；throttle=ERROR 立即，alarm/recover=WARN 聚合。"""
    from worldsim.audit import alerts

    alerts.set_path(tmp_path / "alerts.log")
    try:
        pool = await asyncpg.create_pool(test_db_dsn)
        try:
            cb = await _drive_to_throttle(pool)  # alarm(WARN) → throttle(ERROR 立即)
            lines = (tmp_path / "alerts.log").read_text(encoding="utf-8").splitlines()
            assert any(json.loads(l)["key"] == "cost.throttle"
                       and json.loads(l)["level"] == "ERROR" for l in lines)
            await cb.evaluate(12.0, sim_now=_T0 + 3 * _DAY)   # throttle → alarm
            await cb.evaluate(12.0, sim_now=_T0 + 4 * _DAY)   # alarm → normal（recover）
            alerts.flush_warnings()
            keys = [json.loads(l)["key"] for l in
                    (tmp_path / "alerts.log").read_text(encoding="utf-8").splitlines()]
            assert keys == ["cost.alarm", "cost.throttle", "cost.recover"], keys
            # 事件序列复核（03 侧产出）：payload 键 = {level, ratio}
            rows = await pool.fetch(
                "SELECT payload FROM events WHERE type='system.llm.throttle'"
                " AND sim_time >= $1 ORDER BY seq", _T0)
            levels = [json.loads(r["payload"])["level"] if isinstance(r["payload"], str)
                      else r["payload"]["level"] for r in rows]
            assert levels[:3] == ["alarm", "throttle", "recover"]
            for r in rows:
                p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
                assert set(p) == {"level", "ratio"}
        finally:
            await pool.close()
    finally:
        alerts.set_path(None)


async def test_throttle_24h_escalates_to_pause(test_db_dsn: str) -> None:
    """验收 3：fake clock 下 throttle 持续 24 真实小时未恢复 → needs_clock_pause → clock.pause 被调用。"""
    from worldsim.audit.probe import consume_throttle_escalation

    class FakeClock:
        def __init__(self) -> None:
            self.t = 1_000_000.0

        def now(self) -> float:
            return self.t

    clock = FakeClock()
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        cb = CostBreaker(pool, _THRESHOLDS, baseline_cny=10.0, clock=clock)
        await cb.evaluate(16.0, sim_now=_T0)
        await cb.evaluate(16.0, sim_now=_T0 + _DAY)
        await cb.evaluate(25.0, sim_now=_T0 + _DAY)
        assert await cb.evaluate(25.0, sim_now=_T0 + 2 * _DAY) == "throttle"
        assert cb.state.needs_clock_pause is False
        clock.t += 24 * 3600 + 1  # 24 真实小时仍超线
        await cb.evaluate(25.0, sim_now=_T0 + 3 * _DAY)
        assert cb.state.needs_clock_pause is True

        paused: list[str] = []

        async def _pause(reason: str) -> None:
            paused.append(reason)

        assert await consume_throttle_escalation(cb.state, _pause) is True
        assert paused and "24" in paused[0]
    finally:
        await pool.close()
