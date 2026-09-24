"""背景 NPC 驻留规则引擎（校外住处节点组）（02 T-LOD-04；01 §1.6 三行表、06 §1.2 `agent.move`、00 §4 红线 11）。

- 节点组：每名公司 NPC（`room: null`，01 §2.2）一个抽象节点 `home.<agent_id>`（01 §1.6 节点定义行），
  非 tileset 场景、不占地图资产；租客 `move` 进入 `home.*` 由 T-ADJ-03 通用可达性校验拦截
  （本模块 `enterable_for_tenant` 是其消费面，01 §1.6 可达性行）。
- 驻留移位（01 §1.6 驻留时段行逐字）：工作日 18:30 → 移位 `home.<agent_id>`、次日 8:00 → 返岗公司节点、
  周末全天驻留；加班夜/团建事件期间为例外不移位。移位由调度器每 tick 规则评估触发
  （挂 T-LOD-01 时钟兜底排程，不占认知循环、**零 LLM 调用**——本模块无 gateway 依赖），
  落 `agent.move` 事件（payload `from`/`to`/`sim_cost_min` 逐字按 06 §1.2，系统结算 `trigger='system'`）。
- 赴约折算：校外 NPC 赴约移动按 30~45min 区间折算在途 `sim_cost_min`（01 §1.6 可达性行，
  world.yaml `offsite.npc_move_minutes` 镜像），区间内取数经 `events.rng_seed` 确定性采样（可回放）。
- 校外邀约地点限制：`invite_location_allowed` 供 T-REL-05 邀约可达性校验消费（01 §1.6 可达性行）。
- 全部判定只读 sim_time（00 §4 红线 11）。M1 口径：8 人世界全员租客、无校外 NPC 在册——
  引擎以合成夹具单测覆盖规则全集，真实 16 人启用随 40 人扩容（seed_40，M6 前收口）。

例外窗口（工程默认，02 文档偏差表登记）：`world.overtime`（排期 18:30~21:00，01 §1.6）/
`world.team_building`（19:00~22:00）事件参与人当晚 18:30 驻留移位豁免；事件以 sim_time 落窗判定。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
from typing import Any

log = logging.getLogger(__name__)

# 驻留时段（01 §1.6；world.yaml offsite.dwell.workday = "18:30~08:00(+1)"）
_WORKDAY_GO_HOME = (18, 30)
_WORKDAY_BACK = (8, 0)
# 例外事件类型（01 §1.6"加班夜/团建/外出事件除外"；外出事件 M3 起由世界 Agent 产出，类型名 06 §1.2）
_EXCEPTION_TYPES = ("world.overtime", "world.team_building")


def is_home_node(location_id: str | None) -> bool:
    return isinstance(location_id, str) and location_id.startswith("home.")


def is_external(location_id: str | None) -> bool:
    return isinstance(location_id, str) and location_id.startswith("ext.")


def enterable_for_tenant(location_id: str) -> bool:
    """租客可达性（01 §1.6 可达性行）：home.* 一律不可 move 进入（T-ADJ-03 通用校验消费）。"""
    return not is_home_node(location_id)


def _minute(hhmm: tuple[int, int]) -> int:
    return hhmm[0] * 60 + hhmm[1]


class ResidenceEngine:
    """驻留规则引擎。`pool` = asyncpg pool；`world` = world.yaml 字典。"""

    def __init__(self, pool: Any, world: dict[str, Any]) -> None:
        self._pool = pool
        offsite = (world or {}).get("locations", {}).get("offsite", {})
        lo, hi = (offsite.get("npc_move_minutes") or [30, 45])
        self._travel_range = (int(lo), int(hi))
        commons = (world or {}).get("locations", {}).get("apartment", {}).get("commons", [])
        self._apt_commons = {c["id"] for c in commons}
        # 部门中文名 → corp 节点 key（01 §1.3 / world.yaml company.departments）
        self._dept_node = {
            d["name"]: f"corp.{d['key']}"
            for d in (world or {}).get("company", {}).get("departments", [])
        }

    # ---- 纯规则（只读 sim_time） ------------------------------------------------

    def dwell_target(self, *, agent_id: str, department: str | None, sim_now: dt.datetime) -> str:
        """该时刻的期望位置：驻留时段 → `home.<agent_id>`；否则返岗公司节点（部门工位区）。"""
        if self.in_dwell_window(sim_now):
            return f"home.{agent_id}"
        return self._dept_node.get(department or "", "corp.reception")

    @staticmethod
    def in_dwell_window(sim_now: dt.datetime) -> bool:
        """驻留时段判定：工作日 18:30~次日 8:00；周末全天（01 §1.6 驻留时段行）。"""
        if sim_now.weekday() >= 5:
            return True  # 周末全天驻留
        m = sim_now.hour * 60 + sim_now.minute
        return m >= _minute(_WORKDAY_GO_HOME) or m < _minute(_WORKDAY_BACK)

    def travel_cost_min(self, agent_id: str, rng_seed: int) -> int:
        """赴约/通勤在途折算：30~45min 区间内取数，rng_seed 确定性采样（同种子可复算）。"""
        lo, hi = self._travel_range
        rng = random.Random(f"residence-travel:{rng_seed}:{agent_id}")
        return rng.randint(lo, hi)

    def invite_location_allowed(self, agent_id: str, location_id: str, sim_now: dt.datetime) -> bool:
        """校外邀约地点限制（01 §1.6 可达性行）：驻留时段内约定地点限外部场所（ext.*）或公寓公共区。

        非驻留时段不限制（白天在公司正常社交）；本判定对租客恒 True（仅约束校外 NPC）。
        调用方须先确认 agent_id 为校外 NPC（`room_no IS NULL`），或经 `is_offsite_agent` 异步版。
        """
        if not self.in_dwell_window(sim_now):
            return True
        return is_external(location_id) or location_id in self._apt_commons

    async def is_offsite_agent(self, agent_id: str) -> bool:
        """room_no IS NULL = 公司 NPC（校外住处节点组，01 §2.2/§1.6）。"""
        return await self._pool.fetchval("SELECT room_no IS NULL FROM agents WHERE id=$1", agent_id)

    async def invite_location_check(self, *, agent_id: str, location_id: str, sim_now: dt.datetime) -> bool:
        """T-REL-05 消费面（异步整判）：非校外 NPC 恒放行；校外 NPC 驻留时段限外部场所/公寓公共区。"""
        if not await self.is_offsite_agent(agent_id):
            return True
        return self.invite_location_allowed(agent_id, location_id, sim_now)

    # ---- 每 tick 规则评估（挂 T-LOD-01 时钟兜底排程；零 LLM） ------------------------

    async def evaluate(self, *, tick: int, sim_now: dt.datetime, rng_seed: int) -> list[int]:
        """对全部校外 NPC 评估驻留移位：期望位置 ≠ 当前位置且无例外 → 落 `agent.move`（trigger='system'）。

        返回新事件 seq 列表。全程不触 LLM（规则驱动，02 T-LOD-04 验收 2 口径）。
        """
        rows = await self._pool.fetch(
            "SELECT id, department, position FROM agents WHERE room_no IS NULL ORDER BY id",
        )
        seqs: list[int] = []
        for r in rows:
            if await self._in_exception(r["id"], sim_now):
                continue  # 加班夜/团建事件期间例外不移位（01 §1.6）
            target = self.dwell_target(agent_id=r["id"], department=r["department"], sim_now=sim_now)
            if r["position"] == target:
                continue
            cost = self.travel_cost_min(r["id"], rng_seed)
            await self._pool.execute("UPDATE agents SET position=$2 WHERE id=$1", r["id"], target)
            seq = await self._pool.fetchval(
                """
                INSERT INTO events (tick, sim_time, type, source, trigger, location_id, actors, rng_seed, visibility, payload)
                VALUES ($1, $2, 'agent.move', 'system', 'system', $3, $4, $5, 'public', $6::jsonb)
                RETURNING seq
                """,
                tick, sim_now, target, [r["id"]], rng_seed,
                json.dumps({"from": r["position"], "to": target, "sim_cost_min": cost}, ensure_ascii=False),
            )
            seqs.append(int(seq))
            log.info("驻留移位：%s %s → %s（cost %d min）", r["id"], r["position"], target, cost)
        return seqs

    async def _in_exception(self, agent_id: str, sim_now: dt.datetime) -> bool:
        """加班夜/团建例外：当日晚 18:00 起存在含该 NPC 的 world.overtime/team_building 事件。"""
        evening = sim_now.replace(hour=18, minute=0, second=0, microsecond=0)
        if sim_now < evening:
            evening -= dt.timedelta(days=1)  # 凌晨判定窗归前一晚
        return bool(
            await self._pool.fetchval(
                """
                SELECT count(*) FROM events
                WHERE type = ANY($3::text[]) AND sim_time >= $2 AND sim_time <= $1
                  AND ($4 = ANY(actors) OR payload->'participants' ? $4)
                """,
                sim_now, evening, list(_EXCEPTION_TYPES), agent_id,
            )
        )
