"""凌晨 batch 段粗粒度推进（02 T-TIME-03；04 §3.3 伪码逐行）。

- 分 chunk 并发（chunk 大小 = 10，04 §3.3"40 人分 4 批，并发 10"）逐 agent 摘要快进；
  batch 段内明星/次要层不跑常规认知循环；背景层日摘要唯一时点即此段。
- 编剧在该段的 A 级事件**先插队结算再快进**（M1 无编剧，`director_preempt` 留接口，04 §3.3）。
- chunk 摘要后、落 `time.batch_advanced` 前经 `batch_hooks` 注册表按注册序执行钩子（唯一挂载点）。
- `time.batch_advanced` 事件 payload 仅含 `sim_hours`（06 §1.2 逐字）。
- 依赖方向（04 §2.1）：本模块不 import 业务模块；`summarize`（摘要器）由调用方注入
  （M1 = 网关 mock bgsummary；`clock` 鸭子类型取 TimeEngine 能力）。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .batch_hooks import BatchContext, run_batch_hooks

log = logging.getLogger(__name__)

CHUNK_SIZE = 10  # 04 §3.3 伪码：40 人分 4 批，并发 10


def _chunked(items: tuple[str, ...], n: int) -> list[tuple[str, ...]]:
    return [tuple(c) for c in itertools.batched(items, n)]


async def batch_advance(
    clock: Any,
    agent_ids: tuple[str, ...] | list[str],
    sim_hours: float,
    *,
    summarize: Callable[[str, float], Awaitable[None]],
    director_preempt: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """batch 段推进闭环：编剧插队 → enter → 分 chunk 并发摘要 → 钩子 → 事件 → exit。"""
    ids = tuple(agent_ids)
    if director_preempt is not None:
        await director_preempt()  # 编剧 A 级事件插队结算（M1 留接口，04 §3.3）
    await clock.enter_batch_mode(sim_hours)
    try:
        for chunk in _chunked(ids, CHUNK_SIZE):
            await asyncio.gather(*[summarize(aid, sim_hours) for aid in chunk])
        await run_batch_hooks(BatchContext(sim_hours=sim_hours, agent_ids=ids, clock=clock))
        await clock.emit_time_event("time.batch_advanced", {"sim_hours": sim_hours})
    finally:
        await clock.exit_batch_mode()
    log.info("batch 段推进完成：%d agents × %.1f sim_hours", len(ids), sim_hours)
