"""T-SYN-01 键白名单验收（06 §1.2 标注列唯一依据；05 §2.1 剥除规则）。"""

from __future__ import annotations

from worldsim.sync.whitelist import strip_payload


def test_text_raw_always_stripped() -> None:
    """验收 1：含 text_raw 的 public 事件剥除后键集无 text_raw（红线 7）。"""
    out = strip_payload(
        {"participants": ["A01", "A02"], "lines": [], "text_display": "你好",
         "text_raw": "原始未过审文本", "prompt": "系统提示片段"},
        type_="dialogue.chat", visibility="public",
    )
    assert "text_raw" not in out
    assert "prompt" not in out
    assert out["text_display"] == "你好"  # conditional 键 public 放行
    assert out["participants"] == ["A01", "A02"]


def test_unregistered_key_stripped() -> None:
    """验收 2：06 §1.2 未登记键默认剥除。"""
    out = strip_payload(
        {"from": "corp.tech", "to": "corp.pantry", "sim_cost_min": 3, "debug_trace": {"x": 1}},
        type_="agent.move", visibility="internal",
    )
    assert out == {"from": "corp.tech", "to": "corp.pantry", "sim_cost_min": 3}


def test_text_display_public_only() -> None:
    """验收 3：text_display 仅 visibility='public' 放行（05 §2.1 N-P0-2）。"""
    payload = {"participants": ["A01", "A02"], "text_display": "展示文本"}
    pub = strip_payload(payload, type_="dialogue.chat", visibility="public")
    internal = strip_payload(payload, type_="dialogue.chat", visibility="internal")
    assert "text_display" in pub
    assert "text_display" not in internal
    assert internal == {"participants": ["A01", "A02"]}


def test_per_type_annotations() -> None:
    """验收 4：06 §1.2 逐行抽查（promoted 评分中间量剥除 / gossip cites 放行 / reflection 仅 text_display）。"""
    promoted = strip_payload(
        {"from_tier": "secondary", "to_tier": "star", "reason": "冷启动加成",
         "caused_by": "1089", "score_breakdown": {"heat": 0.9}, "gini": 0.42},
        type_="agent.promoted", visibility="public",
    )
    assert promoted == {"from_tier": "secondary", "to_tier": "star",
                        "reason": "冷启动加成", "caused_by": "1089"}
    gossip = strip_payload(
        {"teller": "A01", "listener": "A02", "about": "A03", "cites": ["101", "102"],
         "lines": [], "text_display": "听说了吗"},
        type_="dialogue.gossip", visibility="public",
    )
    assert gossip["cites"] == ["101", "102"]
    reflection = strip_payload(
        {"text_display": "反思摘要", "mood_after": 61, "salience": 0.8},
        type_="agent.reflection", visibility="public",
    )
    assert reflection == {"text_display": "反思摘要"}


def test_internal_event_structured_payload_kept() -> None:
    """05 §2.1：internal 事件结构化 payload 照常放行，仅展示文本键剥除。"""
    out = strip_payload(
        {"changes": [{"agent_id": "A01", "need": "hunger", "delta": -5, "new_value": 60, "cause": "eat"}],
         "text_display": "不该出现"},
        type_="state.needs_delta", visibility="internal",
    )
    assert "changes" in out
    assert "text_display" not in out
