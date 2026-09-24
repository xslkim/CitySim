"""对话整段落库与逐句时间表（02 T-ADJ-04；04 §6.4 整段落库/at_offset_s/客户端节拍权威、
01 §7 轮数/自评/开场白/语言校验/话题系统、06 §1.2 `dialogue.chat` payload）。

- 轮数区间 6~8 轮（01 §7 源方案钦定不变量）；整段生成后写**一条** `dialogue.chat` 事件，
  `payload.lines=[{speaker, text_display, at_offset_s}, ...]`，`at_offset_s` 生成时按每句
  uniform(2,4)s 累计写死（分布见 04 §6.4）；内核不维护任何服务端播放队列（00 §4 红线 9）。
- 结构化自评字段 `self_eval`（a_enjoy/b_enjoy 0~10 → min≥7 愉快 / max≤3 敷衍 / 其间平淡，01 §7）
  / `opening_fact`（type ∈ memory/scene/state，缺失或非法 → 重生成 1 次）/ `quotable_lines`（≤2 句）；
  语言校验：任一角色台词命中其 `speech_style.taboo` → 重生成 1 次（01 §7）。
- **话题注入**：每场 1~2 个话题由 T-REL-06 `TopicSystem.select` 供给（含冷却与解锁门限），
  话题变量注入生成 prompt（模板渲染归 03 T-LLM-09）；select 返回空（全冷却）时兜底取**最旧冷却的
  日常类话题**（工程默认，02 文档偏差表 D28）；`trigger_point` 被台词戳中时冲突话题强制切入
  （T-REL-06 `find_trigger_hit`/`forced_conflict_topic`，重生成 1 次）。
- **降速读取点**：每模拟日对话场次上限运行时读取 `ThrottleState.dialogue_daily_cap`
  （判级器与 `system.llm.throttle` 事件唯一归 03 T-LLM-08；降速动作消费接线归 08 T-OPS-02；
  降速档场次上限降为 60% 口径见 04 §8.4 ③），未触发降速档时按 models.yaml 默认上限。
  达上限后该场对话不生成：写"想聊但今天话说得够多了"记忆兜底（工程默认，D28）。
- M1 不过安全管线，`text_display` 直写（M2 接三层过滤，02 文档 D10）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
from typing import Any

from ..memory import store
from ..memory.reflect import NullThrottleState, ThrottleState
from ..relations.cooldown import CooldownEngine
from ..relations.needs import NeedsEngine
from ..relations.relations import RelationEngine
from ..relations.topics import TopicSystem, Topic
from .grade import MOOD_HIT_MIN
from .state_events import StateAggregator

log = logging.getLogger(__name__)

ROUNDS_RANGE = (6, 8)                       # 对话轮数区间（01 §7 钦定不变量）
OPENING_FACT_TYPES = ("memory", "scene", "state")  # 开场白类型枚举（01 §7 可执行检测）
LINE_OFFSET_RANGE = (2.0, 4.0)              # 每句播放偏移 uniform(2,4)s（04 §6.4）
MAX_REGENERATE = 1                          # 校验失败重生成次数（01 §7）
CHAT_IMPORTANCE = 5                         # 对话记忆投影重要性（工程默认）
MOOD_ENJOYABLE_RANGE = (10.0, 18.0)         # 愉快对话情绪 +10~18（01 §3.1；rng 区间内取数）


def line_offsets(n: int, *, rng_seed: int, key: str) -> list[float]:
    """逐句时间表：每句 uniform(2,4)s 累计写死（04 §6.4；rng_seed 确定性，可回放）。"""
    rng = random.Random(f"dlg-offset:{rng_seed}:{key}")
    offsets: list[float] = []
    acc = 0.0
    for _ in range(n):
        acc += rng.uniform(*LINE_OFFSET_RANGE)
        offsets.append(round(acc, 3))
    return offsets


class DialogueEngine:
    """对话整段生成与结算。引擎/配置注入；仅运行于裁决协程（串行前提）。"""

    def __init__(
        self,
        pool: Any,
        gateway: Any,
        *,
        topics: TopicSystem,
        relations: RelationEngine,
        cooldown: CooldownEngine,
        needs_engine: NeedsEngine,
        agg: StateAggregator,
        throttle: ThrottleState | None = None,
        default_daily_cap: int = 42,
        grader: Any = None,
    ) -> None:
        self._pool = pool
        self._gw = gateway
        self._topics = topics
        self._relations = relations
        self._cooldown = cooldown
        self._needs = needs_engine
        self._agg = agg
        self._throttle = throttle or NullThrottleState()
        self._daily_cap = int(default_daily_cap)
        self._grader = grader  # T-ADJ-07 挂接（None = 不打分）

    # ---- T-ADJ-03 结算挂接（dialogue_settle 签名） ------------------------------

    async def settle_chat(self, obs: Any, args: dict[str, Any], *, tick: int, sim_now: dt.datetime,
                          rng_seed: int, cost: int, decision: Any) -> list[int]:
        a_id, b_id = obs.agent_id, args["target"]
        mode = str(args.get("mode") or "small")
        if mode not in ("small", "deep"):
            mode = "small"
        # 降速读取点：每模拟日对话场次上限（04 §8.4 ③；未降速按默认上限）
        cap = self._throttle.dialogue_daily_cap(self._daily_cap)
        if await self._chats_today(sim_now) >= cap:
            await store.insert_memory(
                self._pool, self._gw, agent_id=a_id, sim_time=sim_now, kind="event",
                content="想找人聊天，但今天说得够多了，先缓缓", importance=2, rng_seed=rng_seed,
            )
            log.info("对话场次达当日上限（cap=%d，降速读取点）：%s 本场不生成", cap, a_id)
            return []
        # 话题注入：T-REL-06 select（1~2 个）；空 → 兜底（D28）
        rng = random.Random(f"dlg-topic:{rng_seed}:{a_id}:{b_id}")
        topics = await self._topics.select(a_id=a_id, b_id=b_id, sim_now=sim_now, rng=rng, initiator=a_id)
        if not topics:
            topics = [await self._fallback_topic(a_id, b_id, sim_now)]
            if topics[0] is None:
                topics = []
        personas = {aid: await self._persona(aid) for aid in (a_id, b_id)}
        out = await self._generate(a_id, b_id, mode, topics, personas, sim_now, rng_seed)
        # 开场白/语言校验失败 → 重生成 1 次（01 §7）；trigger_point 戳中 → 冲突话题强制切入重生成
        if not self._opening_ok(out) or self._taboo_hit(out, personas):
            out = await self._generate(a_id, b_id, mode, topics, personas, sim_now, rng_seed + 1)
        texts = [str(l.get("text", "")) for l in out.get("lines", [])]
        hit = self._topics.find_trigger_hit(personas=personas, texts=texts)
        if hit is not None:
            forced = await self._topics.forced_conflict_topic(a_id=a_id, b_id=b_id, sim_now=sim_now, rng=rng)
            if forced is not None:
                topics = [forced]
                out = await self._generate(a_id, b_id, mode, topics, personas, sim_now, rng_seed + 2)
                texts = [str(l.get("text", "")) for l in out.get("lines", [])]
                log.info("trigger_point 戳中 %s：冲突话题 %s 强制切入（01 §7）", hit, forced.topic_id)
        lines_in = [l for l in out.get("lines", []) if isinstance(l, dict)][: ROUNDS_RANGE[1]]
        if len(lines_in) < ROUNDS_RANGE[0]:
            log.warning("对话轮数不足 %d（%d），按现有落库", ROUNDS_RANGE[0], len(lines_in))
        # 围观者（同节点第三者，上限 ≤6，01 §7/§4.1）
        witnesses = await self._witnesses(obs.position, [a_id, b_id], rng_seed)
        names = {aid: (personas[aid] or {}).get("name", aid) for aid in (a_id, b_id)}
        topic_ids = [t.topic_id for t in topics]
        topic_titles = "、".join(t.title for t in topics) or "随便聊聊"
        offsets = line_offsets(len(lines_in), rng_seed=rng_seed, key=f"{a_id}:{b_id}:{tick}")
        lines = [
            {"speaker": str(l.get("speaker") or (a_id if i % 2 == 0 else b_id)),
             "text_display": str(l.get("text", ""))[:40],  # 台词每人每句 ≤40 字（01 §7）
             "at_offset_s": offsets[i]}
            for i, l in enumerate(lines_in)
        ]
        self_eval = out.get("self_eval") or {}
        a_enjoy = float(self_eval.get("a_enjoy", 5))
        b_enjoy = float(self_eval.get("b_enjoy", 5))
        band = RelationEngine.chat_band(a_enjoy, b_enjoy)  # 愉快/平淡/敷衍（01 §7）
        # 情绪满足量先算（grade R3 信号预申报与实际施加同一数据源，D30）
        mood_gain = random.Random(f"dlg-mood:{rng_seed}:{a_id}:{b_id}:{tick}").uniform(*MOOD_ENJOYABLE_RANGE)
        text_display = f"{names[a_id]}和{names[b_id]}在{obs.position}聊起「{topic_titles}」"
        ui = None
        payload: dict[str, Any] = {
            "participants": [a_id, b_id], "mode": mode, "topic_ids": topic_ids,
            "lines": lines, "witnesses": witnesses, "text_display": text_display,
        }
        if self._grader is not None:
            ui = {"grade": await self._grader.grade(
                type_="dialogue.chat", actors=[a_id, b_id], payload=payload, sim_now=sim_now,
                rel_hit=False,  # chat 矩阵 ±3/±1（04 §6.6 R2 阈 |Δaffinity|≥5 不达）
                mood_hit=(band == "enjoyable" and mood_gain >= MOOD_HIT_MIN),
                followups=True,  # 敷衍 → 冷却 / 对象唤醒入队（04 §6.6 R4）
            )}
        seq = await self._pool.fetchval(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, location_id, actors, rng_seed, visibility, payload, ui)
            VALUES ($1, $2, 'dialogue.chat', $3, 'autonomous', $4, $5, $6, 'public', $7::jsonb, $8::jsonb)
            RETURNING seq
            """,
            tick, sim_now, f"agent:{a_id}", obs.position, [a_id, b_id], rng_seed,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(ui, ensure_ascii=False) if ui else None,
        )
        cause = store.caused_by(seq)
        # 关系结算（01 §3.2 chat 行 + 同日减半，T-REL-02；减半计数不含本场）；敷衍 → 冷却（01 §3.4）
        await self._relations.settle_chat(self._agg, a_id=a_id, b_id=b_id, band=band, cause=cause,
                                          sim_now=sim_now, exclude_seq=seq)
        if band == "perfunctory":
            await self._cooldown.write_cooldown(agent_id=a_id, trigger_kind="chat_perfunctory",
                                                target=b_id, sim_now=sim_now)
        # 需求结算：社交 +15/深聊 +25（01 §4）；愉快情绪 +10~18（01 §3.1，落库前已取定，D30）
        social_gain = self._needs.satisfy_amount("social", "deep_chat" if mode == "deep" else "chat")
        for x in (a_id, b_id):
            await self._agg.apply_needs_delta(agent_id=x, need="social", delta=social_gain, cause=cause)
            if band == "enjoyable":
                await self._agg.apply_needs_delta(agent_id=x, need="mood", delta=mood_gain, cause=cause)
        # 记忆写入（04 §6.3 单源双方投影 + 目击投影）
        await store.write_event_memories(
            self._pool, self._gw, event_seq=seq, sim_time=sim_now,
            perspectives={
                a_id: f"我和{names[b_id]}聊了聊「{topic_titles}」（{band}）",
                b_id: f"{names[a_id]}和我聊了聊「{topic_titles}」（{band}）",
            },
            importance=CHAT_IMPORTANCE, rng_seed=rng_seed,
        )
        await store.write_witness_projections(
            self._pool, self._gw, event_seq=seq, event_type="dialogue.chat", location_id=obs.position,
            actors=[a_id, b_id], sim_time=sim_now, base_importance=CHAT_IMPORTANCE, rng_seed=rng_seed,
            summary=f"聊天（{topic_titles}）",
        )
        # 话题冷却（72 模拟小时，T-REL-06 D11）
        if topic_ids:
            await self._topics.mark_used(agent_ids=[a_id, b_id], topic_ids=topic_ids, sim_now=sim_now)
        log.info("对话落库：%s↔%s 轮数=%d band=%s topics=%s seq=%s", a_id, b_id, len(lines), band, topic_ids, seq)
        return [seq]

    # ---- 生成与校验（01 §7） ----------------------------------------------------

    async def _generate(self, a_id: str, b_id: str, mode: str, topics: list[Topic],
                        personas: dict[str, Any], sim_now: dt.datetime, seed: int) -> dict[str, Any]:
        topic_vars = [{"topic_id": t.topic_id, "title": t.title, "template": t.template} for t in topics]
        obs_json = json.dumps(
            {"participants": [a_id, b_id], "mode": mode, "rounds": list(ROUNDS_RANGE),
             "topics": topic_vars},
            ensure_ascii=False, sort_keys=True,
        )
        styles = {
            aid: (personas[aid] or {}).get("speech_style") for aid in (a_id, b_id)
        }
        result = await self._gw.chat(
            "dialogue",
            [
                {"role": "system", "content": "你是角色扮演引擎。整段生成对话，只输出 JSON："
                 "{lines:[{speaker,text}], opening_fact:{type,ref}, self_eval:{a_enjoy,b_enjoy,basis}, quotable_lines:[]}。"},
                {"role": "user", "content": f"OBS_JSON={obs_json}\n语气卡={json.dumps(styles, ensure_ascii=False)}"},
            ],
            self._gw.gen_params("dialogue") if hasattr(self._gw, "gen_params") else None,
            seed=seed, agent_id=a_id, sim_time=sim_now,
        )
        try:
            body = json.loads(result.text)
            return body if isinstance(body, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _opening_ok(out: dict[str, Any]) -> bool:
        of = out.get("opening_fact")
        return isinstance(of, dict) and of.get("type") in OPENING_FACT_TYPES and bool(of.get("ref"))

    @staticmethod
    def _taboo_hit(out: dict[str, Any], personas: dict[str, Any]) -> bool:
        for line in out.get("lines", []):
            speaker = line.get("speaker")
            taboos = ((personas.get(speaker) or {}).get("speech_style") or {}).get("taboo") or []
            text = str(line.get("text", ""))
            if any(t and t in text for t in taboos):
                return True
        return False

    async def _fallback_topic(self, a_id: str, b_id: str, sim_now: dt.datetime) -> Topic | None:
        """select 全冷却兜底：最旧冷却的日常类话题（工程默认，02 文档偏差表 D28）。"""
        daily = [t for t in self._topics._topics if t.category == "日常"]
        if not daily:
            return None
        untils = []
        for t in daily:
            u1 = await self._topics.cooldown_until(a_id, t.topic_id)
            u2 = await self._topics.cooldown_until(b_id, t.topic_id)
            untils.append((min([u for u in (u1, u2) if u] or [dt.datetime.min.replace(tzinfo=sim_now.tzinfo)]), t))
        return min(untils, key=lambda x: x[0])[1]

    async def _witnesses(self, position: str, participants: list[str], rng_seed: int) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT id FROM agents WHERE position=$1 AND NOT (id = ANY($2::text[])) ORDER BY id",
            position, participants,
        )
        ids = [r["id"] for r in rows]
        if len(ids) > 6:  # 同节点目击上限 ≤6（01 §4.1/§7，超出确定性抽样）
            ids = sorted(random.Random(f"dlg-wit:{rng_seed}:{position}").sample(ids, 6))
        return ids

    async def _persona(self, agent_id: str) -> dict[str, Any]:
        raw = await self._pool.fetchval("SELECT persona FROM agents WHERE id=$1", agent_id)
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})

    async def _chats_today(self, sim_now: dt.datetime) -> int:
        day_start = sim_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._pool.fetchval(
            "SELECT count(*) FROM events WHERE type='dialogue.chat' AND sim_time >= $1 AND sim_time < $2",
            day_start, day_start + dt.timedelta(days=1),
        )
