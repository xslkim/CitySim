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
