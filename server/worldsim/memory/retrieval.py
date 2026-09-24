"""配额制记忆检索（02 T-MEM-01；04 §7.1 三桶 SQL / 04 §5.2 ef_search / 04 §7.3 归档排除）。

top-k = 近期 N + 高重要性 M + 语义相关 K，分桶取后合并去重（不用混合排序，04 §7.1）：

- SQL 按 04 §7.1 三桶 CTE 口径实现：recent（sim_time DESC LIMIT N）/ important（近 7 模拟日，
  importance DESC, sim_time DESC LIMIT M）/ semantic（排除已选，`embedding <=> $vec` LIMIT K）；
  归档集（`archived=true`）不进检索（04 §7.3）。
- 配额 N/M/K 默认 10/5/10（04 §7.1），读 models.yaml `thresholds.retrieval_quota` 可调。
- 语义桶 embedding M1 由 mock 供数（02 文档 D1）；查询侧 `SET LOCAL hnsw.ef_search = 40`（04 §5.2）。
- `make_retrieve_hook` 产出管道 step2 `retrieve=` 挂接签名（T-ADJ-02：`async (agent_id, obs) -> list`）；
  一切时间过滤只读 sim_time（00 §4 红线 11）。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_QUOTA = {"N": 10, "M": 5, "K": 10}  # 04 §7.1 默认值（models.yaml thresholds.retrieval_quota 可调）
DEFAULT_EF_SEARCH = 40                      # 查询侧 ef_search（04 §5.2 mem_embed_hnsw 注释）

_QUOTA_SQL = """
WITH recent AS (
  SELECT id, 1 AS bucket FROM memories
  WHERE agent_id=$1 AND NOT archived ORDER BY sim_time DESC LIMIT $4),
important AS (
  SELECT id, 2 AS bucket FROM memories
  WHERE agent_id=$1 AND NOT archived AND sim_time > $3::timestamptz - interval '7 days'
  ORDER BY importance DESC, sim_time DESC LIMIT $5),
semantic AS (
  SELECT id, 3 AS bucket FROM memories
  WHERE agent_id=$1 AND NOT archived
    AND id NOT IN (SELECT id FROM recent UNION SELECT id FROM important)
  ORDER BY embedding <=> $2::vector LIMIT $6)
SELECT m.id, m.agent_id, m.kind, m.content, m.importance, m.sim_time, m.is_witness, q.bucket
FROM memories m
JOIN (SELECT DISTINCT ON (id) id, bucket FROM
      (SELECT * FROM recent UNION ALL SELECT * FROM important UNION ALL SELECT * FROM semantic) t
     ) q USING (id)
ORDER BY q.bucket, m.sim_time DESC;
"""


async def quota_retrieve(
    pool: Any,
    gateway: Any,
    *,
    agent_id: str,
    query_text: str,
    sim_now: dt.datetime,
    quota: dict[str, int] | None = None,
    rng_seed: int = 0,
    ef_search: int = DEFAULT_EF_SEARCH,
) -> list[dict[str, Any]]:
    """三桶配额检索。返回 [{id, kind, content, importance, sim_time, is_witness, bucket}]（bucket 升序、桶内 sim_time 降序）。"""
    q = {**DEFAULT_QUOTA, **(quota or {})}
    vec = await gateway.embed([query_text], seed=rng_seed, agent_id=agent_id, sim_time=sim_now)
    vec_literal = "[" + ",".join(repr(v) for v in vec.vectors[0]) + "]"
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")  # 04 §5.2 查询侧口径
            rows = await conn.fetch(
                _QUOTA_SQL, agent_id, vec_literal, sim_now,
                int(q["N"]), int(q["M"]), int(q["K"]),
            )
    return [dict(r) for r in rows]


def make_retrieve_hook(
    pool: Any,
    gateway: Any,
    models_cfg: dict[str, Any] | None = None,
    *,
    ef_search: int = DEFAULT_EF_SEARCH,
) -> Any:
    """管道 step2 `retrieve=` 挂接（T-ADJ-02）：`async (agent_id, obs) -> list[str]`（记忆 content 列表）。

    配额读 models.yaml `thresholds.retrieval_quota`（04 §7.1 可调口径）；语义桶查询文本 = 感知摘要
    （位置 + 在场者 + 活跃目标；M1 轻量口径，prompt 模板族归 03 T-LLM-09）。
    """
    quota = dict((models_cfg or {}).get("thresholds", {}).get("retrieval_quota", DEFAULT_QUOTA))

    async def retrieve(agent_id: str, obs: Any) -> list[str]:
        query_text = f"{obs.position} {' '.join(obs.co_located)} {' '.join(obs.goals[:3])}"
        rows = await quota_retrieve(
            pool, gateway, agent_id=agent_id, query_text=query_text,
            sim_now=obs.sim_time, quota=quota, rng_seed=int(obs.sim_time.timestamp()),
        )
        return [r["content"] for r in rows]

    return retrieve
