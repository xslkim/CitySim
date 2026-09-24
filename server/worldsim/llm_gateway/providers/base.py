"""provider 协议抽象（03 文档 T-LLM-01，M1 段交付；04 §2.1/§8）。

- `ChatProvider` / `EmbedProvider` 两接口为全部 provider（mock/zhipu/local_embed/预留 vLLM）的统一契约。
- 结果对象携带计量字段（tokens/latency_ms/request_id），供 ledger（T-LLM-07，M2）与 M1 期网关
  直写 `llm_calls` 留痕消费。
- embedding 维度口径 = 1024（04 §5.2 `memories.embedding vector(1024)`；开发期本地 bge-m3，00 §1 A10）。
- REPLAY 守卫不在本层：守卫唯一归门面 `worldsim.llm_gateway.LLMGateway`（04 §5.3，03 T-LLM-01），
  provider 不得自行判断回放态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# task_type 封闭枚举 9 项（04 §5.2 llm_calls DDL 注释全集 = 04 §8.1 路由表，评审 R1 §A.12；
# world_copy 不得拒绝）。门面侧以此钉死，枚举外一律拒绝。
TASK_TYPES: tuple[str, ...] = (
    "star_decision",
    "dialogue",
    "secondary",
    "bgsummary",
    "reflection",
    "director",
    "world_copy",
    "safety",
    "embed",
)

EMBED_DIM = 1024  # 04 §5.2 vector(1024)

# OpenAI 兼容消息形态：[{"role": "system"|"user"|"assistant", "content": str}, ...]
Message = dict[str, str]


@dataclass(frozen=True)
class ChatResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    request_id: str
    provider: str = ""
    model: str = ""


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    prompt_tokens: int
    latency_ms: int
    request_id: str
    provider: str = ""
    model: str = ""
    dimensions: int = EMBED_DIM


@runtime_checkable
class ChatProvider(Protocol):
    """chat 提供者协议。`seed` = 内核本 tick 规则骰子（04 §5.2 events.rng_seed 语义）。"""

    name: str

    async def chat(
        self,
        task_type: str,
        messages: list[Message],
        gen_params: dict[str, Any] | None = None,
        *,
        seed: int | None = None,
    ) -> ChatResult: ...


@runtime_checkable
class EmbedProvider(Protocol):
    """embedding 提供者协议；输出维度必须 = EMBED_DIM。"""

    name: str

    async def embed(self, texts: list[str], *, seed: int | None = None) -> EmbedResult: ...
