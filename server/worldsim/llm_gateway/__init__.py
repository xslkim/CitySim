"""LLM 网关门面（03 文档 T-LLM-01，M1 段交付；04 §2.1/§2.2/§5.3）。

职责边界（M1 段）：
- `LLMGateway` 门面：task_type 封闭枚举校验（9 项含 `world_copy`，枚举外拒绝）、provider 分发、
  `llm_calls` 留痕（M1 期 mock 记 provider='mock'、cost 0；T-LLM-07 M2 接管全量计量与开关）。
- **REPLAY 守卫唯一归本门面**（04 §5.3"LLM 网关拒绝一切真实调用"）：`WSIM_REPLAY_MODE=replay`
  时 `chat`/`embed` 一律抛 `ReplayViolation`，provider 层不可绕过（守卫先于任何 provider 调用）。
- 依赖方向单向（04 §2.1）：本包不得反向依赖内核上层包（scheduler/adjudicator/world_agent）。

构造签名对齐 04 §2.2 伪码 `LLMGateway(db, load(WSIM_MODELS_CONFIG))`；M1 内核经
`providers=`/`default_provider=` 注入 mock（注入 seam，T-LLM-04 router M2 接管完整路由后不变）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any

from .providers.base import (
    TASK_TYPES,
    ChatProvider,
    ChatResult,
    EmbedProvider,
    EmbedResult,
    Message,
)

__all__ = [
    "TASK_TYPES",
    "ChatResult",
    "EmbedResult",
    "EmbedProvider",
    "ChatProvider",
    "LLMGateway",
    "ReplayViolation",
    "UnknownTaskType",
    "ProviderUnavailable",
    "Message",
]

log = logging.getLogger(__name__)

REPLAY_ENV = "WSIM_REPLAY_MODE"


class ReplayViolation(RuntimeError):
    """WSIM_REPLAY_MODE=replay 下一切真实 LLM 调用被门面拦截（04 §5.3）。"""


class UnknownTaskType(ValueError):
    """task_type 不在封闭枚举 9 项内（04 §5.2/§8.1）。"""


class ProviderUnavailable(RuntimeError):
    """路由解析不到可用 provider（M1：未注册；M2 起由 router/降级链接管）。"""


def _check_task_type(task_type: str) -> None:
    if task_type not in TASK_TYPES:
        raise UnknownTaskType(f"task_type {task_type!r} 不在封闭枚举内（{', '.join(TASK_TYPES)}）")


def _prompt_hash(messages: list[Message]) -> str:
    """渲染后完整 prompt 的 SHA-256 hex（04 §11.2：只记 hash 不记原文；算法为工程默认，03 §6 D4）。"""
    h = hashlib.sha256()
    for m in messages:
        h.update(m.get("role", "").encode("utf-8"))
        h.update(b"\x00")
        h.update(m.get("content", "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class LLMGateway:
    """网关门面。`db` 为 asyncpg pool（None 时跳过 llm_calls 留痕，供纯逻辑单测）。

    provider 解析序：调用侧显式 `provider=` → 路由表 `task_routes[task_type].primary.provider`
    （已注册才可用）→ 构造期 `default_provider`。M2 router（T-LLM-04）接管降级链后解析序不变。
    """

    def __init__(
        self,
        db: Any,
        models_config: dict[str, Any],
        *,
        providers: dict[str, ChatProvider | EmbedProvider] | None = None,
        default_provider: str | None = None,
        record_calls: bool = True,
    ) -> None:
        self._db = db
        self._cfg = models_config or {}
        self._providers: dict[str, ChatProvider | EmbedProvider] = dict(providers or {})
        self._default_provider = default_provider
        self._record_calls = record_calls

    # ---- 装配 -----------------------------------------------------------

    def register_provider(self, name: str, provider: ChatProvider | EmbedProvider) -> None:
        self._providers[name] = provider

    def gen_params(self, task_type: str) -> dict[str, Any]:
        _check_task_type(task_type)
        return dict(self._cfg.get("generation_params", {}).get(task_type, {}))

    # ---- 守卫 -----------------------------------------------------------

    @staticmethod
    def _guard_replay() -> None:
        if os.environ.get(REPLAY_ENV, "off") == "replay":
            raise ReplayViolation(
                f"{REPLAY_ENV}=replay：LLM 网关拒绝一切真实调用（04 §5.3；重放从事件流取数，不得重算世界）"
            )

    # ---- 调用 -----------------------------------------------------------

    async def chat(
        self,
        task_type: str,
        messages: list[Message],
        gen_params: dict[str, Any] | None = None,
        *,
        provider: str | None = None,
        seed: int | None = None,
        agent_id: str | None = None,
        sim_time: Any = None,
    ) -> ChatResult:
        self._guard_replay()
        _check_task_type(task_type)
        name, impl = self._resolve(task_type, provider, want="chat")
        started = time.perf_counter()
        try:
            result = await impl.chat(task_type, messages, gen_params or self.gen_params(task_type), seed=seed)  # type: ignore[union-attr]
        except Exception:
            await self._record(
                task_type, agent_id, name, "unknown", 0, 0, 0, "failed", sim_time,
                _prompt_hash(messages), int((time.perf_counter() - started) * 1000),
            )
            raise
        await self._record(
            task_type, agent_id, result.provider or name, result.model or "unknown",
            result.prompt_tokens, result.completion_tokens, result.latency_ms, "ok", sim_time,
            _prompt_hash(messages), result.latency_ms if result.latency_ms else int((time.perf_counter() - started) * 1000),
            request_id=result.request_id,
        )
        return result

    async def embed(
        self,
        texts: list[str],
        *,
        provider: str | None = None,
        seed: int | None = None,
        agent_id: str | None = None,
        sim_time: Any = None,
    ) -> EmbedResult:
        self._guard_replay()
        name, impl = self._resolve("embed", provider, want="embed")
        started = time.perf_counter()
        try:
            result = await impl.embed(texts, seed=seed)  # type: ignore[union-attr]
        except Exception:
            await self._record(
                "embed", agent_id, name, "unknown", 0, 0, 0, "failed", sim_time,
                _prompt_hash([{"role": "user", "content": t} for t in texts]),
                int((time.perf_counter() - started) * 1000),
            )
            raise
        await self._record(
            "embed", agent_id, result.provider or name, result.model or "unknown",
            result.prompt_tokens, 0, result.latency_ms, "ok", sim_time,
            _prompt_hash([{"role": "user", "content": t} for t in texts]),
            result.latency_ms if result.latency_ms else int((time.perf_counter() - started) * 1000),
            request_id=result.request_id,
        )
        return result

    # ---- 内部 -----------------------------------------------------------

    def _resolve(self, task_type: str, provider: str | None, *, want: str) -> tuple[str, Any]:
        candidates: list[str] = []
        if provider is not None:
            candidates.append(provider)
        else:
            route = self._cfg.get("task_routes", {}).get(task_type, {})
            primary = (route.get("primary") or {}).get("provider")
            if primary:
                candidates.append(primary)
            if self._default_provider:
                candidates.append(self._default_provider)
        for name in candidates:
            impl = self._providers.get(name)
            if impl is None:
                continue
            if want == "chat" and isinstance(impl, ChatProvider):
                return name, impl
            if want == "embed" and isinstance(impl, EmbedProvider):
                return name, impl
        raise ProviderUnavailable(
            f"task_type={task_type!r} 无可用 {want} provider（候选 {candidates or '∅'}；M1 请注入 mock）"
        )

    async def _record(
        self,
        task_type: str,
        agent_id: str | None,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        status: str,
        sim_time: Any,
        prompt_hash: str,
        latency_fallback_ms: int,
        request_id: str | None = None,
    ) -> None:
        """M1 期直写 llm_calls（mock 记 cost 0；replay 模式在守卫层已抛，绝不会走到这里）。

        T-LLM-07（M2）接管全量计量与写库开关后本方法收敛为其客户端；成本单价表未回填前一律 0。
        """
        if not self._record_calls or self._db is None:
            return
        try:
            await self._db.execute(
                """
                INSERT INTO llm_calls
                    (sim_time, task_type, agent_id, provider, model,
                     prompt_tokens, completion_tokens, cost_micro_cny,
                     latency_ms, status, request_id, prompt_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, $9, $10, $11)
                """,
                sim_time, task_type, agent_id, provider, model,
                prompt_tokens, completion_tokens,
                latency_ms or latency_fallback_ms, status,
                request_id or uuid.uuid4().hex, prompt_hash,
            )
        except Exception:  # 留痕失败不阻断主调用（计量归账问题走日志，04 §12.1）
            log.warning("llm_calls 留痕写入失败（task_type=%s provider=%s）", task_type, provider, exc_info=True)
