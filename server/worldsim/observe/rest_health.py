"""REST：健康度接口（05 T-WEB-06；03 §3.6、05 §3.5、01 §9 阈值唯一持有方）。

`/api/health` 单接口下发：七指标当前值（`obs.health_daily` 最新行 + 当日未定稿部分实时小查询，
05 §6 行 / 05 文档 D12）+ **阈值与阈值色随响应下发**（每请求重读 `config/health_thresholds.yaml`
——文件归 01 T-CFG-07 创建、数值持有方 01 §9，本模块只消费；改 yaml 即生效，前端零硬编码）+
运行指标（干预率 / 成本 vs 熔断线 / 副本延迟）。

色判定：green/yellow/red 三档（前端映射 green→positive/yellow→warn/red→negative，02 §7.1）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends

from .app import get_pool, ok_envelope, require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

SERVER_ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS_PATH = SERVER_ROOT / "config" / "health_thresholds.yaml"
MODELS_PATH = SERVER_ROOT / "config" / "models.yaml"

# 指标 key（health_thresholds.yaml，01 §9 镜像）→ obs.health_daily 列（05 §3.5）
METRIC_COLUMN = {
    "a_grade_event_interval_days": "a_grade_gap_days",
    "event_type_entropy_bits": "type_entropy",
    "appearance_gini": "gini",
    "dialogue_3gram_repeat_ratio": "ngram_dup",
    "relation_graph_weekly_change_ratio": "relation_week_change",
    "high_tension_edge_ratio": "high_tension_ratio",
    "active_conflict_edges": "active_conflict_edges",
}

# 卡片建议动作文案（03 §3.6 表逐行）
METRIC_ADVICE = {
    "a_grade_event_interval_days": "编剧该排弧线了（L1/L2 干预）",
    "event_type_entropy_bits": "退化为吃饭睡觉循环，查动机系统",
    "appearance_gini": "驱动明星层轮换",
    "dialogue_3gram_repeat_ratio": "台词套路化，查 prompt 多样性",
    "relation_graph_weekly_change_ratio": "死水或崩坏",
    "high_tension_edge_ratio": "tension 淤积——L2 修复助推、减少对立性扰动",
    "active_conflict_edges": "戏不够——点火一台冲突发生器",
}


def load_thresholds() -> list[dict[str, Any]]:
    """每请求重读（验收：改 yaml 重读生效）。"""
    cfg = yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    return cfg["metrics"]


def _in_band(v: float, band: dict[str, Any]) -> bool:
    """区间判定：min 开区间（>）、max 闭区间（≤）——01 §9 表端点归属（health_thresholds.yaml 注释）。"""
    lo = band.get("min")
    hi = band.get("max")
    if lo is not None and not (v > float(lo)):
        return False
    if hi is not None and not (v <= float(hi)):
        return False
    return True


def _bands(spec: Any) -> list[dict[str, Any]]:
    """warning/alarm 允许单段或 low/high 双段（relation_graph_weekly_change_ratio）。"""
    if not isinstance(spec, dict):
        return []
    if "low" in spec or "high" in spec:
        return [b for b in (spec.get("low"), spec.get("high")) if isinstance(b, dict)]
    return [spec]


def classify_color(value: float | None, metric_spec: dict[str, Any]) -> str | None:
    """green/yellow/red 三档判定（阈值全量读 yaml 段；active_conflict_edges 的 star 维度由
    metrics.py 权威实现持有，本接口只按 total 档判定总量段，per-star 明细随值下发）。"""
    if value is None:
        return None
    v = float(value)
    healthy = metric_spec.get("healthy") or {}
    if "min_total" in healthy:  # active_conflict_edges 计数型（01 §9）
        if v >= float(healthy["min_total"]):
            return "green"
        warn = metric_spec.get("warning") or {}
        rng = warn.get("total_range")
        if rng and float(rng[0]) <= v <= float(rng[1]):
            return "yellow"
        return "red"
    if _in_band(v, healthy):
        return "green"
    for band in _bands(metric_spec.get("warning")):
        if _in_band(v, band):
            return "yellow"
    return "red"


async def health_payload(pool: Any) -> dict[str, Any]:
    """`/api/health` data 组装（WS health 频道每分钟推送复用本函数，T-WEB-07）。"""
    latest = await pool.fetchrow("SELECT * FROM obs.health_daily ORDER BY sim_day DESC LIMIT 1")
    metrics = []
    for spec in load_thresholds():
        key = spec["metric"]
        col = METRIC_COLUMN.get(key)
        value = latest[col] if (latest is not None and col) else None
        metrics.append({
            "key": key,
            "column": col,
            "value": float(value) if value is not None else None,
            "unit": spec.get("unit"),
            "window": spec.get("window"),
            "thresholds": {k: spec.get(k) for k in ("healthy", "warning", "alarm")},
            "color": classify_color(value, spec),
            "advice": METRIC_ADVICE.get(key),
        })
    # 当日未定稿部分实时小查询（05 §6 行 / D12：主库侧现算）
    today_partial = await pool.fetchrow(
        """
        SELECT count(*) AS events_today,
               count(*) FILTER (WHERE g.grade = 'A') AS a_grade_today,
               count(*) FILTER (WHERE e.trigger = 'director') AS director_today
        FROM obs.events e LEFT JOIN obs.event_grade_view g ON g.seq = e.seq
        WHERE (e.sim_time AT TIME ZONE 'Asia/Shanghai')::date
              = (SELECT (max(sim_time) AT TIME ZONE 'Asia/Shanghai')::date FROM obs.events)
        """
    )
    models_cfg = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8"))
    th = models_cfg.get("thresholds") or {}
    cap = th.get("intervention_rate_cap")  # 数值持有方 = 源方案 §4.8（models.yaml 镜像行）
    breaker = th.get("cost_breaker") or {}
    cost_limit_cny = float(os.environ.get("WSIM_COST_LIMIT_CNY_PER_SIMDAY", "0") or 0)
    events_today = int(today_partial["events_today"]) if today_partial else 0
    director_today = int(today_partial["director_today"]) if today_partial else 0
    return {
        "sim_day": latest["sim_day"].isoformat() if latest else None,
        "metrics": metrics,
        "today_partial": {
            "events_today": events_today,
            "a_grade_today": int(today_partial["a_grade_today"]) if today_partial else 0,
            "intervention_rate_today": (director_today / events_today) if events_today else 0.0,
        },
        "runtime": {
            "intervention_rate": float(latest["intervention_rate"]) if latest and latest["intervention_rate"] is not None else None,
            "intervention_rate_cap": cap,
            "cost_micro_cny": int(latest["cost_micro_cny"]) if latest and latest["cost_micro_cny"] is not None else None,
            "cost_limit_micro_cny": int(cost_limit_cny * 1_000_000),
            "cost_breaker": {"alarm_ratio": breaker.get("alarm_ratio"),
                             "throttle_ratio": breaker.get("throttle_ratio")},
            # M4 主库直连：副本延迟恒 0（05 文档 D4；M6 切副本 DSN 后获得真实语义）
            "replica_lag_s": 0,
            "replica_lag_ticks": 0,
        },
    }


@router.get("/health")
async def api_health(pool: Any = Depends(get_pool)) -> dict[str, Any]:
    return await ok_envelope(pool, await health_payload(pool))
