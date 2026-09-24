"""裁决优先级队列（02 T-ADJ-01；04 §2.2 主循环伪码/tick 串行裁决语义，00 §4 红线 10）。

- 三类入队源优先级：**世界事件 > 交互唤醒（wakeup）> 时钟兜底（ClockTick）**（04 §2.2 伪码）。
- "事件唤醒 = 进队列、下一裁决点处理"：A 对 B 发起交互，B 不当场跑认知，收 wakeup 入队
  （由交互结算侧以目标 tick 入队，通常 = 当前 tick + 1）；**同 tick 对同 agent 的多个唤醒
  去重合并**（原因合并进 `reasons`）；跨 tick 不去重。
- **只有一个裁决协程消费队列**（04 §2.2 写死）；队列本身不写状态（纯内存，无 DB）。
- 衔接点标注（供 T-ADJ-02 消费）：`pop_due` 返回序即"tick 内 LLM 并发发出、结果按队列顺序
  串行回写"的落库顺序——裁决管道对该批次并发跑 LLM、按本序串行落库。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

WORLD_EVENT = "world_event"
WAKEUP = "wakeup"
CLOCK_TICK = "clock_tick"

KINDS: tuple[str, ...] = (WORLD_EVENT, WAKEUP, CLOCK_TICK)
_PRIORITY = {WORLD_EVENT: 0, WAKEUP: 1, CLOCK_TICK: 2}


@dataclass(frozen=True)
class QueueItem:
    """队列元素。`tick` = 应被处理的裁决点；`order` = 入队序号（稳定排序）。"""

    tick: int
    kind: str
    order: int
    agent_id: str | None = None  # wakeup 必填（'A01'~'A40' TEXT 形态）
    payload: dict[str, Any] = field(default_factory=dict)


class AdjudicationQueue:
    """单消费者优先级堆。线程/asyncio 双安全（put 可来自时钟回调与裁决协程）。"""

    def __init__(self) -> None:
        self._buckets: dict[int, dict[tuple, QueueItem]] = {}
        self._order = 0
        self._lock = threading.Lock()
        self._notified: asyncio.Event | None = None

    # ---- 入队 -----------------------------------------------------------

    def _next_order(self) -> int:
        self._order += 1
        return self._order

    def put_world_event(self, tick: int, payload: dict[str, Any]) -> QueueItem:
        """世界事件入队（最高优先级）。每次调用都是独立元素（不去重）。"""
        with self._lock:
            item = QueueItem(tick=tick, kind=WORLD_EVENT, order=self._next_order(), payload=dict(payload))
            self._buckets.setdefault(tick, {})[(WORLD_EVENT, item.order)] = item
        self._notify()
        return item

    def put_wakeup(self, tick: int, agent_id: str, *, reason: str = "", caused_by: str | None = None) -> QueueItem | None:
        """交互唤醒入队；同 tick 同 agent 去重合并（返回 None 表示被合并）。

        `caused_by` 值形态 = 裸 seq 数字字符串（00 §4 红线 3），由调用方保证。
        """
        with self._lock:
            bucket = self._buckets.setdefault(tick, {})
            key = (WAKEUP, agent_id)
            existing = bucket.get(key)
            if existing is not None:
                reasons = list(existing.payload.get("reasons", []))
                if reason:
                    reasons.append(reason)
                causes = list(existing.payload.get("caused_by", []))
                if caused_by is not None and caused_by not in causes:
                    causes.append(caused_by)
                bucket[key] = QueueItem(
                    tick=tick, kind=WAKEUP, order=existing.order, agent_id=agent_id,
                    payload={"reasons": reasons, "caused_by": causes},
                )
                self._notify()
                return None
            item = QueueItem(
                tick=tick, kind=WAKEUP, order=self._next_order(), agent_id=agent_id,
                payload={"reasons": [reason] if reason else [], "caused_by": [caused_by] if caused_by else []},
            )
            bucket[key] = item
        self._notify()
        return item

    def put_clock_tick(self, tick: int) -> QueueItem:
        """时钟兜底入队（最低优先级；同 tick 幂等）。"""
        with self._lock:
            bucket = self._buckets.setdefault(tick, {})
            key = (CLOCK_TICK,)
            existing = bucket.get(key)
            if existing is not None:
                return existing
            item = QueueItem(tick=tick, kind=CLOCK_TICK, order=self._next_order())
            bucket[key] = item
        self._notify()
        return item

    # ---- 出队（单消费者：裁决协程） --------------------------------------

    def pop_due(self, tick: int) -> list[QueueItem]:
        """取出所有 `item.tick <= tick` 的元素，按 (tick, 优先级, 入队序) 排序。

        排序即 T-ADJ-02 的串行落库顺序：同 tick 内世界事件 → 唤醒 → 时钟兜底。
        """
        with self._lock:
            due_ticks = sorted(t for t in self._buckets if t <= tick)
            items: list[QueueItem] = []
            for t in due_ticks:
                items.extend(self._buckets.pop(t).values())
        items.sort(key=lambda i: (i.tick, _PRIORITY[i.kind], i.order))
        return items

    def peek(self, tick: int) -> list[QueueItem]:
        """只读查看到期元素（不移除）。"""
        with self._lock:
            items = [i for t, bucket in self._buckets.items() if t <= tick for i in bucket.values()]
        items.sort(key=lambda i: (i.tick, _PRIORITY[i.kind], i.order))
        return items

    def __len__(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._buckets.values())

    def pending_ticks(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(self._buckets))

    # ---- 单消费者等待 ----------------------------------------------------

    def bind_notify_event(self, event: asyncio.Event) -> None:
        """绑定通知事件（main 装配：裁决协程 wait，put 触发 set）。"""
        self._notified = event

    def _notify(self) -> None:
        if self._notified is not None:
            try:
                self._notified.set()
            except RuntimeError:
                pass  # 跨 loop 绑定时忽略（测试直接消费不需要通知）
