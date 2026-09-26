"""T-ITER2-01⑤ 裁决主循环协程隔离验收（round2 #1）：注入非 LLM 异常 → 协程存活、本拍跳过、后续拍继续。

事故背景：裁决循环只捕获 ChainExhausted/ProviderUnavailable，DB/授权/未知异常穿出
adjudication_loop → 世界空转/全核退出且无任何告警。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import pytest

from worldsim.adjudicator.pipeline import Pipeline, adjudication_loop
from worldsim.adjudicator.queue import AdjudicationQueue
from worldsim.llm_gateway import LLMGateway
from worldsim.llm_gateway.providers.mock import MockProvider
from worldsim.time_engine.clock import LOCAL_TZ


class _FakeClock:
    """裁决点逐读自增（内核真实形态：时钟协程独立推进，与裁决异常解耦）。"""

    def __init__(self) -> None:
        self._tick = 0
        self.batch_mode = False

    @property
    def current_tick(self) -> int:
        self._tick += 1
        return self._tick

    @current_tick.setter
    def current_tick(self, v: int) -> None:
        self._tick = v

    def sim_of_tick(self, tick: int) -> dt.datetime:
        return dt.datetime(2026, 10, 12, 12, 0, tzinfo=LOCAL_TZ)

    def now_sim(self) -> dt.datetime:
        return self.sim_of_tick(self._tick)


class _FlakyPipeline(Pipeline):
    """首拍抛非 LLM 异常（模拟 DB/授权故障），之后正常。"""

    def __init__(self) -> None:
        super().__init__(pool=None, gateway=LLMGateway(None, {}, providers={"mock": MockProvider()}))
        self.calls = 0

    async def run_tick(self, **kwargs):  # noqa: ANN003 - 测试桩
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("mock DB/授权异常（InsufficientPrivilege 类）")
        return [1]


@pytest.mark.asyncio
async def test_adjudication_loop_survives_generic_exception(caplog: pytest.LogCaptureFixture) -> None:
    """注入异常拍：ERROR 留痕 + 协程不退出；后续拍裁决继续执行。"""
    clock = _FakeClock()
    queue = AdjudicationQueue()
    pipeline = _FlakyPipeline()
    stop = asyncio.Event()
    after_calls = {"n": 0}

    async def after_tick(tick: int, sim_now: dt.datetime) -> None:
        after_calls["n"] += 1
        stop.set()  # 第二拍（异常拍已跳过）成功收尾即停机

    queue.put_wakeup(1, "A01", reason="test")
    queue.put_wakeup(2, "A02", reason="test")
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(
            adjudication_loop(
                None, queue, pipeline._gw, clock, pipeline,
                stop=stop, notify=None, agent_ids=("A01", "A02"),
                after_tick=after_tick,
            ),
            timeout=5,
        )
    assert pipeline.calls == 2, "异常被隔离后第二拍仍须执行裁决"
    assert after_calls["n"] == 1, "异常拍跳过本拍收尾（含 after_tick），成功拍正常收尾"
    assert any("T-ITER2-01⑤" in r.getMessage() and r.levelno >= logging.ERROR for r in caplog.records), \
        "未预期异常必须 ERROR 留痕（告警通道）"
