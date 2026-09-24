"""关系结算矩阵与自然回归（02 T-REL-02；01 §3.2 数值唯一持有方 / 04 §6.5 聚合事件）。

- affinity/tension 值域与有序对 (A→B) 语义见 01 §3.2；矩阵数值一律读 `config/relations.yaml`
  （01 §3.2 镜像），P0 行 M1 全实现、P1/P2 行经同一 `settle` 接口同构可用（02 文档口径"同构预留"）。
- chat 愉快判定读对话自评（01 §7：`chat_band`，min≥7 愉快 / max≤3 敷衍 / 其间平淡）；
  同日同对第 3 次起减半（当日 chat 计数查事件流，可回放；减半取整 = 向零取整，工程默认 D16）。
- 自然回归每周结算一次：affinity 向 0 回归 5%；tension 周回归率 = max(5%, tension×10%)
  （存量非线性，01 §3.2 末段；回归量取整 = round 半进偶，工程默认 D16）。
- 全部变更经 `StateAggregator` 并入本 tick `relation.changed`（04 §6.5），缓存行可全量重建。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import yaml

from ..adjudicator.state_events import StateAggregator

CHAT_BANDS = ("enjoyable", "flat", "perfunctory")  # 愉快/平淡/敷衍（01 §7 自评映射）


def load_relations_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("matrix", "regression", "chat", "cooldown_hours", "argue_damping", "refuse_streak", "grudge", "willingness"):
        if key not in cfg:
            raise ValueError(f"relations.yaml 缺 {key} 段（01 §3.2/§3.4/§3.5 镜像）")
    return cfg


class RelationEngine:
    """关系结算引擎。`pool` = asyncpg pool（同日计数查事件流）；`cfg` = relations.yaml 字典。"""

    def __init__(self, pool: Any, cfg: dict[str, Any]) -> None:
        self._pool = pool
        self._cfg = cfg
        self._matrix = cfg["matrix"]

    # ---- chat 自评档位（01 §7） ------------------------------------------------

    @staticmethod
    def chat_band(a_enjoy: float, b_enjoy: float) -> str:
        """min ≥7 → 愉快；max ≤3 → 敷衍；其间 → 平淡（01 §7 对话自评行）。"""
        lo, hi = min(a_enjoy, b_enjoy), max(a_enjoy, b_enjoy)
        if lo >= 7:
            return "enjoyable"
        if hi <= 3:
            return "perfunctory"
        return "flat"

    # ---- 矩阵查询 ---------------------------------------------------------------

    def matrix_row(self, kind: str) -> dict[str, Any]:
        if kind == "chat_flat":
            return self._matrix["chat_flat"]
        if kind not in self._matrix:
            raise KeyError(f"relations.yaml matrix 无行 {kind!r}（01 §3.2）")
        return self._matrix[kind]

    # ---- 结算（经聚合器并入 relation.changed） ------------------------------------

    async def settle(
        self,
        agg: StateAggregator,
        *,
        kind: str,
        a_id: str,
        b_id: str,
        cause: str,
        factor: float = 1.0,
        scope: str | None = None,
        labels_added: list[str] | None = None,
        labels_removed: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按矩阵行结算关系变化。`scope='both'`（对称动作）双方同时结算各自的值（01 §3.2 章首）。

        `factor` 用于礼貌拒绝减半/同日减半等修正（向零取整，工程默认 D16）。
        """
        row = self.matrix_row(kind)
        eff_scope = scope or row.get("scope", "a_to_b")
        d_aff = _scale(row["delta_affinity"], factor)
        d_ten = _scale(row["delta_tension"], factor)
        changes: list[dict[str, Any]] = []
        pairs = [(a_id, b_id)] if eff_scope == "a_to_b" else [(a_id, b_id), (b_id, a_id)]
        for x, y in pairs:
            change = await agg.apply_relation_delta(
                a_id=x, b_id=y, delta_affinity=d_aff, delta_tension=d_ten, cause=cause,
                labels_added=labels_added, labels_removed=labels_removed,
            )
            if change is not None:
                changes.append(change)
        return changes

    async def settle_chat(
        self, agg: StateAggregator, *, a_id: str, b_id: str, band: str, cause: str, sim_now: dt.datetime,
        exclude_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """chat 结算：愉快/平淡按矩阵（敷衍无矩阵行、不进关系结算，冷却归 T-REL-04）；
        当日同对第 3 次起减半（01 §3.2 chat 行；当日计数查事件流，回放一致）。

        `exclude_seq`：调用方在落 dialogue.chat 事件后才结算时传入该事件 seq，减半计数只计
        此前场次（"第 3 次起"语义不含本场，T-ADJ-04 对话引擎口径）。
        """
        if band not in CHAT_BANDS:
            raise ValueError(f"未知 chat 档位 {band!r}（01 §7：{CHAT_BANDS}）")
        if band == "perfunctory":
            return []
        prior = await self.chats_today(a_id, b_id, sim_now, exclude_seq=exclude_seq)
        halving_from = int(self._cfg["chat"]["same_pair_daily_halving_from"])
        factor = 0.5 if prior + 1 >= halving_from else 1.0
        kind = "chat_enjoyable" if band == "enjoyable" else "chat_flat"
        return await self.settle(agg, kind=kind, a_id=a_id, b_id=b_id, cause=cause, factor=factor, scope="both")

    async def chats_today(self, a_id: str, b_id: str, sim_now: dt.datetime, *, exclude_seq: int | None = None) -> int:
        """当日（本地日界）两人间已落库的 dialogue.chat 场次数（减半规则数据源）。"""
        day_start = sim_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._pool.fetchval(
            """
            SELECT count(*) FROM events
            WHERE type='dialogue.chat' AND actors @> $1::text[] AND sim_time >= $2 AND sim_time < $3
              AND ($4::bigint IS NULL OR seq <> $4)
            """,
            sorted([a_id, b_id]), day_start, day_start + dt.timedelta(days=1), exclude_seq,
        )

    # ---- 自然回归（每周结算一次，01 §3.2 末段） ------------------------------------

    def regression_deltas(self, affinity: int, tension: int) -> tuple[int, int]:
        """回归量计算（纯函数）：affinity 向 0 回归 5%；tension 回归率 = max(5%, tension×10%)。"""
        reg = self._cfg["regression"]
        aff_rate = float(reg["affinity_rate"])
        ten_rate = max(float(reg["tension_min_rate"]), tension * float(reg["tension_scale"]) / 100.0)
        d_aff = -_sign(affinity) * round(abs(affinity) * aff_rate)
        d_ten = -round(tension * ten_rate)
        return int(d_aff), int(d_ten)

    async def weekly_regression(self, agg: StateAggregator, *, cause: str) -> list[dict[str, Any]]:
        """全量关系边周回归：无交互的关系自然冷却（01 §3.2）；变更并入 relation.changed。"""
        rows = await self._pool.fetch("SELECT a_id, b_id, affinity, tension FROM relations ORDER BY a_id, b_id")
        changes: list[dict[str, Any]] = []
        for r in rows:
            d_aff, d_ten = self.regression_deltas(int(r["affinity"]), int(r["tension"]))
            if not d_aff and not d_ten:
                continue
            change = await agg.apply_relation_delta(
                a_id=r["a_id"], b_id=r["b_id"], delta_affinity=d_aff, delta_tension=d_ten, cause=cause,
            )
            if change is not None:
                changes.append(change)
        return changes


def _scale(v: int | float, factor: float) -> int:
    """矩阵值 × 修正系数，向零取整（减半/礼貌减半语义，工程默认 D16）。"""
    return int(float(v) * factor)


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)
