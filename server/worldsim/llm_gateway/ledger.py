"""ledger 计量：`llm_calls` 全量记录与 ¥/模拟日聚合（03 文档 T-LLM-07；04 §8.3/§5.2/§11.2/§12.1）。

- 每次调用（含失败与重试）落一行；列逐一映射 04 §5.2 DDL。
- 成本公式（04 §8.3）：`cost_micro_cny = prompt_tokens×in_price + completion_tokens×out_price`，
  单价读 models.yaml `providers:` 段（**单位 = ¥/百万 tokens**，微元整数运算防浮点漂移：
  tokens/1e6 × ¥/Mtok × 1e6 微元 = tokens × 单价，整数化 round）；
  mock/本地 provider 记 0 但记耗时（04 §8.3 归账口径）；单价未回填（null）一律记 0。
- `prompt_hash` = 渲染后完整 prompt 的 SHA-256 hex（工程默认，03 §6 D4）；prompt 原文只进 hash，
  不落库不落日志（04 §11.2/§12.1）。
- 写库串行：`record()` 由裁决协程调用（"LLM 并发、落库串行"，00 §4 红线 10）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

log = logging.getLogger(__name__)

PriceLookup = Callable[[str, str], tuple[float | None, float | None]]


def price_of(models_cfg: dict[str, Any], provider: str, model: str) -> tuple[float | None, float | None]:
    """(in_price, out_price) ¥/百万 tokens；未回填（null/缺条目）→ (None, None) 记 0。"""
    pcfg = (models_cfg.get("providers") or {}).get(provider) or {}
    for section in ("chat", "embedding"):
        for m in pcfg.get(section) or []:
            if m.get("id") == model:
                return m.get("input_price"), m.get("output_price")
    if "input_price" in pcfg:
        return pcfg.get("input_price"), None
    return None, None


def cost_micro_cny(
    prompt_tokens: int,
    completion_tokens: int,
    in_price: float | None,
    out_price: float | None,
) -> int:
    """04 §8.3 公式；单价 ¥/百万 tokens → 微元整数（tokens × 单价，round 防浮点漂移）。"""
    return round(prompt_tokens * (in_price or 0) + completion_tokens * (out_price or 0))


class Ledger:
    """llm_calls 写库器（全量计量唯一写入口；gateway 门面/降级链共用）。"""

    def __init__(self, db: Any, models_cfg: dict[str, Any] | None = None) -> None:
        self._db = db
        self._cfg = models_cfg or {}

    async def record(
        self,
        *,
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
        fallback_from: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """落一行 llm_calls；留痕失败不阻断主调用（WARN 走日志，04 §12.1）。"""
        if self._db is None:
            return
        in_price, out_price = price_of(self._cfg, provider, model)
        cost = cost_micro_cny(prompt_tokens, completion_tokens, in_price, out_price)
        try:
            await self._db.execute(
                """
                INSERT INTO llm_calls
                    (sim_time, task_type, agent_id, provider, model,
                     prompt_tokens, completion_tokens, cost_micro_cny,
                     latency_ms, status, fallback_from, request_id, prompt_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                sim_time, task_type, agent_id, provider, model,
                prompt_tokens, completion_tokens, cost,
                latency_ms, status, fallback_from,
                request_id or uuid.uuid4().hex, prompt_hash,
            )
        except Exception:
            log.warning("llm_calls 留痕写入失败（task_type=%s provider=%s）", task_type, provider, exc_info=True)


async def cny_per_simday(db: Any) -> float:
    """¥/模拟日（04 §8.3）：SUM(cost_micro_cny)/1e6 ÷ COUNT(DISTINCT date(sim_time))。"""
    row = await db.fetchrow(
        """
        SELECT coalesce(SUM(cost_micro_cny), 0)::float / 1e6
               / nullif(COUNT(DISTINCT date(sim_time)), 0) AS v
        FROM llm_calls WHERE sim_time IS NOT NULL
        """
    )
    return float(row["v"]) if row and row["v"] is not None else 0.0


async def cny_per_simday_rolling(db: Any, *, sim_days: int = 7) -> float:
    """7 模拟日滚动平滑变体（04 §8.3）：最近 sim_days 个模拟日窗口同公式。"""
    row = await db.fetchrow(
        """
        SELECT coalesce(SUM(cost_micro_cny), 0)::float / 1e6
               / nullif(COUNT(DISTINCT date(sim_time)), 0) AS v
        FROM llm_calls
        WHERE sim_time IS NOT NULL
          AND date(sim_time) > (SELECT max(date(sim_time)) FROM llm_calls) - ($1 || ' days')::interval
        """
        , str(sim_days),
    )
    return float(row["v"]) if row and row["v"] is not None else 0.0
