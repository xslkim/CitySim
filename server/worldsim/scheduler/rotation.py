"""明星层轮换与事件驱动即时升格（02 T-LOD-02 路径二段 / T-LOD-03 路径一段；04 §4.2/§4.3、06 §1.2）。

**路径二：事件驱动即时升格（本模块 `EventDrivenLOD`，不受迟滞约束、不占每日 churn 名额）**：
- 触发集（裁决协程内当 tick 生效）：①成为他人动作对象（动作清单见 04 §4.2 路径二①）；
  ②成为 `visibility='public'` 或 A 级候选事件的非发起参与者/直接目击；③编剧点名（M1 接口
  `nominate`，M3 接 L1/L2）。升格目标层：①② background→secondary；③直达 star（04 §4.2）。
- 落库：当 tick 写 `agent.promoted`（`trigger='system'`，`payload.reason='event_driven'/
  'director_nominated'`，`payload.caused_by` = 裸 seq 数字字符串，00 §4 红线 3），本 tick 即按新层响应。
- 回落：事件驱动升入 secondary 者连续 2 模拟日零新交互 → `agent.demoted`（`reason='cooldown'`）。
- 硬上限：secondary ≤16 人；超额按 LRU（键 = 最近一次出现在事件 `actors` 的 sim_time，
  调度器随事件流维护）当 tick 挤出回 background，写 `agent.demoted`（`reason='lru_evict'`）。

数值载体：`config/models.yaml` `thresholds.lod` 段（00 §7 DoD 4/6；16 人上限/2 模拟日回落为
04 §4.2 镜像）。目标提取口径（工程默认，02 文档偏差表登记）：动作对象 = payload `to`/`target` 键，
或 `participants` 减发起者（source='agent:<id>'）；直接目击 = dialogue 域 `witnesses[]`。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_AGENT_RE = re.compile(r"^A(0[1-9]|[1-3][0-9]|40)$")  # agent id TEXT 形态（00 §4 红线 1）

# 04 §4.2 路径二① 触发动作清单（成为其 target 即升格）
PROMOTE_ACTION_TYPES = (
    "dialogue.chat", "social.invite", "dialogue.argue", "social.give_gift", "social.help",
    "social.borrow_money", "social.repay_money", "dialogue.confess", "dialogue.apologize",
)


def extract_targets(event: dict[str, Any]) -> list[str]:
    """从事件行提取"非发起参与者/动作对象/直接目击"（确定性序：id 升序）。

    发起者 = source 'agent:<id>'；对象 = payload.to / payload.target / participants 减发起者；
    直接目击 = payload.witnesses[]（dialogue 域注册键，06 §1.2）。
    """
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    source = str(event.get("source") or "")
    initiator = source.removeprefix("agent:") if source.startswith("agent:") else None
    targets: set[str] = set()
    for key in ("to", "target"):
        v = payload.get(key)
        if isinstance(v, str) and _AGENT_RE.fullmatch(v) and v != initiator:
            targets.add(v)
    for v in payload.get("participants") or []:
        if isinstance(v, str) and v != initiator:
            targets.add(v)
    if event.get("visibility") == "public" or (event.get("ui") or {}).get("grade") == "A":
        for v in payload.get("witnesses") or []:
            if isinstance(v, str) and v != initiator:
                targets.add(v)
    return sorted(targets)


class EventDrivenLOD:
    """事件驱动升格/挤出/回落。`pool` = asyncpg pool；`cfg` = models.yaml thresholds.lod 段。"""

    def __init__(self, pool: Any, cfg: dict[str, Any] | None = None) -> None:
        self._pool = pool
        cfg = cfg or {}
        self._cap = int(cfg.get("secondary_cap", 16))
        self._cooldown_days = int(cfg.get("cooldown_demote_sim_days", 2))

    # ---- 升格（当 tick 生效） -------------------------------------------------

    async def on_event(self, *, tick: int, sim_now: dt.datetime, event: dict[str, Any]) -> list[int]:
        """事件落库后调用：命中触发集的背景层 agent 当 tick 升格 secondary。返回新事件 seq 列表。"""
        if event.get("type") not in PROMOTE_ACTION_TYPES and event.get("visibility") != "public":
            return []
        seqs: list[int] = []
        for target in extract_targets(event):
            tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", target)
            if tier != "background":
                continue  # 已在 secondary/star 不动作（04 §4.2）
            seqs.append(
                await self._promote(target, to_tier="secondary", reason="event_driven",
                                    caused_by=str(event["seq"]), tick=tick, sim_now=sim_now)
            )
        if seqs:
            seqs.extend(await self.enforce_cap(tick=tick, sim_now=sim_now, caused_by=str(event["seq"])))
        return seqs

    async def nominate(self, *, agent_id: str, tick: int, sim_now: dt.datetime, caused_by: str | None = None) -> int | None:
        """编剧点名：直达 star（04 §4.2 路径二③；M1 留接口，M3 接 L1/L2）。已升/在星返回 None。"""
        tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", agent_id)
        if tier == "star":
            return None
        return await self._promote(agent_id, to_tier="star", reason="director_nominated",
                                   caused_by=caused_by, tick=tick, sim_now=sim_now)

    async def _promote(
        self, agent_id: str, *, to_tier: str, reason: str, caused_by: str | None, tick: int, sim_now: dt.datetime,
    ) -> int:
        from_tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", agent_id)
        await self._pool.execute("UPDATE agents SET cognition_tier=$2 WHERE id=$1", agent_id, to_tier)
        payload: dict[str, Any] = {"from_tier": from_tier, "to_tier": to_tier, "reason": reason}
        if caused_by is not None:
            if not caused_by.isdigit():
                raise ValueError(f"caused_by 必须为裸 seq 数字字符串，得到 {caused_by!r}（00 §4 红线 3）")
            payload["caused_by"] = caused_by
        seq = await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES ($1, $2, 'agent.promoted', 'system', 'system', $3, 'public', $4::jsonb)
            RETURNING seq
            """,
            tick, sim_now, [agent_id], json.dumps(payload, ensure_ascii=False),
        )
        log.info("升格：%s %s → %s（%s）", agent_id, from_tier, to_tier, reason)
        return int(seq)

    # ---- 硬上限 + LRU 挤出（当 tick 生效） ---------------------------------------

    async def enforce_cap(self, *, tick: int, sim_now: dt.datetime, caused_by: str | None = None) -> list[int]:
        """secondary 超员按 LRU 挤出回 background（`reason='lru_evict'`，04 §4.2 N-P1-7）。

        LRU 键 = 该 agent 最近一次出现在事件 `actors` 的 sim_time（调度器随事件流维护；
        从未出现 = 最久，NULLS FIRST）。同键并列按 id 升序（确定性）。
        """
        seqs: list[int] = []
        while True:
            count = await self._pool.fetchval("SELECT count(*) FROM agents WHERE cognition_tier='secondary'")
            if count <= self._cap:
                return seqs
            row = await self._pool.fetchrow(
                """
                SELECT a.id FROM agents a
                LEFT JOIN LATERAL (
                  SELECT max(e.sim_time) AS last_seen FROM events e WHERE a.id = ANY(e.actors)
                ) s ON true
                WHERE a.cognition_tier='secondary'
                ORDER BY s.last_seen ASC NULLS FIRST, a.id
                LIMIT 1
                """,
            )
            if row is None:
                return seqs
            seqs.append(await self._demote(row["id"], to_tier="background", reason="lru_evict",
                                           caused_by=caused_by, tick=tick, sim_now=sim_now))

    # ---- 回落（连续 2 模拟日零新交互；日界/周界批量驱动） -------------------------

    async def demote_inactive(self, *, tick: int, sim_now: dt.datetime) -> list[int]:
        """事件驱动升入 secondary 且连续 2 模拟日零新交互者回落 background（`reason='cooldown'`）。

        "事件驱动升入"判定 = 最近一次 `agent.promoted` 的 payload.reason='event_driven'（事件流推导，
        可回放）；零新交互 = 最近出现于事件 actors 的 sim_time < sim_now − 2 模拟日（含从未出现）。
        """
        rows = await self._pool.fetch(
            """
            SELECT a.id, s.last_seen FROM agents a
            LEFT JOIN LATERAL (
              SELECT max(e.sim_time) AS last_seen FROM events e WHERE a.id = ANY(e.actors)
            ) s ON true
            WHERE a.cognition_tier='secondary'
            ORDER BY a.id
            """,
        )
        cutoff = sim_now - dt.timedelta(days=self._cooldown_days)
        seqs: list[int] = []
        for r in rows:
            if r["last_seen"] is not None and r["last_seen"] >= cutoff:
                continue
            last_reason = await self._pool.fetchval(
                """
                SELECT payload->>'reason' FROM events
                WHERE type='agent.promoted' AND $1 = ANY(actors) ORDER BY seq DESC LIMIT 1
                """,
                r["id"],
            )
            if last_reason != "event_driven":
                continue
            seqs.append(await self._demote(r["id"], to_tier="background", reason="cooldown",
                                           caused_by=None, tick=tick, sim_now=sim_now))
        return seqs

    async def _demote(
        self, agent_id: str, *, to_tier: str, reason: str, caused_by: str | None, tick: int, sim_now: dt.datetime,
    ) -> int:
        from_tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", agent_id)
        await self._pool.execute("UPDATE agents SET cognition_tier=$2 WHERE id=$1", agent_id, to_tier)
        payload: dict[str, Any] = {"from_tier": from_tier, "to_tier": to_tier, "reason": reason}
        if caused_by is not None:
            if not caused_by.isdigit():
                raise ValueError(f"caused_by 必须为裸 seq 数字字符串，得到 {caused_by!r}（00 §4 红线 3）")
            payload["caused_by"] = caused_by
        seq = await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES ($1, $2, 'agent.demoted', 'system', 'system', $3, 'public', $4::jsonb)
            RETURNING seq
            """,
            tick, sim_now, [agent_id], json.dumps(payload, ensure_ascii=False),
        )
        log.info("降格：%s %s → %s（%s）", agent_id, from_tier, to_tier, reason)
        return int(seq)


# ==============================================================================
# 路径一：基尼驱动明星轮换（唯一受迟滞约束的路径，只决定明星层 8 席位，04 §4.2）
# ==============================================================================


def gini_coefficient(counts: list[float]) -> float:
    """基尼系数（04 §10.2 口径：每模拟周各角色事件数分布）。"""
    vals = sorted(float(c) for c in counts)
    n = len(vals)
    if n == 0:
        return 0.0
    total = sum(vals)
    if total == 0:
        return 0.0
    acc = sum((2 * (i + 1) - n - 1) * v for i, v in enumerate(vals))
    return acc / (n * total)


class StarRotation:
    """路径一每日轮换（04 §4.2 rotation_tick 伪码逐行）+ 升星追赶反思（04 §4.3）。

    数值载体：models.yaml `thresholds.rotation`（滞回带 PROMOTE_IN/DEMOTE_OUT）+
    `thresholds.lod`（star_count/daily_churn_cap/consecutive_over/under/weights/gini 联动阈）。
    权重 w_* 为设计未定值的等权初值（02 文档 D4）；arc/编剧点名因子 M1 无数据源恒 0（接口就位）。
    """

    def __init__(
        self,
        pool: Any,
        gateway: Any = None,
        *,
        thresholds_rotation: dict[str, Any] | None = None,
        thresholds_lod: dict[str, Any] | None = None,
        reflector: Any = None,
        event_lod: EventDrivenLOD | None = None,
    ) -> None:
        self._pool = pool
        self._gw = gateway
        rot = thresholds_rotation or {}
        lod = thresholds_lod or {}
        self._promote_in = float(rot.get("promote_in", 0.62))
        self._demote_out = float(rot.get("demote_out", 0.45))
        self._star_count = int(lod.get("star_count", 8))
        self._churn_cap = int(lod.get("daily_churn_cap", 2))
        self._need_over = int(lod.get("consecutive_over", 2))
        self._need_under = int(lod.get("consecutive_under", 3))
        self._weights = dict(lod.get("weights", {"w_arc": 1.0, "w_dir": 1.0, "w_int": 1.0, "w_cold": 1.0}))
        self._gini_above = float(lod.get("gini_cold_bonus_above", 0.45))
        self._reflector = reflector
        self._event_lod = event_lod or EventDrivenLOD(pool, lod)

    # ---- 评分（04 §4.2 四因子） -------------------------------------------------

    async def _scores(self, sim_now: dt.datetime) -> tuple[dict[str, float], dict[str, dict[str, float]], float]:
        """score = w_arc·arc + w_dir·编剧点名 + w_int·近 2 模拟日交互 + w_cold·cold_bonus（归一化到 0~1）。"""
        agents = await self._pool.fetch("SELECT id FROM agents ORDER BY id")
        ids = [r["id"] for r in agents]
        week_start = sim_now - dt.timedelta(days=7)
        rows = await self._pool.fetch(
            """
            SELECT a.id, count(e.seq) AS n FROM agents a
            LEFT JOIN LATERAL (
              SELECT e.seq FROM events e WHERE a.id = ANY(e.actors) AND e.sim_time > $1
            ) e ON true GROUP BY a.id
            """,
            week_start,
        )
        counts = {r["id"]: float(r["n"]) for r in rows}
        gini = gini_coefficient(list(counts.values()))
        rows2 = await self._pool.fetch(
            """
            SELECT a.id, count(e.seq) AS n FROM agents a
            LEFT JOIN LATERAL (
              SELECT e.seq FROM events e WHERE a.id = ANY(e.actors) AND e.sim_time > $1
            ) e ON true GROUP BY a.id
            """,
            sim_now - dt.timedelta(days=2),
        )
        recent = {r["id"]: float(r["n"]) for r in rows2}
        max_recent_raw = max(recent.values())
        max_count_raw = max(counts.values())
        # 基尼超标时 cold_bonus 权重上调（04 §4.2：指标与架构同向；阈值 = 01 §9 预警线，models.yaml 镜像）
        cold_factor = 2.0 if gini > self._gini_above else 1.0
        w = self._weights
        scores: dict[str, float] = {}
        parts: dict[str, dict[str, float]] = {}
        for aid in ids:
            f_int = recent[aid] / max_recent_raw if max_recent_raw > 0 else 0.0   # 近 2 模拟日交互（归一化）
            f_cold = (max_count_raw - counts[aid]) / max_count_raw if max_count_raw > 0 else 0.0  # 冷门度
            breakdown = {"arc": 0.0, "dir": 0.0, "int": f_int, "cold": f_cold * cold_factor}
            scores[aid] = (float(w.get("w_arc", 1.0)) * breakdown["arc"]
                           + float(w.get("w_dir", 1.0)) * breakdown["dir"]
                           + float(w.get("w_int", 1.0)) * breakdown["int"]
                           + float(w.get("w_cold", 1.0)) * breakdown["cold"])
            parts[aid] = breakdown
        return scores, parts, gini

    # ---- 每日轮换（04 §4.2 rotation_tick） --------------------------------------

    async def rotation_tick(self, *, tick: int, sim_now: dt.datetime) -> dict[str, Any]:
        """每模拟日 1 次：滞回带进出 + churn ≤2 + star 数恒等于席位 + 升星追赶反思。"""
        scores, parts, gini = await self._scores(sim_now)
        rows = await self._pool.fetch(
            "SELECT id, cognition_tier, consecutive_over, consecutive_under FROM agents ORDER BY id",
        )
        churn = 0
        promoted: list[str] = []
        demoted: list[str] = []
        tiers = {r["id"]: r["cognition_tier"] for r in rows}

        # 迟滞计数更新（只管路径一，04 §5.2 agents 列注释）
        for r in rows:
            aid = r["id"]
            over = r["consecutive_over"] + 1 if scores[aid] > self._promote_in else 0
            under = r["consecutive_under"] + 1 if scores[aid] < self._demote_out else 0
            await self._pool.execute(
                "UPDATE agents SET consecutive_over=$2, consecutive_under=$3 WHERE id=$1", aid, over, under,
            )
            tiers[aid] = r["cognition_tier"]

        # 路径一 = 席位互换模型：降格（star → secondary，连续 under 达标）与补位/升格配对进行，
        # 两者共享每日 churn 预算（04 §4.2：enforce star_count==8 与 daily_churn≤2 同约束）；
        # 禁止 star 直达 background（04 §4.2 矩阵）；无补位候选时不降（M1 全员 star 世界恒真）。
        demote_ready = sorted(
            (r for r in rows
             if r["cognition_tier"] == "star"
             and scores[r["id"]] < self._demote_out and r["consecutive_under"] + 1 >= self._need_under),
            key=lambda r: (scores[r["id"]], r["id"]),
        )
        promote_ready = sorted(
            (r for r in rows
             if tiers[r["id"]] != "star"
             and scores[r["id"]] > self._promote_in and r["consecutive_over"] + 1 >= self._need_over),
            key=lambda r: (-scores[r["id"]], r["id"]),
        )

        def _top_non_star() -> str | None:
            rest = sorted((aid for aid, t in tiers.items() if t != "star"), key=lambda a: (-scores[a], a))
            return rest[0] if rest else None

        for r in demote_ready:
            aid = r["id"]
            fill = promote_ready[0]["id"] if promote_ready else _top_non_star()
            if fill is None or churn + 2 > self._churn_cap:
                continue  # 预算/候选不足，顺延次日（迟滞语义优先于当日对齐席位）
            await self._demote_path1(aid, tick=tick, sim_now=sim_now, score=scores[aid],
                                     breakdown=parts[aid], gini=gini)
            tiers[aid] = "secondary"
            demoted.append(aid)
            churn += 1
            if promote_ready and promote_ready[0]["id"] == fill:
                promote_ready.pop(0)
            await self._promote_path1(fill, tick=tick, sim_now=sim_now, score=scores[fill],
                                      breakdown=parts[fill], gini=gini)
            tiers[fill] = "star"
            promoted.append(fill)
            churn += 1

        # enforce：超员末位降格 / 缺员补位（04 §4.2；受 churn 上限约束，超额顺延次日）
        while sum(1 for t in tiers.values() if t == "star") > self._star_count and churn < self._churn_cap:
            stars = sorted((aid for aid, t in tiers.items() if t == "star"), key=lambda a: (scores[a], a))
            aid = stars[0]
            await self._demote_path1(aid, tick=tick, sim_now=sim_now, score=scores[aid],
                                     breakdown=parts[aid], gini=gini)
            tiers[aid] = "secondary"
            demoted.append(aid)
            churn += 1
        while sum(1 for t in tiers.values() if t == "star") < self._star_count and churn < self._churn_cap:
            if promote_ready:
                aid = promote_ready.pop(0)["id"]
            else:
                aid = _top_non_star()
                if aid is None:
                    break
            await self._promote_path1(aid, tick=tick, sim_now=sim_now, score=scores[aid],
                                      breakdown=parts[aid], gini=gini)
            tiers[aid] = "star"
            promoted.append(aid)
            churn += 1

        # 升星追赶反思（04 §4.3）：最近 1 条日摘要 + 配额检索一次 → 当前计划与心境
        for aid in promoted:
            await self._catchup_reflection(aid, tick=tick, sim_now=sim_now)
        return {"gini": gini, "promoted": promoted, "demoted": demoted, "churn": churn}

    async def _promote_path1(
        self, agent_id: str, *, tick: int, sim_now: dt.datetime, score: float,
        breakdown: dict[str, float], gini: float,
    ) -> int:
        """路径一升格事件：payload 含 score 分解与 gini（internal 键标注，06 §1.2 口径）。"""
        from_tier = await self._pool.fetchval("SELECT cognition_tier FROM agents WHERE id=$1", agent_id)
        await self._pool.execute(
            "UPDATE agents SET cognition_tier='star', consecutive_over=0, consecutive_under=0 WHERE id=$1", agent_id,
        )
        return await self._write_rotation_event(
            "agent.promoted", agent_id, from_tier=str(from_tier), to_tier="star",
            tick=tick, sim_now=sim_now, score=score, breakdown=breakdown, gini=gini,
        )

    async def _demote_path1(
        self, agent_id: str, *, tick: int, sim_now: dt.datetime, score: float,
        breakdown: dict[str, float], gini: float,
    ) -> int:
        """路径一降格：落入 secondary 同样受 ≤16 上限约束（先 LRU 挤出再落入，04 §4.2）。"""
        await self._event_lod.enforce_cap(tick=tick, sim_now=sim_now)
        await self._pool.execute(
            "UPDATE agents SET cognition_tier='secondary', consecutive_over=0, consecutive_under=0 WHERE id=$1", agent_id,
        )
        return await self._write_rotation_event(
            "agent.demoted", agent_id, from_tier="star", to_tier="secondary",
            tick=tick, sim_now=sim_now, score=score, breakdown=breakdown, gini=gini,
        )

    async def _write_rotation_event(
        self, type_: str, agent_id: str, *, from_tier: str | None, to_tier: str,
        tick: int, sim_now: dt.datetime, score: float, breakdown: dict[str, float], gini: float,
    ) -> int:
        if from_tier is None:
            raise ValueError("from_tier 由调用方捕获（UPDATE 前快照），不得缺省")
        payload = {
            "from_tier": from_tier, "to_tier": to_tier,
            "score": {"total": round(score, 4), **{k: round(v, 4) for k, v in breakdown.items()}},
            "gini": round(gini, 4),
        }
        seq = await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, visibility, payload)
            VALUES ($1, $2, $3, 'system', 'system', $4, 'public', $5::jsonb)
            RETURNING seq
            """,
            tick, sim_now, type_, [agent_id], json.dumps(payload, ensure_ascii=False),
        )
        log.info("路径一%s：%s → %s（score=%.3f gini=%.3f）",
                 "升格" if type_ == "agent.promoted" else "降格", agent_id, to_tier, score, gini)
        return int(seq)

    # ---- 升格衔接：追赶反思（04 §4.3） ------------------------------------------

    async def _catchup_reflection(self, agent_id: str, *, tick: int, sim_now: dt.datetime) -> None:
        """升入明星层立即跑追赶反思：最近 1 条日摘要 + 配额检索一次 → 当前计划与心境。

        产出：1 条 kind='reflection'（心境，重要性 7~9 档，经 T-MEM-02 挂接写双通道）
        + 1 条 kind='plan'（当前计划，首个明星层认知循环初始上下文）。
        """
        summary = await self._pool.fetchval(
            """
            SELECT content FROM memories
            WHERE agent_id=$1 AND kind='summary' AND NOT archived ORDER BY sim_time DESC LIMIT 1
            """,
            agent_id,
        )
        recent = await self._pool.fetch(
            """
            SELECT content FROM memories
            WHERE agent_id=$1 AND NOT archived ORDER BY sim_time DESC LIMIT 10
            """,
            agent_id,
        )
        if self._gw is None:
            insights = [f"追赶反思：{summary or '（无日摘要）'}", "当前计划：照常推进本周目标"]
        else:
            mem_lines = "\n".join(f"- {r['content']}" for r in recent) or "- （无）"
            result = await self._gw.chat(
                "reflection",
                [
                    {"role": "system", "content": "你是角色扮演引擎。基于日摘要与近期记忆生成当前心境与计划。只输出 JSON：{\"insights\": [...]}。"},
                    {"role": "user", "content": f"最近日摘要：{summary or '（无）'}\n近期记忆：\n{mem_lines}"},
                ],
                self._gw.gen_params("reflection") if hasattr(self._gw, "gen_params") else None,
                seed=tick, agent_id=agent_id, sim_time=sim_now,
            )
            try:
                insights = [str(x)[:80] for x in json.loads(result.text).get("insights", [])][:3]
            except (ValueError, TypeError):
                insights = []
            if not insights:
                return
        if self._reflector is not None:
            await self._reflector.write_generated_reflection(
                agent_id, f"升入镜头前沿的心境：{insights[0]}", importance=7, sim_now=sim_now, rng_seed=tick,
            )
        else:
            from ..memory.store import insert_memory

            await insert_memory(self._pool, self._gw, agent_id=agent_id, sim_time=sim_now, kind="reflection",
                                content=f"升入镜头前沿的心境：{insights[0]}", importance=7, rng_seed=tick)
        if len(insights) > 1 and self._gw is not None:
            from ..memory.store import insert_memory

            await insert_memory(self._pool, self._gw, agent_id=agent_id, sim_time=sim_now, kind="plan",
                                content=f"当前计划：{insights[-1]}", importance=5, rng_seed=tick)
