"""T-LLM-03 zhipu 联网冒烟（03 验收 2）：真实跑一次 glm-4.5-flash chat。

默认 skip（真 LLM 测试口径：不拖垮 CI/免费档 RPM）；显式跑法：
    cd server && uv run pytest tests/test_llm_zhipu_live.py -m live
无 `WSIM_ZHIPU_API_KEY`（或根 .env 无该键）时 skip 并给出显式原因。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from worldsim.llm_gateway.providers.zhipu import ZhipuProvider

pytestmark = [pytest.mark.asyncio, pytest.mark.live]


def _load_key() -> str | None:
    key = os.environ.get("WSIM_ZHIPU_API_KEY")
    if key:
        return key
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("WSIM_ZHIPU_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


@pytest.fixture()
def api_key() -> str:
    key = _load_key()
    if not key:
        pytest.skip("WSIM_ZHIPU_API_KEY 未配置（env 与根 .env 均无）——真 LLM 冒烟跳过")
    return key


async def test_live_glm45_flash_chat(api_key: str) -> None:
    """glm-4.5-flash 真实 chat 一次：JSON 正文可回收、usage 全量、reasoning 不外泄进 content。"""
    p = ZhipuProvider(model="glm-4.5-flash", api_key=api_key)
    r = await p.chat(
        "star_decision",
        [{"role": "user", "content": '只回复 JSON：{"ok": true}'}],
        {"max_tokens": 800, "response_format": "json"},
    )
    assert json.loads(r.text) == {"ok": True}
    assert r.prompt_tokens > 0 and r.completion_tokens > 0
    assert r.request_id and r.latency_ms > 0


async def test_live_glm4_flash_chat(api_key: str) -> None:
    """glm-4-flash 真实 chat 一次（次要/背景/摘要档冒烟）。"""
    p = ZhipuProvider(model="glm-4-flash", api_key=api_key)
    r = await p.chat(
        "safety",
        [{"role": "user", "content": '只回复 JSON：{"label": "pass"}'}],
        {"max_tokens": 50, "response_format": "json"},
    )
    assert json.loads(r.text)["label"] == "pass"
    assert r.prompt_tokens > 0
