"""T-LLM-10 输出结构化解析验收（03 验收 1~3；04 §6.1 step3，01 §7）。

- 各 task_type 合法样例过 schema；
- 首次非法 → 重试请求带纠错后缀（mock 断言 prompt 增量）；二次仍非法 → think 降级决策 + 恰好 2 次调用；
- schema 动作枚举与共享常量 ACTIONS（01 §4，19 项）集相等。
"""

from __future__ import annotations

import json

import pytest

from worldsim.adjudicator.validators import ACTIONS
from worldsim.llm_gateway import parse as llm_parse
from worldsim.llm_gateway.providers.base import ChatResult
from worldsim.llm_gateway.providers.base import TASK_TYPES

pytestmark = pytest.mark.asyncio

VALID_SAMPLES = {
    "star_decision": {"intent": "去天台透透气", "action": {"type": "move", "args": {"to": "apt.roof"}},
                      "say": None, "emotion_delta": {"mood": 1}},
    "dialogue": {"lines": [{"speaker": "A01", "text": "昨晚那家店真不错"}, {"speaker": "A02", "text": "是啊还想再去"}] * 3,
                 "opening_fact": {"type": "memory", "ref": "mem:42"},
                 "self_eval": {"a_enjoy": 8, "b_enjoy": 7, "basis": "聊得投机"},
                 "quotable_lines": ["昨晚那家店真不错"]},
    "secondary": {"intent": "想去买点吃的", "emotion_delta": {}, "action": {"type": "shop", "args": {}}, "gossip": None},
    "bgsummary": {"diary": "今天照常上班。", "needs_delta": {}, "mood": "平稳", "tomorrow_plan": "明天继续"},
    "reflection": {"insights": ["最近睡得不够", "和 A02 的关系在升温"]},
    "director": {"note": "加强 A03 线索", "actions": []},
    "world_copy": {"title": "公告", "body": "今晚小区停电检修"},
    "safety": {"label": "pass"},
}


def test_valid_outputs_all_types() -> None:
    """各 task_type 合法样例过 schema（03 验收 1）。"""
    for task_type, sample in VALID_SAMPLES.items():
        llm_parse.validate(task_type, sample)
        llm_parse.parse_json(task_type, json.dumps(sample, ensure_ascii=False))
    # schema 覆盖全集 chat 类 task_type（embed 无 LLM 输出 schema）
    for t in TASK_TYPES:
        if t == "embed":
            continue
        llm_parse._schema(t)  # 不抛 SchemaNotFound 即存在
    # 非法样例被拒
    bad = dict(VALID_SAMPLES["dialogue"], lines=[])
    with pytest.raises(llm_parse.OutputValidationError):
        llm_parse.validate("dialogue", bad)
    with pytest.raises(llm_parse.OutputValidationError):
        llm_parse.parse_json("star_decision", "not json at all")


class _ScriptedChat:
    """脚本化 chat 桩：记录收到的 messages，按脚本返回。"""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[list[dict]] = []

    async def __call__(self, task_type, messages, gen_params) -> ChatResult:
        self.calls.append(messages)
        return ChatResult(text=self._texts.pop(0), prompt_tokens=1, completion_tokens=1,
                          latency_ms=1, request_id="r", provider="stub", model="m")


async def test_retry_once_then_fallback_think() -> None:
    """首次非法 → 重试带纠错后缀；二次仍非法 → think『走神了』降级决策 + 恰好 2 次调用（03 验收 2）。"""
    stub = _ScriptedChat(["garbage", "still garbage"])
    failures: list[str] = []

    async def record_failure(err: str) -> None:
        failures.append(err)

    out = await llm_parse.call_and_parse(
        stub, "star_decision", [{"role": "user", "content": "决策"}],
        record_failure=record_failure,
    )
    assert len(stub.calls) == 2, f"恰好 2 次 provider 调用（实测 {len(stub.calls)}）"
    assert len(stub.calls[1]) == 2 and "格式纠错" in stub.calls[1][-1]["content"], "重试请求必须追加纠错后缀"
    assert out == llm_parse.FALLBACK_THINK and out["action"]["type"] == "think"
    assert len(failures) == 1  # 落 llm_calls status='failed' 行一次

    # 首次非法、重试合法 → 正常返回，不降级
    stub2 = _ScriptedChat(["garbage", json.dumps(VALID_SAMPLES["secondary"], ensure_ascii=False)])
    out2 = await llm_parse.call_and_parse(stub2, "secondary", [{"role": "user", "content": "x"}])
    assert out2["intent"] == "想去买点吃的" and len(stub2.calls) == 2


def test_action_enum_matches_19() -> None:
    """schema 动作枚举与共享常量集相等（03 验收 3；01 §4 唯一持有方 19 项，禁止抄副本漂移）。"""
    assert len(ACTIONS) == 19
    for task_type in ("star_decision", "secondary"):
        enum = set(llm_parse._schema(task_type)["properties"]["action"]["properties"]["type"]["enum"])
        assert enum == set(ACTIONS), f"{task_type} schema 动作枚举与 01 §4 共享常量漂移"
