"""T-LLM-09 prompt 模板注册表验收（03 验收 1~4）。

- 12 个模板族全部用 fixture 上下文渲染成功（fixture 含 topic/willingness）；缺失变量抛 MissingVariable；
- gossip：fidelity=0.6 → 渲染文本含"丢数字/时间"档指令（阈值口径 01 §4.1，模板只引用不复制数值）；
- 次要层模板渲染 fixture ≤900 tokens 预算断言（预算持有方 04 §4.3；tiktoken cl100k_base 近似，
  CJK 文本按字符数上界复核，方法见用例注释）。
"""

from __future__ import annotations

import pytest
import tiktoken

from worldsim.llm_gateway.prompts import (
    MissingVariable,
    PromptRegistry,
    gossip_distortion_instruction,
)

FIXTURE = {
    "persona": {"name": "叶蓁", "age": 26, "occupation": "咖啡师", "speech_style": {"tone": "温和"}},
    "obs": {"sim_time": "2026-09-25 10:00", "position": "apt.lobby", "exits": ["apt.roof"]},
    "mems": [{"id": 1, "content": "昨天和 A02 一起喝了咖啡"}],
    "goals": ["攒够钱开自己的店"],
    "topic": {"topic_id": "T-DLY-01", "text": "晚饭吃什么"},
    "willingness": 62.5,
    "fidelity": 0.6,
    "day": "2026-10-12",
    "candidates": "e101 [B] dialogue.chat: 测试",
}

EXPECTED_IDS = {
    "think", "chat", "argue", "gossip", "confess", "apologize",
    "send_message", "invite", "refuse", "reflect", "daily_summary", "batch_summary",
    "director_review",  # 04 T-DIR-05 K3 复核模板（M3 新增族）
}

# 模板族 → task_type 映射（03 T-LLM-09 实现要点清单）
EXPECTED_TASK_TYPE = {
    "think": "secondary", "chat": "dialogue", "argue": "dialogue", "gossip": "dialogue",
    "confess": "dialogue", "apologize": "dialogue", "send_message": "dialogue",
    "invite": "dialogue", "refuse": "dialogue", "reflect": "reflection",
    "daily_summary": "secondary", "batch_summary": "bgsummary",
    "director_review": "director",
}


@pytest.fixture(scope="module")
def registry() -> PromptRegistry:
    return PromptRegistry.load()


def test_all_templates_render_with_fixture(registry: PromptRegistry) -> None:
    """12 族全渲染（03 验收 1）；缺变量抛 MissingVariable。"""
    assert set(registry.ids()) == EXPECTED_IDS
    for tid in registry.ids():
        template_id, version, messages = registry.render(tid, **FIXTURE)
        assert template_id == tid and version
        assert [m["role"] for m in messages] == ["system", "user"]
        assert all(m["content"].strip() for m in messages)
        assert registry.get(tid).task_type == EXPECTED_TASK_TYPE[tid]
    with pytest.raises(MissingVariable):
        registry.render("chat", persona=FIXTURE["persona"])  # 缺 obs/mems/goals/topic/willingness
    with pytest.raises(MissingVariable):
        registry.render("gossip", **{k: v for k, v in FIXTURE.items() if k != "fidelity"})


def test_gossip_template_injects_fidelity(registry: PromptRegistry) -> None:
    """fidelity=0.6 → 渲染文本含"丢数字/时间"档指令（03 验收 2；01 §4.1 档位）。"""
    _, _, messages = registry.render("gossip", **FIXTURE)
    user = messages[1]["content"]
    assert "丢" in user and "数字" in user and "时间" in user
    assert "0.6" in user  # 保真度数值随指令注入
    # 档位函数三档（01 §4.1：<0.7 丢数字/时间；<0.55 只留主干）
    assert "主干" in gossip_distortion_instruction(0.5)
    assert "数字" in gossip_distortion_instruction(0.6)
    assert "忠实" in gossip_distortion_instruction(0.9)


def test_no_at_offset_s_in_prompts() -> None:
    """`lines[].at_offset_s` 不是 LLM 输出字段（03 §6 D5；验收 3 的 grep 单测化）。"""
    from pathlib import Path

    prompts_dir = Path(__file__).resolve().parents[1] / "config" / "prompts"
    for f in prompts_dir.glob("*.yaml"):
        assert "at_offset_s" not in f.read_text(encoding="utf-8"), f.name


def test_secondary_templates_within_token_budget(registry: PromptRegistry) -> None:
    """次要层模板渲染 ≤900 tokens（04 §4.3 预算；方法：tiktoken cl100k_base 计数 + 字符数上界复核——
    CJK 在 cl100k 下约 1 token/字，二者取大作为保守上界断言）。"""
    enc = tiktoken.get_encoding("cl100k_base")
    for tid in ("think", "daily_summary"):  # secondary 档模板
        _, _, messages = registry.render(tid, **FIXTURE)
        total_chars = sum(len(m["content"]) for m in messages)
        total_tokens = sum(len(enc.encode(m["content"])) for m in messages)
        assert max(total_tokens, total_chars) <= 900, f"{tid} 超 900 tokens 预算（tokens={total_tokens}, chars={total_chars}）"
