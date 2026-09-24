"""反思系统（02 T-MEM-02；04 §7.2 阈值/兜底/双通道、04 §6.1 step6、06 §1.2 display-only、01 §4.1 反思链口径）。

- 每 agent 维护 `importance_acc`（落 `agents.mood` JSON 持久化，与 T-REL-06 topic_cooldowns 同例）；
  累计 ≥20（models.yaml `thresholds.reflection.importance_acc`）触发**深度反思**：窗口内记忆 →
  明星层模型（task_type='reflection'）2~3 条洞察（`kind='reflection'`，重要性 7~9）。
- **降速读取点**：触发阈值运行时经 `ThrottleState` 读取（判级器与 `system.llm.throttle` 事件唯一归
  03 T-LLM-08 breaker；降速动作消费接线归 08 T-OPS-02；降速档阈值 20→35 口径见 04 §8.4 ④）。
  M1 交付 `NullThrottleState`（恒回默认阈），T-OPS-02 以同协议替换注入。
- **每日兜底**：每模拟日 23:00 批量反思点（`run_due_daily_fallbacks`，按 agent 日幂等）强制一次
  廉价摘要式反思（次要层档位模型，M1 取 bgsummary 摘要形态，工程口径 D19）。
- **双通道落库**（每次反思）：① memories 写 `kind='reflection'`（原文仅内部通道 content；
  content_display M1 直写，02 文档 D10）；② 同时落一条 `agent.reflection` display-only 事件
  （`visibility='public'`、`trigger='autonomous'`、payload 白名单仅 `text_display`，06 §1.2）；
  反思原文永不入事件 payload。
- `think` = 轻反思档：低重要性（1~3，管道 T-ADJ-02 已落）、不产 `agent.reflection` 事件；
  acc 只累计事件/目击/计划类记忆重要性，反思/摘要类不再回喂（防自激循环，工程口径 D19）。
- T-REL-03 挂接：`write_generated_reflection`（换策略=轻档不产事件 / 放弃=深度档 importance≥8 产事件，
  01 §3.3 + 01 §4.1 反思链口径）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Callable, Protocol

from .store import insert_memory

log = logging.getLogger(__name__)

ACC_KINDS = ("event", "projection", "plan")  # 计入 importance_acc 的记忆类（反思/摘要不回喂，D19）
DEEP_IMPORTANCE_BASE = 7                     # 深度反思洞察重要性区间 7~9（04 §7.2）
FALLBACK_IMPORTANCE = 4                      # 每日兜底摘要式反思重要性（工程默认，D19）
FALLBACK_HOUR = 23                           # 每日兜底时点 23:00（04 §7.2；或就寝时，M1 取 23:00 批量点）
LIGHT_REFLECTION_EVENT_MIN = 7               # 生成反思产 display 事件的重要性下限（深度档口径；轻档不产事件）


class ThrottleState(Protocol):
    """降速状态读取协议（判级唯一归 03 T-LLM-08 breaker；消费接线归 08 T-OPS-02）。"""

    def reflection_threshold(self, default: int) -> int:
        """当前生效的深度反思触发阈值（未降速返回 default=20；降速档返回 35，04 §8.4 ④）。"""
        ...

    def dialogue_daily_cap(self, default: int) -> int:
        """当前生效的每模拟日对话场次上限（未降速返回 default；降速档降为 60%，04 §8.4 ③）。"""
        ...


class NullThrottleState:
    """M1 默认：未接降速（T-OPS-02 以同协议替换注入）。"""

    def reflection_threshold(self, default: int) -> int:
        return default

    def dialogue_daily_cap(self, default: int) -> int:
        return default


class Reflector:
    """反思引擎。`pool` = asyncpg pool；`gateway` = LLM 网关；`tick_of` = sim_time→tick 换算（clock 注入）。"""

    def __init__(
        self,
        pool: Any,
        gateway: Any,
        *,
        threshold: int = 20,
        throttle: ThrottleState | None = None,
        tick_of: Callable[[dt.datetime], int] | None = None,
    ) -> None:
        self._pool = pool
        self._gw = gateway
        self._threshold = threshold
        self._throttle = throttle or NullThrottleState()
        self._tick_of = tick_of or (lambda _sim: 0)

    # ---- 管道 step6 挂接（reflect= 签名：(agent_id, decision)） ----------------------

    async def hook(self, agent_id: str, decision: Any) -> None:
        """累计重要性并判定触发深度反思（04 §6.1 step6：importance_acc += Σ事件重要性）。

        sim_now/种子派生自该 agent 最新一条记忆（裁决协程串行前提下的确定口径，可回放）。
        """
        state = await self._read_state(agent_id)
        rows = await self._pool.fetch(
            "SELECT id, kind, importance, sim_time FROM memories WHERE agent_id=$1 AND id > $2 ORDER BY id",
            agent_id, state["acc_cursor"],
        )
        if not rows:
            return
        gained = sum(int(r["importance"]) for r in rows if r["kind"] in ACC_KINDS)
        state["importance_acc"] += gained
        state["acc_cursor"] = int(rows[-1]["id"])
        threshold = self._throttle.reflection_threshold(self._threshold)  # 降速读取点（04 §8.4 ④）
        if gained and state["importance_acc"] >= threshold:
            sim_now = rows[-1]["sim_time"]
            await self.deep_reflect(agent_id, sim_now, rng_seed=state["acc_cursor"], state=state)
            state["importance_acc"] = 0
        await self._write_state(agent_id, state)

    # ---- 深度反思（importance_acc 阈值触发） ------------------------------------------

    async def deep_reflect(
        self, agent_id: str, sim_now: dt.datetime, *, rng_seed: int, state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """窗口内记忆 → 明星层模型 2~3 条洞察（重要性 7~9）→ 双通道落库。"""
        state = state or await self._read_state(agent_id)
        window = await self._pool.fetch(
            """
            SELECT id, content, importance FROM memories
            WHERE agent_id=$1 AND id > $2 AND kind IN ('event','projection') AND NOT archived
            ORDER BY id LIMIT 50
            """,
            agent_id, state["reflect_cursor"],
        )
        persona = await self._persona(agent_id)
        mem_lines = "\n".join(f"- {r['content']}" for r in window) or "- （无）"
        result = await self._gw.chat(
            "reflection",
            [
                {"role": "system", "content": "你是角色扮演引擎。基于人设卡与近期经历生成 2~3 条第一人称洞察。只输出 JSON：{\"insights\": [...]}。"},
                {"role": "user", "content": f"人设卡={json.dumps(persona, ensure_ascii=False, sort_keys=True)}\n近期经历：\n{mem_lines}"},
            ],
            self._gw.gen_params("reflection") if hasattr(self._gw, "gen_params") else None,
            seed=rng_seed, agent_id=agent_id, sim_time=sim_now,
        )
        insights = self._parse_insights(result.text)
        memory_ids: list[int] = []
        for i, text in enumerate(insights):
            memory_ids.append(
                await insert_memory(
                    self._pool, self._gw, agent_id=agent_id, sim_time=sim_now, kind="reflection",
                    content=text, importance=DEEP_IMPORTANCE_BASE + (i % 3), rng_seed=rng_seed,
                )
            )
        event_seq = await self._emit_reflection_event(agent_id, insights, sim_now, rng_seed)
        state["reflect_cursor"] = max([state["reflect_cursor"], *[r["id"] for r in window], *memory_ids])
        state["acc_cursor"] = max(state["acc_cursor"], *memory_ids)
        await self._write_state(agent_id, state)
        log.info("深度反思：%s 洞察 %d 条（窗口 %d 条记忆）", agent_id, len(insights), len(window))
        return {"insights": insights, "memory_ids": memory_ids, "event_seq": event_seq}

    # ---- 每日兜底（23:00 批量反思点） -------------------------------------------------

    async def run_due_daily_fallbacks(self, agent_ids: list[str] | tuple[str, ...], sim_now: dt.datetime, *, rng_seed: int) -> list[str]:
        """23:00 批量反思点：对当日未兜底反思的 agent 逐一执行（按 agent 日幂等）。返回执行的 agent 列表。"""
        if sim_now.hour < FALLBACK_HOUR:
            return []
        done: list[str] = []
        today = sim_now.date().isoformat()
        for aid in agent_ids:
            state = await self._read_state(aid)
            if state["last_fallback_day"] == today:
                continue
            await self.daily_fallback(aid, sim_now, rng_seed=rng_seed, state=state)
            done.append(aid)
        return done

    async def daily_fallback(
        self, agent_id: str, sim_now: dt.datetime, *, rng_seed: int, state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """每日兜底：廉价摘要式反思（次要层档位模型）→ 1 条 reflection 记忆 + display-only 事件。"""
        state = state or await self._read_state(agent_id)
        day_start = sim_now.replace(hour=0, minute=0, second=0, microsecond=0)
        window = await self._pool.fetch(
            """
            SELECT content FROM memories
            WHERE agent_id=$1 AND kind IN ('event','projection') AND NOT archived AND sim_time >= $2
            ORDER BY id LIMIT 20
            """,
            agent_id, day_start,
        )
        persona = await self._persona(agent_id)
        mem_lines = "\n".join(f"- {r['content']}" for r in window) or "- （平淡一日）"
        result = await self._gw.chat(
            "bgsummary",
            [
                {"role": "system", "content": "总结该角色今天的模拟日，写一段第一人称摘要式反思。输出 JSON。"},
                {"role": "user", "content": f"人设卡={json.dumps(persona, ensure_ascii=False, sort_keys=True)}\n今日经历：\n{mem_lines}"},
            ],
            self._gw.gen_params("bgsummary") if hasattr(self._gw, "gen_params") else None,
            seed=rng_seed, agent_id=agent_id, sim_time=sim_now,
        )
        diary = self._parse_diary(result.text)
        memory_id = await insert_memory(
            self._pool, self._gw, agent_id=agent_id, sim_time=sim_now, kind="reflection",
            content=diary, importance=FALLBACK_IMPORTANCE, rng_seed=rng_seed,
        )
        event_seq = await self._emit_reflection_event(agent_id, [diary], sim_now, rng_seed)
        state["last_fallback_day"] = sim_now.date().isoformat()
        state["acc_cursor"] = max(state["acc_cursor"], memory_id)
        state["reflect_cursor"] = max(state["reflect_cursor"], memory_id)
        await self._write_state(agent_id, state)
        log.info("每日兜底反思：%s", agent_id)
        return {"diary": diary, "memory_id": memory_id, "event_seq": event_seq}

    # ---- T-REL-03 挂接（换策略/放弃生成反思） -----------------------------------------

    async def write_generated_reflection(
        self, agent_id: str, content: str, *, importance: int, sim_now: dt.datetime | None = None, rng_seed: int = 0,
    ) -> int | None:
        """系统规则触发的生成反思（01 §3.3 换策略/放弃）：写 `kind='reflection'` 记忆；
        深度档（importance ≥7）同时落 display-only 事件，轻档不产（01 §4.1 反思链口径）。
        sim_now 缺省时取该 agent 最近事件时刻（裁决串行前提下的确定口径）。
        """
        if sim_now is None:
            sim_now = await self._pool.fetchval("SELECT max(sim_time) FROM events WHERE $1 = ANY(actors)", agent_id)
            if sim_now is None:
                log.warning("write_generated_reflection：%s 无事件史，跳过（%s）", agent_id, content[:20])
                return None
        memory_id = await insert_memory(
            self._pool, self._gw, agent_id=agent_id, sim_time=sim_now, kind="reflection",
            content=content, importance=importance, rng_seed=rng_seed,
        )
        if importance >= LIGHT_REFLECTION_EVENT_MIN:
            await self._emit_reflection_event(agent_id, [content], sim_now, rng_seed)
        # 生成反思不回喂 acc（防自激），仅推进游标
        state = await self._read_state(agent_id)
        state["acc_cursor"] = max(state["acc_cursor"], memory_id)
        state["reflect_cursor"] = max(state["reflect_cursor"], memory_id)
        await self._write_state(agent_id, state)
        return memory_id

    # ---- 内部 -------------------------------------------------------------------

    async def _emit_reflection_event(self, agent_id: str, texts: list[str], sim_now: dt.datetime, rng_seed: int) -> int:
        """双通道②：`agent.reflection` display-only 事件——visibility='public'、trigger='autonomous'、
        payload 白名单仅 `text_display`（06 §1.2；反思原文永不入 payload，只走 memory_projection）。"""
        return await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, rng_seed, visibility, payload)
            VALUES ($1, $2, 'agent.reflection', $3, 'autonomous', $4, $5, 'public', $6::jsonb)
            RETURNING seq
            """,
            self._tick_of(sim_now), sim_now, f"agent:{agent_id}", [agent_id], rng_seed,
            json.dumps({"text_display": "；".join(texts)}, ensure_ascii=False),
        )

    @staticmethod
    def _parse_insights(text: str) -> list[str]:
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            return [text[:80]]
        insights = body.get("insights")
        if isinstance(insights, list) and insights:
            return [str(x)[:80] for x in insights[:3]]
        return [text[:80]]

    @staticmethod
    def _parse_diary(text: str) -> str:
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            return text[:120]
        return str(body.get("diary") or body.get("intent") or text)[:120]

    async def _persona(self, agent_id: str) -> dict[str, Any]:
        row = await self._pool.fetchrow("SELECT persona FROM agents WHERE id=$1", agent_id)
        if row is None:
            raise KeyError(f"agent {agent_id!r} 不存在")
        persona = row["persona"]
        if isinstance(persona, str):
            persona = json.loads(persona)
        persona = dict(persona or {})
        return {k: persona.get(k) for k in ("name", "age", "big_five", "speech_style") if persona.get(k) is not None}

    async def _read_state(self, agent_id: str) -> dict[str, Any]:
        row = await self._pool.fetchrow("SELECT mood FROM agents WHERE id=$1", agent_id)
        if row is None:
            raise KeyError(f"agent {agent_id!r} 不存在")
        mood = row["mood"]
        if isinstance(mood, str):
            mood = json.loads(mood)
        mood = dict(mood or {})
        return {
            "importance_acc": int(mood.get("importance_acc", 0)),
            "acc_cursor": int(mood.get("acc_cursor", 0)),
            "reflect_cursor": int(mood.get("reflect_cursor", 0)),
            "last_fallback_day": str(mood.get("last_fallback_day", "")),
        }

    async def _write_state(self, agent_id: str, state: dict[str, Any]) -> None:
        """importance_acc 与游标落 agents.mood JSON 持久化（04 §7.2；保留 mood 内其他键如 grudges/topic_cooldowns）。"""
        row = await self._pool.fetchrow("SELECT mood FROM agents WHERE id=$1", agent_id)
        mood = row["mood"]
        if isinstance(mood, str):
            mood = json.loads(mood)
        mood = dict(mood or {})
        mood.update(
            {
                "importance_acc": state["importance_acc"],
                "acc_cursor": state["acc_cursor"],
                "reflect_cursor": state["reflect_cursor"],
                "last_fallback_day": state["last_fallback_day"],
            }
        )
        await self._pool.execute("UPDATE agents SET mood=$2::jsonb WHERE id=$1", agent_id, json.dumps(mood, ensure_ascii=False))
