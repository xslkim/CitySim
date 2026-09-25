"""T-LLM-FIX-01 回归：InviteMachine.due_reminders/due_stood_ups 主循环接线（02 文档遗漏，偏差表 D34）。

- 功能回归：经主循环挂载入口 `worldsim.main.drive_invite_due` 驱动，到期约定落
  `social.appointment.remind`；过宽限期未执行落 `social.appointment.stood_up`（01 §5.3 时序）。
- 挂载回归：main.py 的 `after_tick` 收尾段必须含 `drive_invite_due` 调用（防再次遗漏）。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.adjudicator.state_events import StateAggregator
from worldsim.invite.state_machine import InviteMachine
from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.main import drive_invite_due
from worldsim.relations.cooldown import CooldownEngine
from worldsim.relations.relations import RelationEngine
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

SIM_NOW = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=LOCAL_TZ)  # 周一 12:00
CFG = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "relations.yaml").read_text(encoding="utf-8"))


class _StubGateway:
    def __init__(self) -> None:
        self._mock = MockProvider()

    async def chat(self, task_type, messages, gen_params=None, **kw) -> ChatResult:
        return ChatResult(text='{"ok": true}', prompt_tokens=1, completion_tokens=1,
                          latency_ms=1, request_id="stub", provider="stub", model="stub")

    async def embed(self, texts, *, seed=None, **kw):
        return await self._mock.embed(texts, seed=seed)


@pytest_asyncio.fixture()
async def machine(test_db_dsn: str):
    pool = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    persona = {"big_five": {"agreeableness": 50, "extraversion": 50, "conscientiousness": 50, "neuroticism": 40, "openness": 50}}
    for aid in ("A02", "A03"):
        await pool.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position)
            VALUES ($1, $2, 'F', 25, 'star', $3::jsonb, $4::jsonb, 100000, 'apt.lobby')
            ON CONFLICT (id) DO NOTHING
            """,
            aid, f"测试{aid}", json.dumps(persona),
            json.dumps({"hunger": 70, "energy": 70, "mood": 60, "social": 80, "wealth": 70, "achievement": 70}),
        )
    m = InviteMachine(
        pool, _StubGateway(), CFG,
        agg=StateAggregator(pool), cooldown=CooldownEngine(pool, CFG),
        relations=RelationEngine(pool, CFG), tick_of=lambda _s: 7,
    )
    try:
        yield m, pool
    finally:
        await pool.close()


async def _mk_appointment(pool, at_sim: dt.datetime) -> int:
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, rng_seed, visibility, payload)
        VALUES (1, $1, 'social.appointment.created', 'system', 'system', '{A02,A03}', 1, 'public',
                $2::jsonb)
        RETURNING seq
        """,
        SIM_NOW, json.dumps({"participants": ["A02", "A03"], "activity": "喝咖啡",
                             "at_sim": at_sim.isoformat()}),
    )


async def test_drive_invite_due_fires_remind_and_stood_up(machine) -> None:
    """经主循环挂载入口驱动：T-30min 提醒落 remind；T+15min 宽限后无人赴约落 stood_up。"""
    m, pool = machine
    at_sim = SIM_NOW + dt.timedelta(minutes=25)  # 25 分钟后：已进入 T-30min 提醒窗
    seq = await _mk_appointment(pool, at_sim)
    await drive_invite_due(m, tick=100, sim_now=SIM_NOW)
    row = await pool.fetchrow(
        "SELECT type, payload FROM events WHERE type='social.appointment.remind' ORDER BY seq DESC LIMIT 1"
    )
    assert row is not None, "drive_invite_due 未驱动 due_reminders（02 遗漏接线回归）"
    assert json.loads(row["payload"])["at_sim"] == at_sim.isoformat()

    # 推进到宽限期后（at_sim+15min 仍无执行事件）→ stood_up + 爽约结算
    later = at_sim + dt.timedelta(minutes=16)
    await drive_invite_due(m, tick=110, sim_now=later)
    su = await pool.fetchrow(
        "SELECT payload FROM events WHERE type='social.appointment.stood_up' ORDER BY seq DESC LIMIT 1"
    )
    assert su is not None, "drive_invite_due 未驱动 due_stood_ups"
    assert json.loads(su["payload"])["participants"] == ["A02", "A03"]
    rel = await pool.fetchrow(
        "SELECT affinity, tension FROM relations WHERE a_id='A03' AND b_id='A02'"
    )
    assert rel is not None  # 爽约关系结算已落地（放方缺省 = participants 序首 A02，受害方 A03）


async def test_after_tick_mounts_drive_invite_due() -> None:
    """挂载回归：main.py after_tick 收尾段必须调用 drive_invite_due（防再次遗漏，D34）。"""
    src = Path(__file__).resolve().parents[2] / "worldsim" / "main.py"
    text = src.read_text(encoding="utf-8")
    body = text.split("async def after_tick", 1)[1]
    assert "drive_invite_due(invite_machine" in body, "after_tick 缺 drive_invite_due 挂载行"
