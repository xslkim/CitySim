"""T-LLM-11 安全三层过滤管线验收（03 验收 1~4；04 §11.1/§11.2，06 §1.2/§2）。

- 层 1 词表命中短路，不触发层 2 safety LLM 调用；
- 连续 block → 恰好 1 次重生成 → 事件 internal 且 payload 无 text_display（SQL 验证）；
- 哨兵原文仅出现在 text_raw/内部通道，text_display 与管线返回值中均无；
- 层 3 落库前复查：层 2 pass 但终稿被加工出词表命中词时拦截。
"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg
import pytest

from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.safety import LocalRules, SafetyPipeline

pytestmark = pytest.mark.asyncio

BANNED = "安全哨兵违禁词"  # config/safety/wordlist.txt 测试哨兵行
SENTINEL_RAW = "原文哨兵-只能在-text_raw"


class _StubGateway:
    """safety task 桩：按脚本返回标签 JSON，计数调用。"""

    def __init__(self, labels: list[str]) -> None:
        self._labels = list(labels) or ["pass"]
        self.calls = 0

    async def chat(self, task_type, messages, gen_params=None, **kw) -> ChatResult:
        assert task_type == "safety"
        self.calls += 1
        label = self._labels.pop(0) if self._labels else self._labels or "pass"
        return ChatResult(text=json.dumps({"label": label}), prompt_tokens=1, completion_tokens=1,
                          latency_ms=1, request_id="r", provider="stub", model="m")


def _pipeline(labels: list[str]) -> tuple[SafetyPipeline, _StubGateway]:
    gw = _StubGateway(labels)
    return SafetyPipeline(gw, rules=LocalRules.load()), gw


async def test_l1_short_circuits_l2() -> None:
    """词表命中样本不触发任何 safety LLM 调用（03 验收 1）。"""
    pipe, gw = _pipeline(["pass"])
    r = await pipe.check(f"这句话包含{BANNED}，必须被层1拦下")
    assert r.label == "block" and r.layer == "L1_rules" and r.filtered_text is None
    assert gw.calls == 0 and pipe.l2_calls == 0, "层 1 短路不得触发层 2 LLM 调用"
    # 干净文本走层 2
    r2 = await pipe.check("今天天气不错，想出去走走")
    assert r2.label == "pass" and r2.layer == "L2_glm" and gw.calls == 1


async def test_block_regenerate_once_then_internal(test_db_dsn: str) -> None:
    """连续 block → 恰好 1 次重生成 → 事件 internal、payload 无 text_display（03 验收 2，SQL 验证）。"""
    pipe, gw = _pipeline(["block", "block"])
    regen_calls = 0

    async def regenerate() -> str:
        nonlocal regen_calls
        regen_calls += 1
        return f"重生成后仍含{BANNED}"

    decision = await pipe.guard_publication(f"初稿含{BANNED}", regenerate)
    assert regen_calls == 1, f"恰好 1 次重生成（实测 {regen_calls}）"
    assert decision.visibility == "internal" and decision.text_display is None
    assert decision.text_raw and BANNED in decision.text_raw

    # 按决策落库（消费侧口径演示）+ SQL 契约断言
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        seq = await pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, rng_seed, visibility, payload)
            VALUES (1, $1, 'dialogue.chat', 'agent:A01', 'autonomous', '{A01,A02}', 1, $2, $3::jsonb)
            RETURNING seq
            """,
            dt.datetime.now(dt.timezone.utc), decision.visibility,
            json.dumps({} if decision.text_display is None else {"text_display": decision.text_display}),
        )
        row = await pool.fetchrow(
            "SELECT visibility, payload ? 'text_display' AS has_td FROM events WHERE seq=$1", seq,
        )
        assert (row["visibility"], row["has_td"]) == ("internal", False)
    finally:
        await pool.close()


async def test_raw_never_in_display_path() -> None:
    """哨兵原文仅在 text_raw/内部通道；text_display 与管线返回值中均无（03 验收 3，00 §4 红线 7）。"""
    pipe, _ = _pipeline(["block"])
    raw = f"{SENTINEL_RAW}（且命中{BANNED}）"
    decision = await pipe.guard_publication(raw, None)
    assert decision.visibility == "internal"
    assert decision.text_display is None
    assert decision.text_raw == raw  # 原文只留 text_raw（本机-only，04 §11.2）
    # soft/pass 路径：filtered_text 即原文（规则不改写），标签随行
    pipe2, _ = _pipeline(["soft"])
    r = await pipe2.check("一句普通但有争议的话")
    assert r.label == "soft" and r.filtered_text == "一句普通但有争议的话"


async def test_l3_recheck_on_final_text() -> None:
    """层 2 pass 但终稿被加工出词表命中词 → 层 3 拦截、不再重生成（03 验收 4）。"""
    pipe, gw = _pipeline(["pass"])
    regen_calls = 0

    async def regenerate() -> str:
        nonlocal regen_calls
        regen_calls += 1
        return "重生成的干净文本"

    decision = await pipe.guard_publication(
        "干净的初稿", regenerate, final_text=f"模板拼装后被引入{BANNED}的终稿",
    )
    assert decision.visibility == "internal" and decision.text_display is None
    assert gw.calls == 1 and regen_calls == 0, "层 3 拦截不触发重生成（初稿本身 pass）"
