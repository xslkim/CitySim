"""编剧 K3 复核与 `director.grade_revise`（04 T-DIR-05；01 §6.4；04 §6.6/§5.1/§8.1；06 §1.2）。

- 复核时点：每模拟日批量 1 次（凌晨 batch 段结算后对刚结束模拟日，工程默认 D-19 已登记）；
  输入 = 当日 `ui.grade` 初值候选集摘要；K3 按两条编辑维度终审（口径 01 §6.4，爽点 01 §11.3）。
- 改判只追加事件：`director.grade_revise` payload `{target_seq, new_grade, reason}` 逐字 06 §1.2，
  `target_seq` = 裸 seq 数字字符串（04 §5.1/00 §4 红线 3），`source='director'`、`trigger='director'`
  （计干预率，量极小口径见 06 §1.2 注释），同落 `interventions` 行（level 取值设计未定义，
  工程默认 'L2' 编辑终审口径，D-30）；events 表永不 UPDATE（红线 4）。
- 上调 B→A 每日上限读 `world.yaml` `director.revise.daily_up_cap`（01 §6.4 保稀缺），超量硬截断
  并 WARN 留痕；下调 A→B 不设上限。
- 有效 grade helper = `adjudicator.grade.effective_grade`（04 §6.6，M1 已预留，本模块复用不重写）。
- K3 调用走 `director` task_type 路由（04 §8.1：失败→任务排队延后，不降级换模型）：
  `ChainExhausted/ProviderUnavailable/超时` → 当日跳过 + WARN + `world_state` `review.pending`
  排队标记；M3 以 mock/桩跑通，真接入随 M2 路由（`--llm routed`）切换。
- 降速读取点（接线归 08 T-OPS-02）：注入 `call_factor` 回调（ThrottleState.director_call_factor，
  03 T-LLM-08）；<1 时每日上调上限按比例减半（04 §8.4 动作②「编剧 K3 调用减半」工程落点）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
from typing import Any, Callable

from ...adjudicator.grade import GRADE_LEVELS, effective_grade  # noqa: F401（再导出：本机侧 helper 消费位）
from ...llm_gateway import ChainExhausted, ProviderUnavailable

log = logging.getLogger(__name__)

PENDING_KEY = "review.pending"  # world_state：K3 失败排队标记（04 §8.1 任务排队延后）

_TARGET_RE = re.compile(r"^[0-9]+$")  # target_seq 裸 seq 数字字符串（禁止 'e<seq>' 形态）


def _parse_revises(text: str) -> list[dict[str, Any]]:
    """解析 K3 输出 {note, revises[]}；格式异常 → 空列表 + WARN（复核失败不阻断世界）。"""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        log.warning("K3 复核输出非 JSON，当日跳过改判")
        return []
    out = []
    for r in obj.get("revises") or []:
        ts, ng = str(r.get("target_seq", "")), str(r.get("new_grade", ""))
        if not _TARGET_RE.match(ts) or ng not in GRADE_LEVELS:
            log.warning("K3 改判条目形态非法（target_seq=%r new_grade=%r），跳过", ts, ng)
            continue
        out.append({"target_seq": ts, "new_grade": ng, "reason": str(r.get("reason", "")),
                    "arc_id": r.get("arc_id")})
    return out


class ReviewEngine:
    """K3 每日复核引擎。`gateway` 走 director 路由；`call_factor` = 降速读取点（T-OPS-02 接线）。"""

    def __init__(self, pool: Any, gateway: Any, clock: Any, *,
                 revise_cfg: dict[str, Any], registry: Any = None,
                 call_factor: Callable[[], float] | None = None) -> None:
        self._pool = pool
        self._gw = gateway
        self._clock = clock
        self._daily_up_cap = int((revise_cfg or {}).get("daily_up_cap", 2))
        self._registry = registry
        self._call_factor = call_factor

    # ---- 候选集 ----------------------------------------------------------------

    async def _candidates(self, day: dt.date) -> list[dict[str, Any]]:
        """当日 ui.grade 初值候选集摘要（A/B 两级；C 不进复核视野，01 §6.4 口径）。"""
        rows = await self._pool.fetch(
            """
            SELECT seq, type, ui->>'grade' AS grade, payload->>'text_display' AS text_display
            FROM events WHERE sim_time::date = $1 AND ui->>'grade' IN ('A', 'B')
            ORDER BY seq
            """, day)
        return [dict(r) for r in rows]

    # ---- 主流程 -----------------------------------------------------------------

    async def run_daily_review(self, day: dt.date) -> dict[str, Any]:
        """对刚结束模拟日批量终审一次。返回统计 {ups, downs, skipped, warned, pending}。"""
        stats = {"ups": 0, "downs": 0, "skipped": 0, "warned": 0, "pending": False}
        candidates = await self._candidates(day)
        if not candidates:
            return stats
        # 降速读取点：director_call_factor <1 → 每日上调上限减半（04 §8.4 动作②）
        factor = float(self._call_factor()) if self._call_factor else 1.0
        up_cap = max(1, math.floor(self._daily_up_cap * factor)) if factor < 1.0 else self._daily_up_cap

        messages = self._render_prompt(day, candidates)
        sim_now = self._clock.now_sim()
        try:
            result = await self._gw.chat(
                "director", messages, seed=self._clock.tick_of(sim_now), sim_time=sim_now,
                tick=self._clock.tick_of(sim_now))
            revises = _parse_revises(result.text)
        except (ChainExhausted, ProviderUnavailable) as e:
            # 04 §8.1：director 路由不降级换模型——失败排队延后，当日可跳过但 WARN 落痕
            await self._mark_pending(day, str(e))
            stats["pending"] = True
            stats["warned"] += 1
            log.warning("K3 复核失败（%s），%s 排队延后（04 §8.1）", e, day)
            return stats
        except Exception as e:  # 超时/网络等——同口径排队
            await self._mark_pending(day, str(e))
            stats["pending"] = True
            stats["warned"] += 1
            log.warning("K3 复核异常（%s），%s 排队延后", e, day)
            return stats

        valid_seqs = {int(c["seq"]): c for c in candidates}
        for r in revises:
            seq = int(r["target_seq"])
            if seq not in valid_seqs:
                stats["skipped"] += 1
                log.warning("K3 改判目标 %s 不在当日候选集，跳过", seq)
                continue
            cur = await effective_grade(self._pool, seq)
            new = r["new_grade"]
            if new == cur:
                stats["skipped"] += 1
                continue
            if new == "A" and cur != "A":  # 上调 B→A 每日上限硬截断（01 §6.4 保稀缺）
                if stats["ups"] >= up_cap:
                    stats["warned"] += 1
                    log.warning("K3 上调超每日上限 %d（cap×%.2f），seq=%d 截断", up_cap, factor, seq)
                    continue
                stats["ups"] += 1
            elif cur == "A" and new != "A":
                stats["downs"] += 1  # 下调 A→B 不设上限（01 §6.4）
            else:  # B→C 等组合：设计只列上调 B→A / 下调 A→B，其余截断
                stats["skipped"] += 1
                continue
            await self._emit_revise(seq, new, r["reason"], arc_id=r.get("arc_id"), sim_now=sim_now)
        await self._clear_pending(day)
        return stats

    # ---- 落库（append-only；同落 interventions 行，D-30 level='L2'） ----------------

    async def _emit_revise(self, target_seq: int, new_grade: str, reason: str, *,
                           arc_id: str | None, sim_now: dt.datetime) -> int:
        tick = self._clock.tick_of(sim_now)
        seq = await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, arc_id, actors, rng_seed, visibility, payload)
            VALUES ($1, $2, 'director.grade_revise', 'director', 'director', $3, '{}', $4, 'public', $5::jsonb)
            RETURNING seq
            """,
            tick, sim_now, arc_id, tick,
            json.dumps({"target_seq": str(target_seq), "new_grade": new_grade, "reason": reason},
                       ensure_ascii=False),
        )
        await self._pool.execute(
            """
            INSERT INTO interventions (sim_time, level, arc_id, reason, payload, event_seq)
            VALUES ($1, 'L2', $2, $3, $4::jsonb, $5)
            """,
            sim_now, arc_id, f"K3 复核改判 e{target_seq} → {new_grade}",
            json.dumps({"action": "grade_revise", "target_seq": str(target_seq), "new_grade": new_grade},
                       ensure_ascii=False),
            seq,
        )
        log.info("grade_revise 落库：e%d → %s（%s）", target_seq, new_grade, reason)
        return seq

    # ---- prompt（挂 M2 模板机制；registry 缺省内联兜底） ------------------------------

    def _render_prompt(self, day: dt.date, candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
        summary = "\n".join(
            f"e{c['seq']} [{c['grade']}] {c['type']}: {(c.get('text_display') or '')[:40]}"
            for c in candidates)
        if self._registry is not None:
            _, _, messages = self._registry.render(
                "director_review", day=day.isoformat(), candidates=summary)
            return messages
        return [
            {"role": "system", "content": "你是世界编剧的终审编辑（K3 复核）。只输出 JSON。"},
            {"role": "user", "content": f"[复核日] {day.isoformat()}\n{candidates and summary}"},
        ]

    # ---- 排队标记（04 §8.1 任务排队延后） ---------------------------------------------

    async def _mark_pending(self, day: dt.date, err: str) -> None:
        pending = list(await self._get(PENDING_KEY, []) or [])
        pending.append({"day": day.isoformat(), "error": err[:200], "at": self._clock.now_sim().isoformat()})
        await self._set(PENDING_KEY, pending)

    async def _clear_pending(self, day: dt.date) -> None:
        pending = [p for p in (await self._get(PENDING_KEY, []) or []) if p.get("day") != day.isoformat()]
        await self._set(PENDING_KEY, pending)

    async def _get(self, key: str, default: Any = None) -> Any:
        row = await self._pool.fetchrow("SELECT value FROM world_state WHERE key=$1", key)
        if row is None:
            return default
        v = row["value"]
        return json.loads(v) if isinstance(v, str) else v

    async def _set(self, key: str, value: Any) -> None:
        await self._pool.execute(
            """
            INSERT INTO world_state (key, value, updated_tick) VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_tick=$3, updated_at=now()
            """, key, json.dumps(value, ensure_ascii=False), self._clock.current_tick)
