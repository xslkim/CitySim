"""记忆膨胀治理（02 T-MEM-03；04 §7.3 三行表 / 04 §2.2 hygiene_loop）。

三措施逐行按 04 §7.3 表：

- **摘要合并**（每模拟日，凌晨 batch 段，经 T-TIME-03 回调注册表挂载，注册名 `memory.merge`）：
  7 模拟天前、`kind='event'`、`importance ≤4` 的连续记忆由次要层模型合并为一条 `summary`
  （M1 合并模型用 mock bgsummary，02 文档 D1），原条目 `archived=true`（不删行，可回溯）；
  不足 2 条不合并（单条无"合并"语义，工程口径 D19）。
- **归档**（每模拟日）：`importance ≤3` 且 30 模拟天前 → `archived=true`；归档集不进配额检索。
- **索引重建**（每周日凌晨真实，运维排程归 08）：`REINDEX INDEX CONCURRENTLY mem_embed_hnsw`；
  提前重建触发 = 行数 >100 万或召回抽检命中率 <85%（04 §7.3；命中率由调用方注入）。

UPDATE 权限：Schema v1 下 memories 应用角色仅 INSERT；本任务以 `ddl/memories_update_grant.sql`
按列最小授权 `GRANT UPDATE (archived)`（02 文档偏差表 D13 登记；合并/归档只置位、永不 DELETE）。

`hygiene_loop()` 协程挂入主循环（04 §2.2 伪码）：日界翻转触发归档；合并只在 batch 钩子。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any

from ..time_engine.batch_hooks import BatchContext, register_batch_hook
from .store import insert_memory

log = logging.getLogger(__name__)

MERGE_AGE_DAYS = 7                 # 摘要合并：7 模拟天前（04 §7.3）
MERGE_MAX_IMPORTANCE = 4           # 摘要合并：importance ≤4、kind='event'
SUMMARY_IMPORTANCE = 4             # 合并产物 summary 重要性（与 main.py bgsummary 摘要重要性同档，D19）
ARCHIVE_AGE_DAYS = 30              # 归档：30 模拟天前
ARCHIVE_MAX_IMPORTANCE = 3         # 归档：importance ≤3
REINDEX_ROW_THRESHOLD = 1_000_000  # 提前重建：行数 >100 万（04 §7.3）
REINDEX_RECALL_MIN = 0.85          # 提前重建：召回抽检命中率 <85%（采样 50 条已知相关对，04 §7.3）
MERGE_BATCH_HOOK = "memory.merge"  # batch 钩子注册名（唯一挂载点，02 T-TIME-03）


class MemoryHygiene:
    """膨胀治理引擎。`pool` = asyncpg pool；`gateway` = LLM 网关（合并模型，M1 mock 注入）。"""

    def __init__(self, pool: Any, gateway: Any = None) -> None:
        self._pool = pool
        self._gw = gateway

    # ---- 摘要合并（凌晨 batch 段） -------------------------------------------------

    async def merge_summaries(
        self, *, sim_now: dt.datetime, agent_ids: list[str] | tuple[str, ...] | None = None,
        rng_seed: int = 0,
    ) -> dict[str, int]:
        """合并 7 模拟天前低重要性事件记忆。返回 {agent_id: 被合并条数}（不足 2 条不合并）。"""
        cutoff = sim_now - dt.timedelta(days=MERGE_AGE_DAYS)
        rows = await self._pool.fetch(
            """
            SELECT id, agent_id, content, sim_time FROM memories
            WHERE kind='event' AND importance <= $1 AND NOT archived AND sim_time < $2
              AND ($3::text[] IS NULL OR agent_id = ANY($3::text[]))
            ORDER BY agent_id, sim_time
            """,
            MERGE_MAX_IMPORTANCE, cutoff, list(agent_ids) if agent_ids is not None else None,
        )
        by_agent: dict[str, list[Any]] = {}
        for r in rows:
            by_agent.setdefault(r["agent_id"], []).append(r)
        merged: dict[str, int] = {}
        for aid, mems in by_agent.items():
            if len(mems) < 2:
                continue
            ids = [int(m["id"]) for m in mems]
            digest = await self._summarize(aid, mems, sim_now, rng_seed)
            latest = max(m["sim_time"] for m in mems)
            if self._gw is not None:
                await insert_memory(
                    self._pool, self._gw, agent_id=aid, sim_time=latest, kind="summary",
                    content=digest, importance=SUMMARY_IMPORTANCE, rng_seed=rng_seed,
                )
            else:  # 无网关（纯 SQL 场景）：确定性拼接摘要，不产 embedding
                await self._pool.execute(
                    """
                    INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance)
                    VALUES ($1, $2, 'summary', $3, $3, $4)
                    """,
                    aid, latest, digest, SUMMARY_IMPORTANCE,
                )
            await self._pool.execute("UPDATE memories SET archived=true WHERE id = ANY($1::bigint[])", ids)
            merged[aid] = len(ids)
            log.info("摘要合并：%s %d 条 → 1 条 summary（原条目置归档）", aid, len(ids))
        return merged

    async def _summarize(self, agent_id: str, mems: list[Any], sim_now: dt.datetime, rng_seed: int) -> str:
        lines = "\n".join(f"- {m['content']}" for m in mems)
        if self._gw is None:
            return f"早前的经历（{len(mems)} 条合并）：" + mems[0]["content"][:40] + "……"
        from ..llm_gateway import ChainExhausted, ProviderUnavailable

        try:
            result = await self._gw.chat(
                "bgsummary",
                [
                    {"role": "system", "content": "把这段较早的经历压缩成一条第一人称摘要（≤60字）。输出 JSON。"},
                    {"role": "user", "content": f"较早的经历：\n{lines}"},
                ],
                self._gw.gen_params("bgsummary") if hasattr(self._gw, "gen_params") else None,
                seed=rng_seed, agent_id=agent_id, sim_time=sim_now,
            )
        except (ChainExhausted, ProviderUnavailable) as exc:
            # M2 真接入崩溃护栏（03 §6 D45）：LLM 链路不可用 → 退化为确定性合并摘要，不拖垮 hygiene_loop
            log.error("摘要合并 LLM 链路不可用（%s）→ 退化确定性摘要", exc)
            return f"早前的经历（{len(mems)} 条合并）：" + mems[0]["content"][:40] + "……"
        try:
            body = json.loads(result.text)
            return str(body.get("diary") or result.text)[:120]
        except (ValueError, TypeError):
            return result.text[:120]

    # ---- 归档（每模拟日） ---------------------------------------------------------

    async def archive_old(self, *, sim_now: dt.datetime) -> int:
        """importance ≤3 且 30 模拟天前 → archived=true（只置位不删行）。返回置位行数。"""
        cutoff = sim_now - dt.timedelta(days=ARCHIVE_AGE_DAYS)
        return await self._pool.fetchval(
            """
            WITH u AS (UPDATE memories SET archived=true
                       WHERE NOT archived AND importance <= $1 AND sim_time < $2 RETURNING id)
            SELECT count(*) FROM u
            """,
            ARCHIVE_MAX_IMPORTANCE, cutoff,
        )

    # ---- 索引重建（每周日凌晨真实；提前重建触发） -------------------------------------

    async def maybe_reindex(self, *, force: bool = False, recall_hit_rate: float | None = None) -> bool:
        """触发条件满足时 REINDEX INDEX CONCURRENTLY mem_embed_hnsw。返回是否执行。

        提前重建触发（04 §7.3）：行数 >100 万，或召回抽检命中率 <85%（由调用方注入）。
        周常重建（每周日凌晨真实）由运维排程以 `force=True` 调用（归 08 文档）。
        """
        rows = await self._pool.fetchval("SELECT count(*) FROM memories")
        trigger = force or rows > REINDEX_ROW_THRESHOLD or (
            recall_hit_rate is not None and recall_hit_rate < REINDEX_RECALL_MIN
        )
        if not trigger:
            return False
        log.warning("mem_embed_hnsw 重建（行数=%d force=%s recall=%s）", rows, force, recall_hit_rate)
        async with self._pool.acquire() as conn:  # REINDEX CONCURRENTLY 不可入事务；asyncpg 连接默认 autocommit
            await conn.execute("REINDEX INDEX CONCURRENTLY mem_embed_hnsw")
        return True

    # ---- 挂载点 ---------------------------------------------------------------------

    def batch_hook(self) -> Any:
        """batch 段钩子（摘要合并；经 T-TIME-03 回调注册表挂载，唯一挂载点）。"""

        async def _hook(ctx: BatchContext) -> None:
            await self.merge_summaries(
                sim_now=ctx.clock.now_sim(), agent_ids=ctx.agent_ids, rng_seed=int(ctx.sim_hours),
            )

        return _hook

    def register_batch_hook(self) -> None:
        register_batch_hook(MERGE_BATCH_HOOK, self.batch_hook())

    async def hygiene_loop(self, *, clock: Any, stop: asyncio.Event, interval: float = 30.0) -> None:
        """治理协程（04 §2.2 主循环挂接）：日界翻转（sim 日）触发归档；停即止。"""
        last_day = clock.now_sim().date()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except TimeoutError:
                pass
            today = clock.now_sim().date()
            if today != last_day:
                n = await self.archive_old(sim_now=clock.now_sim())
                if n:
                    log.info("日界归档：%d 条记忆置归档", n)
                last_day = today
        log.info("hygiene_loop 退出（stop）")
