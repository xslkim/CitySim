"""refresh_health_daily（07 T-SYN-08；05 §3.5/§4.2）：副本侧健康度聚合复算。

- 五指标按 04 §10.2 口径在副本 `events` 白名单 + `relation_change_log` 上复算；与 08 T-AUD-08
  `audit/metrics.py`（主库唯一权威实现）同数据集对拍（R1 §A.4，tests/derived/test_health_daily.py）：
  事件侧口径逐字复用（type_entropy/appearance_gini/ngram_dup/intervention_rate 直接 import），
  A 级间隔读 `event_grade_view`（05 §3.5），关系周变化率读 `relation_change_log`。
- 结构两指标（01 §9/§11.2）：`high_tension_ratio`/`active_conflict_edges` 从 `relation_daily`
  （当日定稿矩阵）+ 近 7 模拟日流水派生；**gossip 取 teller→about 边**（写死）。
- `cost_micro_cny` 取快照 `state.health.cost_daily_micro_cny`（缺席 → NULL 不报错）。
- 阈值不入库（05 §3.5 脱敏规则；阈值唯一持有方 01 §9，本模块代码零字面阈值数字）。
- 幂等：按 sim_day `INSERT … ON CONFLICT DO UPDATE`（05 §4.2 追平后自动覆盖重算）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from ...audit.metrics import (  # 口径同源复用（R1 §A.4 对拍基线 = 08 T-AUD-08 唯一权威实现）
    CONFLICT_TENSION_MIN,
    CONFLICT_TYPES,
    GOSSIP_TYPE,
    HIGH_TENSION_MIN,
    REL_CHANGE_AFFINITY_MIN,
    appearance_gini,
    ngram_dup,
    type_entropy,
)
from ...world_agent.director.intervene import intervention_rate_7d

log = logging.getLogger(__name__)

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


async def a_grade_gap_days(conn: Any, sim_now: dt.datetime) -> float:
    """A 级事件间隔：读 event_grade_view 最新生效 grade（05 §3.5/§3.8；主库侧等价物 = 04 §10.2 有效 grade）。"""
    rows = await conn.fetch(
        """
        SELECT e.sim_time FROM events e JOIN event_grade_view g ON g.seq = e.seq
        WHERE g.grade='A' AND e.sim_time > $1::timestamptz - interval '14 days'
        ORDER BY e.sim_time
        """, sim_now)
    if len(rows) < 2:
        return 7.0  # 与主库侧同口径：窗口内不足两个 A 级按窗长劣化计
    gaps = [(b["sim_time"] - a["sim_time"]).total_seconds() / 86400.0
            for a, b in zip(rows, rows[1:])]
    return round(sum(gaps) / len(gaps), 4)


async def relation_week_change(conn: Any, sim_now: dt.datetime) -> float:
    """关系图周变化率：relation_change_log 近 7 模拟日 |Δaffinity|≥3 或 label 变更边 / 活跃边。"""
    rows = await conn.fetch(
        """
        SELECT a_id, b_id, max(abs(delta_affinity))::int AS max_d,
               bool_or(labels_added IS NOT NULL OR labels_removed IS NOT NULL) AS label_chg
        FROM relation_change_log
        WHERE sim_time > $1::timestamptz - interval '7 days'
        GROUP BY a_id, b_id
        """, sim_now)
    active = len(rows)
    if active == 0:
        return 0.0
    changed = sum(1 for r in rows if int(r["max_d"]) >= REL_CHANGE_AFFINITY_MIN or r["label_chg"])
    return round(changed / active, 4)


async def structural_metrics(conn: Any, day: dt.date, sim_now: dt.datetime) -> tuple[float, int]:
    """高张力边占比 + 活跃冲突边数（01 §9/§11.2；数据源 relation_daily 当日定稿 + 近 7 日流水）。"""
    total = await conn.fetchval("SELECT count(*) FROM relation_daily WHERE sim_day=$1", day)
    high = await conn.fetchval(
        "SELECT count(*) FROM relation_daily WHERE sim_day=$1 AND tension > $2",
        day, HIGH_TENSION_MIN)
    ratio = round(int(high) / int(total), 4) if total else 0.0
    edges: set[tuple[str, str]] = set()
    for r in await conn.fetch(
            "SELECT a_id, b_id FROM relation_daily WHERE sim_day=$1 AND tension >= $2",
            day, CONFLICT_TENSION_MIN):
        edges.add((r["a_id"], r["b_id"]))
    for r in await conn.fetch(
            """
            SELECT CASE e.type WHEN 'dialogue.gossip' THEN e.payload->>'teller'
                               ELSE e.payload->>'from' END AS a_id,
                   CASE e.type WHEN 'dialogue.gossip' THEN e.payload->>'about'
                               WHEN 'social.appointment.stood_up' THEN e.payload->'participants'->>0
                               ELSE e.payload->>'to' END AS b_id
            FROM events e
            WHERE e.sim_time > $1::timestamptz - interval '7 days' AND e.type = ANY($2::text[])
            """, sim_now, list(CONFLICT_TYPES) + [GOSSIP_TYPE]):
        if r["a_id"] and r["b_id"]:
            edges.add((r["a_id"], r["b_id"]))
    return ratio, len(edges)


async def cost_from_snapshot(conn: Any, day: dt.date) -> int | None:
    """成本列 = 快照 state.health.cost_daily_micro_cny（缺席 → None，05 §3.5）。"""
    row = await conn.fetchrow(
        "SELECT state FROM world_state_snapshot WHERE sim_day=$1", day)
    if row is None:
        return None
    state = row["state"]
    if isinstance(state, str):
        state = json.loads(state)
    v = (state.get("health") or {}).get("cost_daily_micro_cny")
    return int(v) if v is not None else None


async def compute_day(conn: Any, day: dt.date) -> dict[str, Any]:
    """计算 sim_day=day 全部列（不落库）；窗口右端 = 次日 00:00 本地（与主库侧 sim_now 口径一致）。"""
    if isinstance(day, str):
        day = dt.date.fromisoformat(day)
    sim_now = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tzinfo=LOCAL_TZ)
    ratio, conflict_edges = await structural_metrics(conn, day, sim_now)
    return {
        "a_grade_gap_days": await a_grade_gap_days(conn, sim_now),
        "type_entropy": await type_entropy(conn, sim_now),
        "gini": await appearance_gini(conn, sim_now),
        "ngram_dup": await ngram_dup(conn, sim_now),
        "relation_week_change": await relation_week_change(conn, sim_now),
        "high_tension_ratio": ratio,
        "active_conflict_edges": conflict_edges,
        "intervention_rate": await intervention_rate_7d(conn, sim_now),
        "cost_micro_cny": await cost_from_snapshot(conn, day),
    }


async def refresh(conn: Any, day: dt.date) -> dict[str, Any]:
    """定稿 sim_day=day：INSERT … ON CONFLICT DO UPDATE（幂等覆盖重算）。返回指标 dict。"""
    m = await compute_day(conn, day)
    await conn.execute(
        """
        INSERT INTO health_daily (sim_day, a_grade_gap_days, type_entropy, gini, ngram_dup,
                                  relation_week_change, high_tension_ratio, active_conflict_edges,
                                  intervention_rate, cost_micro_cny)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (sim_day) DO UPDATE SET
          a_grade_gap_days=$2, type_entropy=$3, gini=$4, ngram_dup=$5, relation_week_change=$6,
          high_tension_ratio=$7, active_conflict_edges=$8, intervention_rate=$9,
          cost_micro_cny=$10, computed_at=now()
        """,
        day, m["a_grade_gap_days"], m["type_entropy"], m["gini"], m["ngram_dup"],
        m["relation_week_change"], m["high_tension_ratio"], m["active_conflict_edges"],
        m["intervention_rate"], m["cost_micro_cny"])
    log.info("health_daily 定稿 sim_day=%s（副本侧复算）", day)
    return m
