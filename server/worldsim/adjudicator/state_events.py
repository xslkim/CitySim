"""聚合状态事件（`state.needs_delta` / `relation.changed`，04 §6.5；06 §1.2；02 T-ADJ-06 机制）。

波次 2a 交付（T-REL-01~05 消费地基；T-ADJ-06 波次 2b 接管完整语义：trigger 细化/审计⑥对账/grade 联动）：

- `state.needs_delta`：`payload.changes=[{agent_id, need, delta, new_value, cause}]`；
  情绪摆动同结构携带（`need='mood'`）。`relation.changed`：`changes=[{a_id, b_id,
  delta_affinity, delta_tension, labels_added?, labels_removed?, cause}]`（字段逐字 06 §1.2）。
- **每 tick 合并写入，无变更不落，每 tick 至多 +2 条**；`visibility='internal'`、不携带展示文本键；
  `trigger` 取引发源（交互 `'autonomous'`、系统结算 `'system'`）。
- `cause` = **裸 seq 数字字符串**（00 §4 红线 3）；写入侧强校验（非数字串直接抛错）。
- `agents.needs` / `relations` 降为缓存列：事实源 = 初始值 + Σ 事件流；本类是缓存列唯一写入路径
  （数值只由系统改，04 §6.1 step5）。`rebuild_*` 供 replay/审计对账（04 §10.1 ⑥ 口径地基）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# 六需求键（01 §3.1：饥饿/精力/情绪/社交/成就/财富；情绪以 need='mood' 同结构携带，04 §6.5）
NEED_KEYS: tuple[str, ...] = ("hunger", "energy", "mood", "social", "achievement", "wealth")

NEED_MIN, NEED_MAX = 0.0, 100.0          # 需求值域 0~100（01 §3.1）
AFFINITY_MIN, AFFINITY_MAX = -100, 100   # affinity ∈ [-100,100]（01 §3.2）
TENSION_MIN, TENSION_MAX = 0, 100        # tension ∈ [0,100]（01 §3.2）

_DELTA_NDIGITS = 2  # 衰减积分产生小数；记录与缓存同取 2 位舍入（rebuild 与缓存逐位一致的前提）


def _check_cause(cause: str) -> str:
    """cause 值形态 = 裸 seq 数字字符串（00 §4 红线 3；禁止 'e<seq>' 作值）。"""
    if not isinstance(cause, str) or not cause.isdigit():
        raise ValueError(f"cause 必须为裸 seq 数字字符串，得到 {cause!r}（00 §4 红线 3）")
    return cause


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class StateAggregator:
    """每 tick 聚合器：收集本 tick 全部 needs/关系变更，flush 合并落 ≤2 条事件。

    单实例可跨 tick 复用（flush 清空缓冲）；写入仅发生在裁决协程内（00 §4 红线 10 串行前提）。
    `grader`（T-ADJ-07/T-DIR-04）：非空时 flush 落库同写 `ui.grade` 初值（全事件覆盖口径，
    04 §6.6 同事务语义延伸——聚合事件无预申报信号，按 R1/R4 实参即时判定）。
    """

    def __init__(self, pool: Any, grader: Any = None) -> None:
        self._pool = pool
        self._grader = grader
        self._needs_changes: list[dict[str, Any]] = []
        self._rel_changes: list[dict[str, Any]] = []

    # ---- needs 缓存列写入 + 变更记录 ----------------------------------------

    async def apply_needs_delta(self, *, agent_id: str, need: str, delta: float, cause: str) -> dict[str, Any] | None:
        """对 agent 的某项需求施加 delta（衰减/满足/事件伤害），更新缓存列并记录变更。

        返回变更记录（含 new_value）；clamp 后无实际变化时返回 None 且不记录（无变更不落）。
        """
        _check_cause(cause)
        if need not in NEED_KEYS:
            raise ValueError(f"未知需求键 {need!r}（六需求 = {NEED_KEYS}，01 §3.1）")
        delta = round(float(delta), _DELTA_NDIGITS)
        needs = await self.read_needs(agent_id)
        old = float(needs.get(need, 0.0))
        new = round(_clamp(old + delta, NEED_MIN, NEED_MAX), _DELTA_NDIGITS)
        applied = round(new - old, _DELTA_NDIGITS)
        if applied == 0.0:
            return None
        needs[need] = new
        await self._pool.execute("UPDATE agents SET needs=$2::jsonb WHERE id=$1", agent_id, json.dumps(needs, ensure_ascii=False))
        change = {"agent_id": agent_id, "need": need, "delta": applied, "new_value": new, "cause": cause}
        self._needs_changes.append(change)
        return change

    async def read_needs(self, agent_id: str) -> dict[str, float]:
        row = await self._pool.fetchrow("SELECT needs FROM agents WHERE id=$1", agent_id)
        if row is None:
            raise KeyError(f"agent {agent_id!r} 不存在")
        needs = row["needs"]
        if isinstance(needs, str):
            needs = json.loads(needs)
        return {k: float(v) for k, v in dict(needs or {}).items()}

    # ---- relations 缓存列写入 + 变更记录 -------------------------------------

    async def apply_relation_delta(
        self,
        *,
        a_id: str,
        b_id: str,
        delta_affinity: int = 0,
        delta_tension: int = 0,
        cause: str,
        labels_added: list[str] | None = None,
        labels_removed: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """对有序对 (A→B) 关系边施加变更（01 §3.2 单向语义），UPSERT 缓存行并记录变更。"""
        _check_cause(cause)
        if a_id == b_id:
            raise ValueError("关系边为有序对 (A→B)，a_id 不得等于 b_id（04 §5.2 relations CHECK）")
        delta_affinity = int(delta_affinity)
        delta_tension = int(delta_tension)
        labels_added = [str(x) for x in (labels_added or [])]
        labels_removed = [str(x) for x in (labels_removed or [])]
        if not delta_affinity and not delta_tension and not labels_added and not labels_removed:
            return None
        row = await self._pool.fetchrow("SELECT affinity, tension, labels FROM relations WHERE a_id=$1 AND b_id=$2", a_id, b_id)
        if row is None:
            old_aff, old_ten, old_labels = 0, 0, []
        else:
            old_aff, old_ten, old_labels = int(row["affinity"]), int(row["tension"]), list(row["labels"])
        new_aff = int(_clamp(old_aff + delta_affinity, AFFINITY_MIN, AFFINITY_MAX))
        new_ten = int(_clamp(old_ten + delta_tension, TENSION_MIN, TENSION_MAX))
        new_labels = sorted({*old_labels, *labels_added} - set(labels_removed))
        applied_aff = new_aff - old_aff
        applied_ten = new_ten - old_ten
        real_added = sorted(set(new_labels) - set(old_labels))
        real_removed = sorted(set(old_labels) - set(new_labels))
        if not applied_aff and not applied_ten and not real_added and not real_removed:
            return None
        await self._pool.execute(
            """
            INSERT INTO relations (a_id, b_id, affinity, tension, labels, last_event_seq, updated_at)
            VALUES ($1, $2, $3, $4, $5::text[], $6, now())
            ON CONFLICT (a_id, b_id) DO UPDATE SET
              affinity=$3, tension=$4, labels=$5::text[], last_event_seq=$6, updated_at=now()
            """,
            a_id, b_id, new_aff, new_ten, new_labels, int(cause),
        )
        change: dict[str, Any] = {
            "a_id": a_id, "b_id": b_id,
            "delta_affinity": applied_aff, "delta_tension": applied_ten,
            "cause": cause,
        }
        if real_added:
            change["labels_added"] = real_added
        if real_removed:
            change["labels_removed"] = real_removed
        self._rel_changes.append(change)
        return change

    # ---- flush：每 tick 合并落 ≤2 条（04 §6.5） -------------------------------

    @property
    def pending(self) -> tuple[int, int]:
        return len(self._needs_changes), len(self._rel_changes)

    async def flush(self, *, tick: int, sim_now: dt.datetime, trigger: str, rng_seed: int) -> list[int]:
        """合并落库：needs 有变更落 1 条 state.needs_delta，关系有变更落 1 条 relation.changed。

        `trigger` 取引发源（交互结算 'autonomous'、系统结算 'system'，06 §1.2 枚举内）；
        visibility='internal'、payload 仅 changes 键（internal 事件不携带展示文本键）。
        """
        if trigger not in ("autonomous", "system"):
            raise ValueError(f"聚合状态事件 trigger 仅取 'autonomous'/'system'（引发源），得到 {trigger!r}")
        seqs: list[int] = []
        for type_, changes in (("state.needs_delta", self._needs_changes), ("relation.changed", self._rel_changes)):
            if not changes:
                continue
            actors = sorted({c.get("agent_id") for c in changes if c.get("agent_id")} | {x for c in changes for x in (c.get("a_id"), c.get("b_id")) if x})
            payload = {"changes": changes}
            ui: dict[str, Any] | None = None
            if self._grader is not None:
                # 聚合事件 grade 初值：R1=卷入并集、R4=cause 链接存在（04 §6.6 即时判定口径）
                ui = {"grade": await self._grader.grade(
                    type_=type_, actors=actors, payload=payload, sim_now=sim_now,
                    rel_hit=type_ == "relation.changed",
                    mood_hit=any(c.get("need") == "mood" and abs(float(c.get("delta", 0))) >= self._grader.r3_min
                                 for c in changes),
                    followups=True,
                )}
            seq = await self._pool.fetchval(
                """
                INSERT INTO events (tick, sim_time, type, source, trigger, actors, rng_seed, visibility, payload, ui)
                VALUES ($1, $2, $3, 'system', $4, $5, $6, 'internal', $7::jsonb, $8::jsonb)
                RETURNING seq
                """,
                tick, sim_now, type_, trigger, actors, rng_seed,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(ui, ensure_ascii=False) if ui else None,
            )
            seqs.append(seq)
            log.debug("%s 落库：tick=%d changes=%d", type_, tick, len(changes))
        self._needs_changes, self._rel_changes = [], []
        return seqs


# ---- 事件流重建（04 §6.5 事实源口径；replay/审计⑥对账地基） --------------------


async def rebuild_needs(pool: Any, agent_id: str, initial: dict[str, float]) -> dict[str, float]:
    """needs 重建 = 初始值 + Σ state.needs_delta（按 seq 序逐条施加，含 clamp 语义）。"""
    values = {k: round(float(v), _DELTA_NDIGITS) for k, v in initial.items()}
    rows = await pool.fetch(
        """
        SELECT payload->'changes' AS changes FROM events
        WHERE type='state.needs_delta' AND $1 = ANY(actors) ORDER BY seq
        """,
        agent_id,
    )
    for row in rows:
        changes = row["changes"]
        if isinstance(changes, str):
            changes = json.loads(changes)
        for c in changes:
            if c.get("agent_id") != agent_id:
                continue
            need = c["need"]
            old = float(values.get(need, 0.0))
            values[need] = round(_clamp(old + float(c["delta"]), NEED_MIN, NEED_MAX), _DELTA_NDIGITS)
    return values


async def rebuild_relation(pool: Any, a_id: str, b_id: str) -> tuple[int, int, list[str]]:
    """单条关系边重建 = (0,0,[]) + Σ relation.changed（按 seq 序；labels 施加增删）。"""
    aff, ten, labels = 0, 0, set()
    rows = await pool.fetch(
        """
        SELECT payload->'changes' AS changes FROM events
        WHERE type='relation.changed' AND $1 = ANY(actors) AND $2 = ANY(actors) ORDER BY seq
        """,
        a_id, b_id,
    )
    for row in rows:
        changes = row["changes"]
        if isinstance(changes, str):
            changes = json.loads(changes)
        for c in changes:
            if c.get("a_id") != a_id or c.get("b_id") != b_id:
                continue
            aff = int(_clamp(aff + int(c["delta_affinity"]), AFFINITY_MIN, AFFINITY_MAX))
            ten = int(_clamp(ten + int(c["delta_tension"]), TENSION_MIN, TENSION_MAX))
            labels = (labels | set(c.get("labels_added", []))) - set(c.get("labels_removed", []))
    return aff, ten, sorted(labels)
