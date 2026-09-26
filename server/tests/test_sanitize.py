"""R1 迭代包 #3：观众可见文本出站清洗验收（黑话黑名单 + 代码/JSON 残块拦截 + 人话兜底）。"""

from __future__ import annotations

import json

from worldsim.sanitize import (
    FALLBACK_GENERIC,
    FALLBACK_REFLECTION,
    display_or_fallback,
    sanitize_display_text,
)


def test_jargon_blacklist_blocks() -> None:
    """验收词表（```/json/§/LLM/降级/topic_hint/tick/sim_cost/解析失败）整段拦截。"""
    for bad in (
        '```json\n{"intent": "x',
        "LLM 输出解析失败降级，04 §6.1 step3",
        "走神了（LLM 输出解析失败降级）",
        "想做think但 topic_hint 被现实阻止",
        "这一 tick 的 sim_cost 行",
        '{"diary": "截断的残块',          # 裸 JSON 残块（无围栏）
        '前缀 "intent": 后文',            # JSON 键值对形态
    ):
        assert sanitize_display_text(bad) is None, bad


def test_normal_text_passes() -> None:
    """正常人话/中文文案原样通过（去首尾空白）。"""
    for good in (
        "林晚在茶水间想起昨天的对话。",
        "今天楼下开了家新的咖啡店。",
        "工资到账，心情不错。",
    ):
        assert sanitize_display_text(good) == good


def test_fallback_sentence_clean() -> None:
    """兜底句自身不含任何黑名单词（可安全上屏）。"""
    assert sanitize_display_text(FALLBACK_GENERIC) == FALLBACK_GENERIC
    assert sanitize_display_text(FALLBACK_REFLECTION) == FALLBACK_REFLECTION


def test_display_or_fallback() -> None:
    assert display_or_fallback("好文案") == "好文案"
    assert display_or_fallback('```json {"a":1}') == FALLBACK_GENERIC
    assert display_or_fallback(None) == FALLBACK_GENERIC


def test_reflect_parse_broken_output_r1_3() -> None:
    """内核生成侧：reflection 解析失败 → 人话兜底句而非报错原文（seq151 类残块不出站）。"""
    from worldsim.memory.reflect import Reflector

    truncated = '```json\n{"insights": ["今天和林晚聊',  # 截断残块
    assert Reflector._parse_insights(truncated) == [FALLBACK_REFLECTION]
    assert Reflector._parse_diary(truncated) == FALLBACK_REFLECTION
    ok = json.dumps({"insights": ["今天想通了一件事。"]}, ensure_ascii=False)
    assert Reflector._parse_insights(ok) == ["今天想通了一件事。"]
    ok_diary = json.dumps({"diary": "平淡但安稳的一天。"}, ensure_ascii=False)
    assert Reflector._parse_diary(ok_diary) == "平淡但安稳的一天。"


def test_serde_sanitize_event_payload() -> None:
    """obs 出站：text_display 与 lines[].text_display 过清洗，其余键原样。"""
    import datetime as dt

    from worldsim.observe.serde import serialize_event

    class Row(dict):
        def __getitem__(self, k):
            return super().__getitem__(k)

    row = Row(
        seq=1, tick=1, sim_time=dt.datetime(2026, 10, 12, 9, tzinfo=dt.timezone.utc),
        type="agent.reflection", source="agent:A01", trigger="autonomous", arc_id=None,
        actors=["A01"], location_id=None,
        payload={"text_display": '```json {"insights": ["截断', "other": 1},
        ui=None,
    )
    ev = serialize_event(row)
    assert ev["payload"]["text_display"] == FALLBACK_GENERIC
    assert ev["payload"]["other"] == 1

    row2 = Row(
        seq=2, tick=2, sim_time=dt.datetime(2026, 10, 12, 9, tzinfo=dt.timezone.utc),
        type="dialogue.chat", source="agent:A01", trigger="autonomous", arc_id=None,
        actors=["A01", "A02"], location_id="corp.pantry",
        payload={"lines": [{"speaker": "A01", "text_display": "LLM 降级文本", "at_offset_s": 0.0},
                           {"speaker": "A02", "text_display": "正常台词", "at_offset_s": 1.0}]},
        ui=None,
    )
    ev2 = serialize_event(row2)
    lines = ev2["payload"]["lines"]
    assert lines[0]["text_display"] == "（这句没听清）"
    assert lines[1]["text_display"] == "正常台词"
