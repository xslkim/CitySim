"""batch 段回调注册表（02 T-TIME-03 增项；04 §3.3）。

world_agent 日历/审计钩子的**唯一挂载点**：`batch_advance` 在 chunk 摘要后、落
`time.batch_advanced` 前按注册序逐一 `await` 执行——替代 04 §3.3 伪码中
`world_agent.run_batch_calendar()` 的硬编码直调。

挂载方（均后续里程碑）：04 T-WA-02 `run_batch_calendar()`（含 `time.day_summary` 日界钩子）、
02 T-MEM-03 摘要合并、02 T-ADJ-09 快照 dump、08 审计钩子（M3 起）。M1 交付带空跑自检。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

BatchHookFn = Callable[["BatchContext"], Awaitable[None]]

_HOOKS: list[tuple[str, BatchHookFn]] = []


@dataclass(frozen=True)
class BatchContext:
    """传给每个 batch 钩子的上下文。"""

    sim_hours: float
    agent_ids: tuple[str, ...]
    clock: Any  # TimeEngine（鸭子类型，避免 time_engine 内部环导入）


def register_batch_hook(name: str, fn: BatchHookFn) -> None:
    """注册 batch 段钩子；同名重复注册直接报错（挂载点唯一性防重）。"""
    if any(n == name for n, _ in _HOOKS):
        raise ValueError(f"batch hook {name!r} 已注册（唯一挂载点，禁止重复）")
    _HOOKS.append((name, fn))
    log.info("batch hook 注册：%s（共 %d 个）", name, len(_HOOKS))


def registered_hooks() -> tuple[str, ...]:
    return tuple(n for n, _ in _HOOKS)


def clear_batch_hooks() -> None:
    """清空注册表（测试隔离用；生产代码不得调用）。"""
    _HOOKS.clear()


async def run_batch_hooks(ctx: BatchContext) -> None:
    """按注册序逐一 await 执行；空注册表空跑（M1 自检口径）。"""
    for name, fn in list(_HOOKS):
        log.debug("batch hook 执行：%s", name)
        await fn(ctx)
