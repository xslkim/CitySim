"""T-LLM-03 zhipu provider 验收（httpx MockTransport 离线用例；03 验收 1）。

覆盖：200 正常解析（token/延迟/request_id、reasoning_content 只取 content）、429 带 Retry-After、
500 上抛 ServerError、超时上抛 ProviderTimeout、response_format=json 档映射 json_object。
联网冒烟在 test_llm_zhipu_live.py（默认 skip）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from worldsim.llm_gateway.providers.zhipu import (
    ClientError,
    MissingApiKey,
    ProviderTimeout,
    RateLimited,
    ServerError,
    ZhipuProvider,
)

pytestmark = pytest.mark.asyncio

_MESSAGES = [{"role": "user", "content": "只回复 JSON"}]


def _provider(handler, monkeypatch: pytest.MonkeyPatch, **kwargs) -> ZhipuProvider:
    monkeypatch.setenv("WSIM_ZHIPU_API_KEY", "test-key-not-real")
    return ZhipuProvider(model="glm-4.5-flash", transport=httpx.MockTransport(handler), **kwargs)


async def test_ok_parses_usage_and_content_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """200：token/延迟/request_id 解析；响应带 reasoning_content 时业务正文只取 content（00 §1 A9）。"""
    seen_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_body
        seen_body = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "req-abc123",
            "model": "glm-4.5-flash",
            "choices": [{"message": {
                "role": "assistant",
                "content": '{"intent":"x"}',
                "reasoning_content": "推理过程不应外泄",
            }}],
            "usage": {"prompt_tokens": 68, "completion_tokens": 95, "total_tokens": 163},
        })

    p = _provider(handler, monkeypatch)
    r = await p.chat("star_decision", _MESSAGES, {"temperature": 0.8, "max_tokens": 400, "response_format": "json"})
    assert r.text == '{"intent":"x"}' and "推理" not in r.text
    assert r.prompt_tokens == 68 and r.completion_tokens == 95
    assert r.request_id == "req-abc123" and r.latency_ms >= 0
    assert r.provider == "zhipu" and r.model == "glm-4.5-flash"
    # response_format=json 档 → OpenAI 兼容 json_object；参数透传
    assert seen_body["response_format"] == {"type": "json_object"}
    assert seen_body["temperature"] == 0.8 and seen_body["max_tokens"] == 400
    assert seen_body["model"] == "glm-4.5-flash"
    assert "reasoning_content" not in json.dumps(seen_body)


async def test_429_raises_rate_limited_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 → RateLimited 且解析 Retry-After（04 §8.5）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "RPM 超限"}}, headers={"Retry-After": "7"})

    p = _provider(handler, monkeypatch)
    with pytest.raises(RateLimited) as ei:
        await p.chat("secondary", _MESSAGES)
    assert ei.value.retry_after_s == 7.0 and ei.value.status == 429


async def test_500_raises_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": {"message": "bad gateway"}})

    p = _provider(handler, monkeypatch)
    with pytest.raises(ServerError) as ei:
        await p.chat("secondary", _MESSAGES)
    assert ei.value.status == 502


async def test_timeout_raises_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    p = _provider(handler, monkeypatch)
    with pytest.raises(ProviderTimeout):
        await p.chat("dialogue", _MESSAGES)


async def test_400_raises_client_error_no_retry_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    p = _provider(handler, monkeypatch)
    with pytest.raises(ClientError) as ei:
        await p.chat("dialogue", _MESSAGES)
    assert ei.value.status == 400


async def test_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WSIM_ZHIPU_API_KEY", raising=False)
    p = ZhipuProvider(model="glm-4-flash", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(MissingApiKey):
        await p.chat("safety", _MESSAGES)


async def test_request_id_fallback_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """响应无 id 时本地 uuid 兜底（04 §12.1：日志只记 request_id）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    p = _provider(handler, monkeypatch)
    r = await p.chat("bgsummary", _MESSAGES)
    assert r.request_id and len(r.request_id) >= 8
