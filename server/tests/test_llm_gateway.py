"""T-LLM-01 网关骨架验收：REPLAY 守卫 / task_type 封闭枚举 / 依赖方向 / llm_calls 留痕。

口径：03 文档 T-LLM-01 验收 1~3；04 §5.3（replay 拒绝一切真实调用）、04 §5.2（9 项 task_type 枚举）。
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from worldsim.llm_gateway import (
    TASK_TYPES,
    LLMGateway,
    ReplayViolation,
    UnknownTaskType,
)
from worldsim.llm_gateway.providers.base import ChatResult, EmbedResult

pytestmark = pytest.mark.asyncio

MODELS_CFG = {
    "task_routes": {t: {"primary": {"provider": "zhipu", "model": "glm-4.5-flash"}} for t in TASK_TYPES},
    "generation_params": {},
}


class _StubProvider:
    """HTTP 能力 provider 桩：若被调用会经注入的 httpx.MockTransport 出站（用于出站计数断言）。"""

    name = "stub"

    def __init__(self, transport: httpx.MockTransport) -> None:
        self.calls: list[tuple] = []
        self._transport = transport

    async def chat(self, task_type, messages, gen_params=None, *, seed=None) -> ChatResult:
        self.calls.append(("chat", task_type, seed))
        async with httpx.AsyncClient(transport=self._transport) as client:  # noqa: SIM115
            await client.post("https://example.invalid/v1/chat")
        return ChatResult(text='{"ok": true}', prompt_tokens=3, completion_tokens=2,
                          latency_ms=1, request_id="stub-req", provider="stub", model="stub-1")

    async def embed(self, texts, *, seed=None) -> EmbedResult:
        self.calls.append(("embed", len(texts), seed))
        async with httpx.AsyncClient(transport=self._transport) as client:  # noqa: SIM115
            await client.post("https://example.invalid/v1/embed")
        return EmbedResult(vectors=[[0.0] * 1024 for _ in texts], prompt_tokens=1,
                           latency_ms=1, request_id="stub-req", provider="stub", model="stub-emb")


def _gateway(transport: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch, *, replay: bool) -> tuple[LLMGateway, _StubProvider]:
    if replay:
        monkeypatch.setenv("WSIM_REPLAY_MODE", "replay")
    else:
        monkeypatch.delenv("WSIM_REPLAY_MODE", raising=False)
    stub = _StubProvider(transport)
    gw = LLMGateway(None, MODELS_CFG, providers={"stub": stub}, default_provider="stub")
    return gw, stub


async def test_replay_mode_refuses_all_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """replay 下 chat/embed 均抛 ReplayViolation；provider 未触达、httpx 出站计数 = 0（04 §5.3）。"""
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    gw, stub = _gateway(transport, monkeypatch, replay=True)
    with pytest.raises(ReplayViolation):
        await gw.chat("star_decision", [{"role": "user", "content": "hi"}], seed=1)
    with pytest.raises(ReplayViolation):
        await gw.embed(["hello"], seed=1)
    assert stub.calls == [], "守卫必须先于 provider 触发（provider 不可绕过）"
    assert hits == 0, "replay 下 httpx 不得有任何出站请求"


async def test_unknown_task_type_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    gw, _ = _gateway(transport, monkeypatch, replay=False)
    with pytest.raises(UnknownTaskType):
        await gw.chat("not_a_task", [{"role": "user", "content": "hi"}])
    with pytest.raises(UnknownTaskType):
        await gw.gen_params("not_a_task")
    # 9 项枚举（含 world_copy）全部放行
    assert set(TASK_TYPES) == {
        "star_decision", "dialogue", "secondary", "bgsummary", "reflection",
        "director", "world_copy", "safety", "embed",
    }
    for t in TASK_TYPES:
        if t == "embed":
            result = await gw.embed(["x"], seed=7)
            assert len(result.vectors[0]) == 1024
        else:
            result = await gw.chat(t, [{"role": "user", "content": "hi"}], seed=7)
            assert result.text


async def test_gateway_dependency_direction() -> None:
    """04 §2.1 单向依赖：llm_gateway 不得 import scheduler/adjudicator/world_agent（验收 3 的 grep 单测化）。"""
    pkg = Path(__file__).resolve().parents[1] / "worldsim" / "llm_gateway"
    offenders: list[str] = []
    for py in pkg.rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\s*(import|from)\s", line) and re.search(r"scheduler|adjudicator|world_agent", line):
                offenders.append(f"{py.name}: {line.strip()}")
    assert offenders == [], f"依赖方向违规: {offenders}"


async def test_llm_calls_recorded_for_mock(test_db_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """M1 期 mock 调用照常留痕 llm_calls（provider='mock'、cost 0、prompt 只留 hash，04 §8.3/§11.2）。"""
    import asyncpg

    from worldsim.llm_gateway.providers.mock import MockProvider

    monkeypatch.delenv("WSIM_REPLAY_MODE", raising=False)
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        gw = LLMGateway(pool, MODELS_CFG, providers={"mock": MockProvider()}, default_provider="mock")
        sentinel = "哨兵prompt原文-不得落库"
        await gw.chat("star_decision", [{"role": "user", "content": sentinel}], seed=42, agent_id="A01")
        row = await pool.fetchrow("SELECT * FROM llm_calls ORDER BY id DESC LIMIT 1")
        assert row["provider"] == "mock" and row["cost_micro_cny"] == 0
        assert row["task_type"] == "star_decision" and row["agent_id"] == "A01"
        assert row["status"] == "ok" and row["prompt_hash"]
        assert sentinel not in (row["prompt_hash"] or "")
        assert row["request_id"]
    finally:
        await pool.close()
