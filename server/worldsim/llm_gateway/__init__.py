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
import logging
import os
import time
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
    "ChainExhausted",
    "Message",
]

log = logging.getLogger(__name__)

REPLAY_ENV = "WSIM_REPLAY_MODE"

# rpm_limit 未回填期（T-LLM-12 实测回填前）路由模式的保守工程默认（03 §6 D35 登记）
DEFAULT_RPM_LIMIT = 60
# 在飞并发闸默认（模型条目未配 concurrency 时；实测口径见 T-LLM-12 回填）
DEFAULT_MAX_CONCURRENCY = 4


class ReplayViolation(RuntimeError):
    """WSIM_REPLAY_MODE=replay 下一切真实 LLM 调用被门面拦截（04 §5.3）。"""


class UnknownTaskType(ValueError):
    """task_type 不在封闭枚举 9 项内（04 §5.2/§8.1）。"""


class ProviderUnavailable(RuntimeError):
    """路由解析不到可用 provider（M1：未注册；M2 起由 router/降级链接管）。"""


class ChainExhausted(RuntimeError):
    """降级链走尽，末端特殊动作上抛（04 §8.1：pause_clock/queue_retry/pause_publish_channel）。

    消费接线：明星档 pause_clock 经裁决器接口（M3/08 接线）；本异常携带 `special` 供读取。
    """

    def __init__(self, task_type: str, special: str) -> None:
        super().__init__(f"task_type={task_type!r} 降级链走尽 → 末端动作 {special}（04 §8.1）")
        self.task_type = task_type
        self.special = special


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
        router: Any = None,
        breaker: Any = None,
        clock: Any = None,
    ) -> None:
        self._db = db
        self._cfg = models_config or {}
        self._providers: dict[str, ChatProvider | EmbedProvider] = dict(providers or {})
        self._default_provider = default_provider
        self._record_calls = record_calls
        # M2 路由模式（T-LLM-04/05/06 接线）：router 非空时 chat 走降级链（桶/退避/撞墙切换）
        self._router = router
        self._breaker = breaker
        self._clock = clock
        self._clients: dict[str, Any] = {}
        self._degraded_to: dict[str, str] = {}  # task_type → 当前降级到的 provider 别名（恢复事件 from 侧）
        from .ledger import Ledger

        self._ledger = Ledger(db, self._cfg)

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
        tick: int = 0,
    ) -> ChatResult:
        self._guard_replay()
        _check_task_type(task_type)
        if self._router is not None and provider is None:
            return await self._chat_routed(task_type, messages, gen_params, seed=seed, agent_id=agent_id, sim_time=sim_time, tick=tick)
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

    async def _chat_routed(
        self,
        task_type: str,
        messages: list[Message],
        gen_params: dict[str, Any] | None,
        *,
        seed: int | None,
        agent_id: str | None,
        sim_time: Any,
        tick: int,
    ) -> ChatResult:
        """M2 降级链执行（T-LLM-06 增量；04 §8.1/§8.2）：沿 `task_routes[task_type]` 链逐跳调用，
        撞墙（breaker 三条件）即切下一跳并落 `system.llm.failover`；链尽抛 `ChainExhausted`。
        降级期所有调用行 `llm_calls.fallback_from` = 原 provider 别名（04 §8.2 留痕口径）。"""
        gen = gen_params or self._router.gen_params(task_type)
        prompt_hash = _prompt_hash(messages)
        fallback_from: str | None = None
        saw_hop = False
        hops = self._router.chain(task_type)
        for i, hop in enumerate(hops):
            if hop.is_special:
                raise ChainExhausted(task_type, hop.special)
            impl = self._providers.get(hop.provider)
            if impl is None or not isinstance(impl, ChatProvider):
                continue  # 未注册/占位跳（enabled:false 已在 router.chain 过滤）
            if hop.model and hasattr(impl, "for_model"):
                impl = impl.for_model(hop.model, thinking=self._model_entry(hop.provider, hop.model).get("thinking"))
            saw_hop = True
            endpoint = f"{hop.provider}/{hop.model}"  # breaker 端点键（03 §6 D45）
            if self._breaker is not None and self._breaker.is_tripped(endpoint):
                fallback_from = fallback_from or hop.provider  # 降级期留痕（04 §8.2）
                if not self._breaker.due_for_probe(endpoint):
                    continue
                # 冷却 30 分钟满 → 单请求探测（04 §8.2）
                if not await self._probe(hop, impl, task_type, messages, gen, seed=seed):
                    continue
                recovered_from = self._degraded_to.pop(task_type, hop.provider)
                fallback_from = None  # 探测成功即切回：本跳即原 provider，恢复后行 fallback_from 为 NULL
                await self._breaker.emit_failover(
                    self._db, task_type=task_type, from_provider=recovered_from,
                    to_provider=hop.provider, reason="recovered",
                    tick=tick, sim_time=sim_time, rng_seed=seed or 0,
                )
            client = self._client_for(hop.provider, impl, model=hop.model)
            before = len(client.records)
            outcome = await client.call(task_type, messages, gen, seed=seed)
            attempts = client.records[before:]
            if isinstance(outcome, ChatResult):
                for rec in attempts[:-1]:  # 中间失败尝试逐条留痕（终态行由下方 ok/fallback 行承载）
                    await self._record_attempt(task_type, agent_id, hop, rec, sim_time, prompt_hash, fallback_from)
                    self._breaker_feed(hop, rec)
                self._breaker_feed_ok(hop, attempts[-1] if attempts else None)
                await self._record(
                    task_type, agent_id, outcome.provider or hop.provider, outcome.model or hop.model,
                    outcome.prompt_tokens, outcome.completion_tokens, outcome.latency_ms,
                    "fallback" if fallback_from else "ok", sim_time, prompt_hash, outcome.latency_ms,
                    request_id=outcome.request_id, fallback_from=fallback_from,
                )
                return outcome
            # 撞墙：逐尝试喂 breaker（04 §8.2 三条件窗口数据流）
            reason: str | None = None
            for rec in outcome.attempts:
                await self._record_attempt(task_type, agent_id, hop, rec, sim_time, prompt_hash, fallback_from)
                r = self._breaker_feed(hop, rec)
                reason = reason or r
            fallback_from = fallback_from or hop.provider
            nxt = next((h for h in hops[i + 1:] if not h.is_special), None)
            if reason and self._breaker is not None:
                self._degraded_to[task_type] = nxt.provider if nxt else "chain_end"
                await self._breaker.emit_failover(
                    self._db, task_type=task_type, from_provider=hop.provider,
                    to_provider=nxt.provider if nxt else "chain_end", reason=reason,
                    tick=tick, sim_time=sim_time, rng_seed=seed or 0,
                )
        if not saw_hop and self._default_provider:
            impl = self._providers.get(self._default_provider)
            if impl is not None:
                result = await impl.chat(task_type, messages, gen, seed=seed)  # type: ignore[union-attr]
                await self._record(
                    task_type, agent_id, result.provider or self._default_provider, result.model or "unknown",
                    result.prompt_tokens, result.completion_tokens, result.latency_ms, "ok", sim_time,
                    prompt_hash, result.latency_ms, request_id=result.request_id,
                )
                return result
        raise ProviderUnavailable(f"task_type={task_type!r} 降级链无可用 provider（{[h.provider or h.special for h in hops]}）")

    async def _probe(self, hop: Any, impl: Any, task_type: str, messages: list[Message], gen: dict[str, Any], *, seed: int | None) -> bool:
        """单请求探测（04 §8.2 冷却恢复）；结果回报 breaker，探测行照常留痕。"""
        started = time.perf_counter()
        endpoint = f"{hop.provider}/{hop.model}"
        try:
            await impl.chat(task_type, messages, gen, seed=seed)
        except Exception:
            self._breaker.probe_result(endpoint, ok=False)
            return False
        self._breaker.probe_result(endpoint, ok=True)
        log.info("LLM 降级恢复探测成功：%s（冷却满单请求探测，04 §8.2）；探测耗时 %dms", hop.provider, int((time.perf_counter() - started) * 1000))
        return True

    def _model_entry(self, provider: str, model: str) -> dict[str, Any]:
        if self._router:
            pcfg = self._router.provider_config(provider)
            for section in ("chat", "embedding"):
                for m in pcfg.get(section) or []:
                    if m.get("id") == model:
                        return dict(m)
        return {}

    def _model_limits(self, provider: str, model: str) -> tuple[int, int, int | None]:
        """(rpm_limit, concurrency, p95_trip_ms)：优先读模型条目（providers.<alias>.chat[]/embedding[]），
        缺省回落 provider 级 rpm_limit，再缺省工程默认（03 §6 D35/D41/D42）。"""
        rpm: int | None = None
        conc: int | None = None
        p95: int | None = None
        if self._router:
            pcfg = self._router.provider_config(provider)
            for section in ("chat", "embedding"):
                for m in pcfg.get(section) or []:
                    if m.get("id") == model:
                        rpm = m.get("rpm_limit") or rpm
                        conc = m.get("concurrency") or conc
                        p95 = m.get("p95_trip_ms") or p95
            rpm = rpm or pcfg.get("rpm_limit")
        return rpm or DEFAULT_RPM_LIMIT, conc or DEFAULT_MAX_CONCURRENCY, p95

    def _client_for(self, provider: str, impl: Any, *, model: str = "") -> Any:
        """每 (provider, model) 一个 RateLimitedClient（令牌桶容量 = models.yaml rpm_limit，04 §8.5）。"""
        from .clients import RateLimitedClient

        key = f"{provider}/{model}"
        client = self._clients.get(key)
        if client is None:
            rpm, conc, _ = self._model_limits(provider, model)
            client = RateLimitedClient(impl, rpm_limit=rpm, clock=self._clock, max_concurrency=conc)
            self._clients[key] = client
        return client

    def _breaker_feed(self, hop: Any, rec: Any) -> str | None:
        if self._breaker is None:
            return None
        kind = "ok" if rec.status == "ok" else ("429" if rec.retry_after_s is not None or "429" in (rec.error or "") else "5xx")
        rpm, _, p95 = self._model_limits(hop.provider, hop.model)
        return self._breaker.record_attempt(
            f"{hop.provider}/{hop.model}", kind=kind, latency_ms=rec.latency_ms, rpm_limit=rpm, p95_trip_ms=p95,
        )

    def _breaker_feed_ok(self, hop: Any, rec: Any) -> None:
        if self._breaker is not None:
            self._breaker.record_attempt(f"{hop.provider}/{hop.model}", kind="ok", latency_ms=rec.latency_ms if rec else 0)

    async def _record_attempt(self, task_type: str, agent_id: str | None, hop: Any, rec: Any, sim_time: Any, prompt_hash: str, fallback_from: str | None) -> None:
        """中间失败尝试留痕（04 §8.3 全量记录含重试；tokens 未知记 0，成本 0）。"""
        await self._record(
            task_type, agent_id, hop.provider, hop.model or "unknown",
            0, 0, rec.latency_ms, rec.status, sim_time, prompt_hash, rec.latency_ms,
            fallback_from=fallback_from,
        )

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
        fallback_from: str | None = None,
    ) -> None:
        """全量计量落库（T-LLM-07 ledger 接管：成本按 models.yaml 价格表算，未回填/mock/本地记 0；
        replay 模式在守卫层已抛，绝不会走到这里）。"""
        if not self._record_calls or self._db is None:
            return
        await self._ledger.record(
            task_type=task_type, agent_id=agent_id, provider=provider, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            latency_ms=latency_ms or latency_fallback_ms, status=status,
            sim_time=sim_time, prompt_hash=prompt_hash,
            fallback_from=fallback_from, request_id=request_id,
        )
