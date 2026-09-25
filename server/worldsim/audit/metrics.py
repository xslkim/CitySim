"""健康度七指标日聚合 + 主库侧 health_daily（08 T-AUD-08；04 §10.2 口径；阈值唯一持有方 01 §9，
配置镜像 `config/health_thresholds.yaml`，代码零字面阈值）。

- 有效 grade = `ui.grade` 初值 ⊕ 最新 `director.grade_revise`（04 §6.6；本机侧现算，副本侧
  `event_grade_view` 归 M6）。
- 冲突边取边方向写死：gossip 取 teller→about（01 §9/04 §10.2）；argue/refuse/爽约取施→受。
- 日结成本：¥/模拟日 = SUM(cost_micro_cny)/COUNT(DISTINCT date(sim_time)) 滚动 7 模拟日
  （04 §8.3），写 `world_state` 键 `health.cost_daily`（05 §3.5 快照流数据源）；明细不出站。
- DDL：`ddl/health_daily_v1.sql`（00 §1 A12 独立增量文件）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

SERVER_ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS_PATH = SERVER_ROOT / "config" / "health_thresholds.yaml"

CONFLICT_TYPES = ("dialogue.argue", "social.refuse", "social.appointment.stood_up")  # 01 §9 冲突动作集
GOSSIP_TYPE = "dialogue.gossip"   # 取 teller→about 边（写死，01 §9）
REL_CHANGE_AFFINITY_MIN = 3       # 周变化率判定阈 |Δaffinity|≥3（01 §9 阈值表「判定阈随 01 §9」镜像行）
HIGH_TENSION_MIN = 60             # 高张力边 tension>60（01 §9 存量口径）
CONFLICT_TENSION_MIN = 30         # 活跃冲突边 tension≥30（01 §11.2）


def load_thresholds() -> dict[str, Any]:
    with THRESHOLDS_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def classify(metric: str, value: float | int, cfg: dict[str, Any] | None = None) -> str:
    """阈值三档判定（healthy/warning/alarm；读 health_thresholds.yaml 镜像，不落字面量）。"""
    cfg = cfg or load_thresholds()
    spec = next(m for m in cfg["metrics"] if m["metric"] == metric)
    if metric == "active_conflict_edges":
        return _classify_conflict_edges(int(value[0]), int(value[1]), spec)  # (total, star_zero)
    h, w, a = spec["healthy"], spec.get("warning") or {}, spec.get("alarm") or {}
    v = float(value)

    def _in(band: dict) -> bool:
        if not band:
            return False
        lo, hi = band.get("min"), band.get("max")
        if metric == "relation_graph_weekly_change_ratio" and "low" in band:
            return False  # 双带另行处理
        lo_ok = True if lo is None else (v > lo if _open_lo(metric, band) else v >= lo)
        hi_ok = True if hi is None else (v < hi if _open_hi(metric, band) else v <= hi)
        return lo_ok and hi_ok

    if metric == "relation_graph_weekly_change_ratio":
        # 双带区间（01 §9：健康 8~25% 双闭；预警 5~8%[≥5,<8) 或 25~40%(>25,≤40]；报警 <5% 或 >40%）
        if float(h["min"]) <= v <= float(h["max"]):
            return "healthy"
        wl, wh = w.get("low") or {}, w.get("high") or {}
        al, ah = a.get("low") or {}, a.get("high") or {}
        if (al and v < float(al["max"])) or (ah and v > float(ah["min"])):
            return "alarm"
        if (wl and float(wl["min"]) <= v < float(wl["max"])) or (wh and float(wh["min"]) < v <= float(wh["max"])):
            return "warning"
        return "alarm"
    if _in(h):
        return "healthy"
    if _in(a):
        return "alarm"
    return "warning"


# 区间端点归属（01 §9 表逐字转写，health_thresholds.yaml 注释行）：healthy 闭区间；
# warning 下开上闭（min 排他）；alarm min 开（>）/max 开（<）。
_OPEN_LO_METRICS = {"a_grade_event_interval_days", "appearance_gini", "dialogue_3gram_repeat_ratio",
                    "high_tension_ratio"}
_OPEN_HI_METRICS = {"event_type_entropy_bits"}


def _open_lo(metric: str, band: dict) -> bool:
    return metric in _OPEN_LO_METRICS and "min" in band


def _open_hi(metric: str, band: dict) -> bool:
    return metric in _OPEN_HI_METRICS and "max" in band and band.get("_closed") is not True


def _classify_conflict_edges(total: int, star_zero: int, spec: dict[str, Any]) -> str:
    h, w, a = spec["healthy"], spec["warning"], spec["alarm"]
    if total < int(a["max_total"]) or star_zero >= int(a["min_star_zero_count"]):
        return "alarm"
    if total >= int(h["min_total"]) and star_zero < int(h["min_per_star"]):
        return "healthy"
    return "warning"


# ---- 七项指标（04 §10.2 逐条口径） -------------------------------------------------


async def a_grade_gap_days(pool: Any, sim_now: dt.datetime) -> float:
    """A 级事件间隔：相邻 A 级（有效 grade）间隔的 7 日均值（模拟日）。"""
    rows = await pool.fetch(
        """
        SELECT e.sim_time FROM events e
        WHERE e.sim_time > $1::timestamptz - interval '14 days' AND coalesce(
          (SELECT r.payload->>'new_grade' FROM events r
            WHERE r.type='director.grade_revise' AND r.payload->>'target_seq' = e.seq::text
            ORDER BY r.seq DESC LIMIT 1), e.ui->>'grade') = 'A'
        ORDER BY e.sim_time
        """, sim_now)
    if len(rows) < 2:
        return 7.0  # 窗口内不足两个 A 级 → 间隔按窗长劣化计（工程口径）
    gaps = [(b["sim_time"] - a["sim_time"]).total_seconds() / 86400.0
            for a, b in zip(rows, rows[1:])]
    return round(sum(gaps) / len(gaps), 4)


async def type_entropy(pool: Any, sim_now: dt.datetime) -> float:
    """事件类型熵：每日 type 分布 Shannon 熵的 7 日均值（bit）。"""
    rows = await pool.fetch(
        """
        SELECT sim_time::date AS d, type, count(*) AS n FROM events
        WHERE sim_time > $1::timestamptz - interval '7 days' GROUP BY 1, 2 ORDER BY 1
        """, sim_now)
    by_day: dict[Any, list[int]] = {}
    for r in rows:
        by_day.setdefault(r["d"], []).append(int(r["n"]))
    if not by_day:
        return 0.0
    entropies = []
    for counts in by_day.values():
        total = sum(counts)
        entropies.append(-sum((c / total) * math.log2(c / total) for c in counts))
    return round(sum(entropies) / len(entropies), 4)


def gini(values: list[float]) -> float:
    """基尼系数（纯函数；04 §10.2 出场基尼口径）。"""
    vals = sorted(float(v) for v in values)
    n = len(vals)
    if n == 0:
        return 0.0
    total = sum(vals)
    if total == 0:
        return 0.0
    return round(sum((2 * (i + 1) - n - 1) * v for i, v in enumerate(vals)) / (n * total), 4)


async def appearance_gini(pool: Any, sim_now: dt.datetime) -> float:
    """出场基尼：每模拟周各角色事件数（events.actors）分布。"""
    rows = await pool.fetch(
        """
        SELECT u.aid, count(*) AS n FROM events e
        CROSS JOIN LATERAL unnest(e.actors) AS u(aid)
        WHERE e.sim_time > $1::timestamptz - interval '7 days' GROUP BY u.aid
        """, sim_now)
    return gini([float(r["n"]) for r in rows])


async def ngram_dup(pool: Any, sim_now: dt.datetime, *, n: int = 3) -> float:
    """对话 3-gram 重复度：全库台词 3-gram 重复占比，7 日窗（04 §10.2）。"""
    rows = await pool.fetch(
        """
        SELECT l->>'text' AS text FROM events e
        CROSS JOIN LATERAL jsonb_array_elements(e.payload->'lines') AS l
        WHERE e.type = 'dialogue.chat' AND e.sim_time > $1::timestamptz - interval '7 days'
        """, sim_now)
    grams: Counter[tuple[str, ...]] = Counter()
    for r in rows:
        tokens = list(r["text"] or "")  # 字符级 n-gram（中文台词工程口径）
        for i in range(len(tokens) - n + 1):
            grams[tuple(tokens[i:i + n])] += 1
    total = sum(grams.values())
    if total == 0:
        return 0.0
    dup = sum(c - 1 for c in grams.values() if c > 1)
    return round(dup / total, 4)


async def relation_week_change(pool: Any, sim_now: dt.datetime) -> float:
    """关系图周变化率：每周 |Δaffinity|≥3 或 label 变更的边数 / 活跃边总数（relation.changed）。"""
    rows = await pool.fetch(
        """
        SELECT DISTINCT c->>'a_id' AS a, c->>'b_id' AS b,
               max(abs((c->>'delta_affinity')::int)) AS max_d,
               bool_or(c ? 'labels_added' OR c ? 'labels_removed') AS label_chg
        FROM events e CROSS JOIN LATERAL jsonb_array_elements(e.payload->'changes') AS c
        WHERE e.type = 'relation.changed' AND e.sim_time > $1::timestamptz - interval '7 days'
        GROUP BY 1, 2
        """, sim_now)
    active = len(rows)
    if active == 0:
        return 0.0
    changed = sum(1 for r in rows if int(r["max_d"]) >= REL_CHANGE_AFFINITY_MIN or r["label_chg"])
    return round(changed / active, 4)


async def high_tension_ratio(pool: Any) -> float:
    """高张力边占比：tension>60 的边 / 活跃边总数（本机侧 relations 表同口径现算，04 §10.2）。"""
    total = await pool.fetchval("SELECT count(*) FROM relations")
    if not total:
        return 0.0
    high = await pool.fetchval("SELECT count(*) FROM relations WHERE tension > $1", HIGH_TENSION_MIN)
    return round(int(high) / int(total), 4)


async def active_conflict_edges(pool: Any, sim_now: dt.datetime) -> tuple[int, int, list[str]]:
    """活跃冲突边：tension≥30 或近 7 模拟日发生过 argue/gossip/refuse/爽约的有序对（01 §11.2）。
    返回 (总数, 无冲突边明星数, 明星缺口名单)。gossip 取 teller→about 边（写死，01 §9）。"""
    edges: set[tuple[str, str]] = set()
    for r in await pool.fetch("SELECT a_id, b_id FROM relations WHERE tension >= $1", CONFLICT_TENSION_MIN):
        edges.add((r["a_id"], r["b_id"]))
    for r in await pool.fetch(
            """
            SELECT a_id, b_id FROM events e
            CROSS JOIN LATERAL (
              SELECT CASE e.type
                       WHEN 'dialogue.gossip' THEN e.payload->>'teller'
                       ELSE e.payload->>'from' END AS a_id,
                     CASE e.type
                       WHEN 'dialogue.gossip' THEN e.payload->>'about'
                       WHEN 'social.appointment.stood_up' THEN e.payload->'participants'->>0
                       ELSE e.payload->>'to' END AS b_id
            ) x
            WHERE e.sim_time > $1::timestamptz - interval '7 days'
              AND e.type = ANY($2::text[] || $3::text[])
            """, sim_now, list(CONFLICT_TYPES), [GOSSIP_TYPE]):
        if r["a_id"] and r["b_id"]:
            edges.add((r["a_id"], r["b_id"]))
    stars = [r["id"] for r in await pool.fetch("SELECT id FROM agents WHERE cognition_tier='star'")]
    star_gap = [s for s in stars if not any(s in e for e in edges)]
    return len(edges), len(star_gap), star_gap


async def intervention_rate(pool: Any, sim_now: dt.datetime) -> float:
    """干预率 7 日滑窗（04 §10.1 ④ 同口径；复用 T-DIR-03 唯一实现）。"""
    from ..world_agent.director.intervene import intervention_rate_7d

    return await intervention_rate_7d(pool, sim_now)


async def cost_per_simday(pool: Any, sim_now: dt.datetime) -> int:
    """日结成本（微元）：SUM(cost)/COUNT(DISTINCT date) 滚动 7 模拟日（04 §8.3）。"""
    v = await pool.fetchval(
        """
        SELECT coalesce(SUM(cost_micro_cny), 0) / NULLIF(COUNT(DISTINCT sim_time::date), 0)
        FROM llm_calls WHERE sim_time > $1::timestamptz - interval '7 days'
        """, sim_now)
    return int(v or 0)


# ---- 日聚合落库 --------------------------------------------------------------------


async def compute_daily(pool: Any, sim_now: dt.datetime) -> dict[str, Any]:
    """七项 + 干预率 + 成本全量计算（04 §10.2 七指标与审计同跑）。"""
    edges, star_gap, gap_list = await active_conflict_edges(pool, sim_now)
    return {
        "a_grade_gap_days": await a_grade_gap_days(pool, sim_now),
        "type_entropy": await type_entropy(pool, sim_now),
        "gini": await appearance_gini(pool, sim_now),
        "ngram_dup": await ngram_dup(pool, sim_now),
        "relation_week_change": await relation_week_change(pool, sim_now),
        "high_tension_ratio": await high_tension_ratio(pool),
        "active_conflict_edges": edges,
        "stars_without_conflict": star_gap,
        "intervention_rate": await intervention_rate(pool, sim_now),
        "cost_micro_cny": await cost_per_simday(pool, sim_now),
        "_star_gap_list": gap_list,
    }


async def write_health_daily(pool: Any, sim_now: dt.datetime, *,
                             metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    """聚合落 health_daily + 日结成本写 world_state health.cost_daily（05 §3.5 快照流数据源）。"""
    m = metrics or await compute_daily(pool, sim_now)
    day = sim_now.date()
    await pool.execute(
        """
        INSERT INTO health_daily (sim_day, a_grade_gap_days, type_entropy, gini, ngram_dup,
                                  relation_week_change, high_tension_ratio, active_conflict_edges,
                                  stars_without_conflict, intervention_rate, cost_micro_cny)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        ON CONFLICT (sim_day) DO UPDATE SET
          a_grade_gap_days=$2, type_entropy=$3, gini=$4, ngram_dup=$5, relation_week_change=$6,
          high_tension_ratio=$7, active_conflict_edges=$8, stars_without_conflict=$9,
          intervention_rate=$10, cost_micro_cny=$11, computed_at=now()
        """,
        day, m["a_grade_gap_days"], m["type_entropy"], m["gini"], m["ngram_dup"],
        m["relation_week_change"], m["high_tension_ratio"], m["active_conflict_edges"],
        m["stars_without_conflict"], m["intervention_rate"], m["cost_micro_cny"])
    await pool.execute(
        """
        INSERT INTO world_state (key, value, updated_tick) VALUES ('health.cost_daily', $1::jsonb, 0)
        ON CONFLICT (key) DO UPDATE SET value=$1::jsonb, updated_at=now()
        """, json.dumps(m["cost_micro_cny"]))
    return m


def classify_all(m: dict[str, Any]) -> dict[str, str]:
    """七指标阈值三档判定（读 health_thresholds.yaml，进日报）。"""
    cfg = load_thresholds()
    return {
        "a_grade_event_interval_days": classify("a_grade_event_interval_days", m["a_grade_gap_days"], cfg),
        "event_type_entropy_bits": classify("event_type_entropy_bits", m["type_entropy"], cfg),
        "appearance_gini": classify("appearance_gini", m["gini"], cfg),
        "dialogue_3gram_repeat_ratio": classify("dialogue_3gram_repeat_ratio", m["ngram_dup"], cfg),
        "relation_graph_weekly_change_ratio": classify("relation_graph_weekly_change_ratio",
                                                       m["relation_week_change"], cfg),
        "high_tension_edge_ratio": classify("high_tension_edge_ratio", m["high_tension_ratio"], cfg),
        "active_conflict_edges": classify("active_conflict_edges",
                                          (m["active_conflict_edges"], m["stars_without_conflict"]), cfg),
    }
