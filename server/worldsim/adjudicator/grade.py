"""grade 自动打分（`ui.grade` 初值，02 T-ADJ-07 唯一实现；04 §6.6、01 §6.4、06 §2）。

**grade 初值打分唯一实现归本模块**（评审 R1 §A.6）：管道 step5 落库**同事务**完成，
判定结果随 INSERT 写入 `ui.grade`；事件行永不 UPDATE（04 §5.2 append-only），编剧复核
`director.grade_revise` 归 M3（04 文档 T-DIR-04/05），本模块仅预留有效 grade 读取接口。

4 条可程序化规则（01 §6.4/04 §6.6）：
- R1 卷入 ≥2 名 Agent：`actors` + `witnesses` + gossip `payload.cites` 波及者并集计数
  （无 `witnesses` 键的事件类型按空集处理——02 文档 D5；cites 波及者 = 被引用记忆的
  `source_event_seq` 事件的 actors）；
- R2 至少一条关系边 |Δaffinity| ≥5 或有标签增删（本事件结算产出、落 `relation.changed` 口径）；
- R3 至少一名参与者情绪摆动 ≥15（step5 情绪结算增量，落 `state.needs_delta` 口径）；
- R4 因果链完整：`payload.caused_by` 非空，或结算产生了可预期后续（新约定/新冷却/新意图入队）。

命中数定级（≥3/2/0~1 → A/B/C，04 §6.6）。

**落库顺序工程口径（02 文档偏差表 D30）**：append-only 约束下 `ui.grade` 必须随 INSERT 写入，
而 R2/R3 依据本事件的结算结果——结算方（T-ADJ-03 校验器/T-ADJ-04 对话引擎）在落库**前**按
结算矩阵与满足量配置预申报信号（`rel_hit`/`mood_hit`/`followups`；数值只由系统改，与随后实际
施加并经聚合事件落库的变更为同一数据源），本模块不二次读库判定。这与 04 §6.6"同事务即时判定"
同语义，是 append-only 下唯一自洽顺序。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

GRADE_LEVELS = ("A", "B", "C")
# 以下为 01 §6.4 判定的工程默认镜像；配置载体 = world.yaml `director.grade` 段（04 T-DIR-04
# 追加，唯一配置源），Grader(thresholds=...) 注入后优先生效，缺省回落本常量。
REL_HIT_AFFINITY_MIN = 5    # R2：|Δaffinity| ≥5 或标签增删（04 §6.6 规则②）
MOOD_HIT_MIN = 15.0         # R3：情绪摆动 ≥15（04 §6.6 规则③）
R1_MIN_ACTORS = 2           # R1：卷入 ≥2 名 Agent（04 §6.6 规则①）
LEVEL_MAP = {"A": 3, "B": 2}  # 命中数定级（≥3/2/0~1 → A/B/C，04 §6.6）


class Grader:
    """grade 初值打分器。`pool` 仅用于 R1 的 cites 波及者解析（读，不写）。

    `thresholds` = world.yaml `director.grade` 段（T-DIR-04 配置化；None = 工程默认镜像）。
    预申报方（validators/dialogue）经 `grader.r2_min`/`grader.r3_min` 读同一份阈值，
    保证 R2/R3 判定与本模块定级同源（04 §6.6）。
    """

    def __init__(self, pool: Any, thresholds: dict[str, Any] | None = None) -> None:
        self._pool = pool
        th = thresholds or {}
        self.r1_min = int(th.get("r1_min_actors", R1_MIN_ACTORS))
        self.r2_min = int(th.get("r2_min_abs_affinity", REL_HIT_AFFINITY_MIN))
        self.r3_min = float(th.get("r3_min_mood_swing", MOOD_HIT_MIN))
        lm = th.get("level_map") or LEVEL_MAP
        self._level_a = int(lm.get("A", LEVEL_MAP["A"]))
        self._level_b = int(lm.get("B", LEVEL_MAP["B"]))

    def level_of(self, hits: int) -> str:
        """命中数定级（映射读配置段，01 §6.4）。"""
        return "A" if hits >= self._level_a else ("B" if hits >= self._level_b else "C")

    async def grade(
        self,
        *,
        type_: str,
        actors: list[str],
        payload: dict[str, Any],
        rel_hit: bool = False,
        mood_hit: bool = False,
        followups: bool = False,
        sim_now: dt.datetime | None = None,
    ) -> str:
        """4 条规则命中数定级。信号口径见模块头（D30）。返回 'A'/'B'/'C'。"""
        involved = await self.involved_actors(actors=actors, payload=payload)
        r1 = len(involved) >= self.r1_min
        r4 = bool(payload.get("caused_by")) or followups
        hits = int(r1) + int(rel_hit) + int(mood_hit) + int(r4)
        grade = self.level_of(hits)
        log.debug("grade 初值：type=%s hits=[R1=%s R2=%s R3=%s R4=%s] → %s", type_, r1, rel_hit, mood_hit, r4, grade)
        return grade

    async def involved_actors(self, *, actors: list[str], payload: dict[str, Any]) -> set[str]:
        """R1 并集：actors + witnesses（无键按空集，D5）+ gossip cites 波及者。"""
        involved = {a for a in actors if isinstance(a, str)}
        involved.update(w for w in (payload.get("witnesses") or []) if isinstance(w, str))
        cites = [int(c) for c in (payload.get("cites") or []) if str(c).isdigit()]
        if cites:
            rows = await self._pool.fetch(
                """
                SELECT e.actors FROM memories m JOIN events e ON e.seq = m.source_event_seq
                WHERE m.id = ANY($1::bigint[])
                """,
                cites,
            )
            for r in rows:
                involved.update(a for a in (r["actors"] or []) if isinstance(a, str))
        return involved


def level_of(hits: int) -> str:
    """命中数定级（≥3/2/0~1 → A/B/C，04 §6.6）。"""
    return "A" if hits >= 3 else ("B" if hits == 2 else "C")


async def effective_grade(pool: Any, seq: int) -> str | None:
    """有效 grade = `ui.grade` 初值 ⊕ 最新一条 `director.grade_revise.new_grade`（04 §6.6/06 §2）。

    M1 无复核事件（编剧归 M3），接口预留供 03/04/05 消费（时间轴 A 级过滤、健康度 A 级间隔等）。
    """
    revised = await pool.fetchval(
        """
        SELECT payload->>'new_grade' FROM events
        WHERE type='director.grade_revise' AND payload->>'target_seq' = $1
        ORDER BY seq DESC LIMIT 1
        """,
        str(int(seq)),
    )
    if revised in GRADE_LEVELS:
        return revised
    return await pool.fetchval("SELECT ui->>'grade' FROM events WHERE seq=$1", int(seq))
