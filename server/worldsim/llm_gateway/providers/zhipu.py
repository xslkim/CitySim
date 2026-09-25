"""智谱 GLM provider（03 文档 T-LLM-03；00 §1 A3/A9，04 §8.1/§11.2）。

- httpx 直连 OpenAI 兼容端点 `https://open.bigmodel.cn/api/paas/v4`（无 SDK 依赖，00 §3 钉版）。
- key 只从环境变量 `WSIM_ZHIPU_API_KEY` 读；代码/配置/日志零硬编码（04 §11.2）。
- **glm-4.5-flash 是 reasoning 模型**（00 §1 A9）：响应 `reasoning_content` 消耗 completion 配额
  （`max_tokens` 留足归 models.yaml `generation_params`，04 §8.6）；业务正文只取
  `choices[0].message.content`，`reasoning_content` 不落库、不进日志（04 §12.1）。
- `response_format=json` 档（models.yaml 生成参数，04 §8.6）映射 OpenAI 兼容 `{"type": "json_object"}`。
- 异常上抛形态（供 T-LLM-05 退避层/T-LLM-06 撞墙判定消费）：
  429 → `RateLimited`（解析 `Retry-After`）；5xx → `ServerError`；超时/连接错误 → `ProviderTimeout`；
  其余 4xx → `ClientError`（不重试）。
- embedding 不在本 provider（智谱 embedding-3 余额不足，00 §1 附；embed 路由 = 本地 bge-m3，
  03 T-LLM-03 `local_embed.py`）。
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import httpx

from .base import ChatResult, Message

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"  # 00 §1 A3
API_KEY_ENV = "WSIM_ZHIPU_API_KEY"

# 工程默认超时（设计未定数值，03 §6 D3；models.yaml 可经构造参数覆盖）
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_READ_TIMEOUT_S = 60.0


class ZhipuError(RuntimeError):
    """智谱调用失败基类。`status` = HTTP 状态码（超时类为 None）。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimited(ZhipuError):
    """HTTP 429；`retry_after_s` 取响应 `Retry-After` 头（秒，无则 None，04 §8.5）。"""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after_s = retry_after_s


class ServerError(ZhipuError):
    """HTTP 5xx（重试耗尽后计入撞墙条件①，04 §8.2）。"""


class ClientError(ZhipuError):
    """HTTP 4xx（非 429）：不重试的调用侧错误。"""


class ProviderTimeout(ZhipuError):
    """connect/read 超时或连接错误（撞墙判定按 5xx 同类计入，04 §8.2 ③ 延迟口径）。"""


class MissingApiKey(ClientError):
    """WSIM_ZHIPU_API_KEY 未配置（配置类错误，不重试）。"""


class ZhipuProvider:
    """智谱 GLM chat provider（OpenAI 兼容端点，httpx 直连）。

    构造参数均可由 models.yaml `providers.zhipu` 段驱动（T-LLM-04 router 装配）；
    `transport` 仅供测试注入 `httpx.MockTransport`。
    """

    name = "zhipu"

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        thinking: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        self._timeout = httpx.Timeout(read_timeout_s, connect=connect_timeout_s)
        self._thinking = thinking  # "disabled"/"enabled"（models.yaml 模型条目 thinking 键，03 §6 D44）
        self._transport = transport

    def for_model(self, model: str, *, thinking: str | None = None) -> ZhipuProvider:
        """按型号绑定副本（同 provider 别名多型号共享连接/超时/密钥，型号逐跳不同，04 §8.1 降级链）。

        `thinking` 缺省继承母实例；显式传入覆盖（网关按 models.yaml 模型条目注入）。
        """
        return ZhipuProvider(
            model=model, base_url=self._base_url, api_key=self._api_key,
            connect_timeout_s=self._timeout.connect, read_timeout_s=self._timeout.read,
            thinking=self._thinking if thinking is None else thinking,
            transport=self._transport,
        )

    async def chat(
        self,
        task_type: str,
        messages: list[Message],
        gen_params: dict[str, Any] | None = None,
        *,
        seed: int | None = None,
    ) -> ChatResult:
        if not self._api_key:
            raise MissingApiKey(f"缺 {API_KEY_ENV}（04 §11.2：key 只存本机 env，不进代码/配置/日志）")
        body = self._request_body(messages, gen_params or {})
        started = time.perf_counter()
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout, transport=self._transport,
        ) as client:
            try:
                resp = await client.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                raise ProviderTimeout(f"智谱调用超时/连接失败：{exc.__class__.__name__}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._raise_for_status(resp)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        # reasoning 模型：正文只取 content；reasoning_content 不落库不进日志（04 §12.1，00 §1 A9）
        text = message.get("content") or ""
        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
            request_id=str(data.get("id") or resp.headers.get("x-request-id") or uuid.uuid4().hex),
            provider=self.name,
            model=str(data.get("model") or self.model),
        )

    # ---- 内部 -----------------------------------------------------------

    def _request_body(self, messages: list[Message], gen_params: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        for key in ("temperature", "max_tokens", "presence_penalty", "top_p"):
            if key in gen_params:
                body[key] = gen_params[key]
        if gen_params.get("response_format") == "json":  # 04 §8.6 response_format=json 档
            body["response_format"] = {"type": "json_object"}
        if self._thinking:
            body["thinking"] = {"type": self._thinking}  # 03 §6 D44（免费档关 reasoning 控延迟/配额）
        return body

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            err = resp.json().get("error") or {}
            detail = str(err.get("message") or err.get("code") or "")
        except ValueError:
            detail = resp.text[:200]
        if resp.status_code == 429:
            retry_after: float | None = None
            raw = resp.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
            raise RateLimited(f"智谱 429：{detail}", retry_after_s=retry_after)
        if resp.status_code >= 500:
            raise ServerError(f"智谱 {resp.status_code}：{detail}", status=resp.status_code)
        raise ClientError(f"智谱 {resp.status_code}：{detail}", status=resp.status_code)
