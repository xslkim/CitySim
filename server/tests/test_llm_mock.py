"""T-LLM-02 mock provider 验收：确定性 / schema 对齐 / 零网络。

口径：03 文档 T-LLM-02 验收 1~3。验收 3 说明：`--disable-socket` 与 pytest-asyncio 的
function 级事件循环不兼容（CPython 3.12 `_make_self_pipe` 走 `socket.socketpair`，建循环即被封禁），
故按任务书"或等价 socket 封禁"口径：本模块 autouse fixture 在用例执行期封禁全部出站 socket 通道
（connect/connect_ex/create_connection/getaddrinfo），断言 mock 全程零网络。
"""

from __future__ import annotations

import json
import math
import socket

import pytest

from worldsim.llm_gateway.providers.base import TASK_TYPES
from worldsim.llm_gateway.providers.mock import MockProvider

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _ban_outbound_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """等价 socket 封禁（03 验收 3）：一切出站通道抛 SocketBlockedError。"""

    def _blocked(*args, **kwargs):  # noqa: ANN202
        raise OSError("socket 封禁中：mock provider 不得触网（03 T-LLM-02 验收 3）")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

_MESSAGES = [
    {"role": "system", "content": "你是角色扮演引擎。只输出 JSON。"},
    {"role": "user", "content": 'OBS_JSON={"agent_id":"A01","position":"apt.L2.203","exits":["apt.lobby","apt.roof"]}\n做人设决策。'},
]


async def test_mock_deterministic() -> None:
    """同 seed 同 prompt 两次调用输出逐字节全等；异 seed 输出不同（03 验收 1）。"""
    mock = MockProvider()
    a = await mock.chat("star_decision", _MESSAGES, seed=42)
    b = await mock.chat("star_decision", _MESSAGES, seed=42)
    assert a.text == b.text and a.request_id == b.request_id
    c = await mock.chat("star_decision", _MESSAGES, seed=43)
    assert c.text != a.text
    # embed 同口径：同 seed 同文本向量全等，异 seed 不同
    va = await mock.embed(["记忆条目"], seed=42)
    vb = await mock.embed(["记忆条目"], seed=42)
    assert va.vectors == vb.vectors
    vc = await mock.embed(["记忆条目"], seed=43)
    assert vc.vectors != va.vectors


async def test_mock_output_passes_schema() -> None:
    """全 task_type 的 mock 输出过 T-LLM-10 JSON schema（03 验收 2；schema 未落地前 xfail，禁止跳过）。"""
    try:
        from worldsim.llm_gateway import parse as llm_parse  # T-LLM-10（M2）交付
    except ImportError:
        pytest.xfail("T-LLM-10 schema 未落地（M2），落地后本用例自动转绿")
        return
    mock = MockProvider()
    for t in TASK_TYPES:
        if t == "embed":
            continue
        out = await mock.chat(t, _MESSAGES, seed=7)
        llm_parse.validate(t, json.loads(out.text))  # type: ignore[attr-defined]


async def test_mock_output_shape_kernel_contract() -> None:
    """M1 内核消费侧契约（T-LLM-10 前的过渡断言）：各 task_type 输出为合法 JSON 且关键键在位。"""
    mock = MockProvider()
    decision = json.loads((await mock.chat("star_decision", _MESSAGES, seed=7)).text)
    assert set(decision) >= {"intent", "action", "emotion_delta"}
    assert decision["action"]["type"] in {"think", "move"}
    if decision["action"]["type"] == "move":
        assert decision["action"]["args"]["to"] in ["apt.lobby", "apt.roof"]
    dialogue = json.loads((await mock.chat("dialogue", _MESSAGES, seed=7)).text)
    assert 6 <= len(dialogue["lines"]) <= 8
    assert set(dialogue) >= {"lines", "opening_fact", "self_eval", "quotable_lines"}
    bg = json.loads((await mock.chat("bgsummary", _MESSAGES, seed=7)).text)
    assert set(bg) >= {"diary", "needs_delta", "mood", "tomorrow_plan"}
    refl = json.loads((await mock.chat("reflection", _MESSAGES, seed=7)).text)
    assert 2 <= len(refl["insights"]) <= 3
    safety = json.loads((await mock.chat("safety", _MESSAGES, seed=7)).text)
    assert safety["label"] in {"pass", "soft", "block"}


async def test_mock_embed_unit_vector_dim() -> None:
    """embed 输出 = 1024 维单位向量（04 §5.2 vector(1024) 口径）。"""
    mock = MockProvider()
    result = await mock.embed(["a", "b"], seed=1)
    assert result.dimensions == 1024 and len(result.vectors) == 2
    for vec in result.vectors:
        assert len(vec) == 1024
        assert math.isclose(sum(v * v for v in vec), 1.0, rel_tol=1e-9)
