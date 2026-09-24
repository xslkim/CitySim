"""T-ADJ-01 裁决优先级队列验收。

口径：02 文档 T-ADJ-01 验收 1~2（优先级顺序/同 tick 去重/跨 tick 不去重 +
test_wakeup_dedup_same_tick）。
"""

from __future__ import annotations

from worldsim.adjudicator.queue import (
    CLOCK_TICK,
    WAKEUP,
    WORLD_EVENT,
    AdjudicationQueue,
)


def test_priority_order() -> None:
    """世界事件 > 交互唤醒 > 时钟兜底；乱序入队按优先级出队。"""
    q = AdjudicationQueue()
    q.put_clock_tick(1)
    q.put_wakeup(1, "A02", reason="被邀约")
    q.put_world_event(1, {"type": "world.announce"})
    q.put_wakeup(1, "A01", reason="被 @")
    q.put_world_event(1, {"type": "world.overtime"})
    items = q.pop_due(1)
    kinds = [i.kind for i in items]
    assert kinds == [WORLD_EVENT, WORLD_EVENT, WAKEUP, WAKEUP, CLOCK_TICK]
    # 同优先级保入队序
    assert [i.agent_id for i in items if i.kind == WAKEUP] == ["A02", "A01"]
    assert len(q) == 0


def test_wakeup_dedup_same_tick() -> None:
    """验收 2 指定用例：同 tick 对同一 agent 注入 3 个 wakeup，出队仅 1 个（原因合并）。"""
    q = AdjudicationQueue()
    assert q.put_wakeup(5, "A01", reason="chat") is not None
    assert q.put_wakeup(5, "A01", reason="invite", caused_by="1089") is None
    assert q.put_wakeup(5, "A01", reason="argue", caused_by="1090") is None
    items = q.pop_due(5)
    wakeups = [i for i in items if i.kind == WAKEUP and i.agent_id == "A01"]
    assert len(wakeups) == 1
    assert wakeups[0].payload["reasons"] == ["chat", "invite", "argue"]
    assert wakeups[0].payload["caused_by"] == ["1089", "1090"]  # 裸 seq 数字字符串形态保持


def test_wakeup_no_dedup_across_ticks() -> None:
    """跨 tick 不去重：同 agent 不同 tick 的 wakeup 各自保留。"""
    q = AdjudicationQueue()
    q.put_wakeup(5, "A01", reason="r5")
    q.put_wakeup(6, "A01", reason="r6")
    items = q.pop_due(6)
    wakeups = [i for i in items if i.kind == WAKEUP]
    assert len(wakeups) == 2
    assert [i.tick for i in wakeups] == [5, 6]


def test_pop_due_respects_tick_and_stable_serial_order() -> None:
    """只取 tick <= 裁决点的元素；未来 tick 留队；返回序即串行落库顺序（T-ADJ-02 衔接点）。"""
    q = AdjudicationQueue()
    q.put_world_event(3, {"n": 1})
    q.put_clock_tick(2)
    q.put_wakeup(2, "A03")
    early = q.pop_due(2)
    assert [(i.tick, i.kind) for i in early] == [(2, WAKEUP), (2, CLOCK_TICK)]
    late = q.pop_due(3)
    assert [(i.tick, i.kind) for i in late] == [(3, WORLD_EVENT)]
    assert len(q) == 0


def test_clock_tick_idempotent_per_tick() -> None:
    q = AdjudicationQueue()
    first = q.put_clock_tick(4)
    assert q.put_clock_tick(4) is first
    assert len(q) == 1
