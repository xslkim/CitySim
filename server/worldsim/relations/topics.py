"""话题系统（02 T-REL-06；01 §7 话题系统行/话题库规模行、01 §11.1 trigger_point、02 文档 D11 冷却存储口径）。

- `config/topics.yaml` ≥60 条、6 类配比 12/12/10/10/8/8（逐字按 01 §7 话题库规模行），
  `load_topics` 加载即校验（分类计数/总数/topic_id 唯一/必备键）；YAML 重载即生效（月度增补运营动作）。
- **解锁与权重**（关系读数经 T-REL-02 缓存列；双向取大值，工程口径 D21）：情感类 affinity≥20 解锁、
  秘密类 affinity≥60 且一方主动、冲突类 tension≥30 时权重 ×3——数值读 topics.yaml `rules:` 段。
- **每场对话 1~2 个话题**（区间读 meta.per_dialogue），选择结果供 T-ADJ-04 注入对话生成
  （模板变量 `topic` 归 03 T-LLM-09）；八卦类 `dynamic: gossip` 的 `{gossip}` 槽由 T-MEM-01 检索供数。
- **去重冷却 72 模拟小时**（02 D11：72h = 72 模拟小时）：持久化于 `agents.mood` JSON
  `topic_cooldowns` 键（与 T-MEM-02 importance_acc 同例）；冷却只读 sim_time（00 §4 红线 11，
  压缩比切换不影响口径）。
- **trigger_point 强制切入**（01 §7/§11.1）：`find_trigger_hit` 检测台词戳中谁的人设雷区，
  `forced_conflict_topic` 供 T-ADJ-04 强制切入冲突话题；trigger_point 属人设私下半，
  仅内核内部通道使用、永不出站（05 §3.6 白名单边界）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

import yaml

log = logging.getLogger(__name__)

CATEGORIES = ("日常", "工作", "八卦", "情感", "秘密", "冲突")  # 6 类（01 §7）
_REQUIRED_KEYS = ("topic_id", "category", "title", "template")
_QUOTED_RE = re.compile(r'["「](.*?)["」]')  # trigger_point 引号内关键词提取（工程口径 D21）


@dataclass(frozen=True)
class Topic:
    topic_id: str
    category: str
    title: str
    template: str
    dynamic: str | None = None  # 'gossip' = 动态引用 gossip 记忆条目的种子模板（01 §7）


def load_topics(path: str) -> list[Topic]:
    """加载并校验话题库（6 类计数配比、总数 ≥60、topic_id 唯一、必备键齐全）。"""
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    raw = doc.get("topics")
    if not isinstance(raw, list):
        raise ValueError("topics.yaml 缺 topics 列表")
    topics = [Topic(**{k: t.get(k) for k in (*_REQUIRED_KEYS, "dynamic")}) for t in raw]
    ids = [t.topic_id for t in topics]
    if len(set(ids)) != len(ids):
        raise ValueError("topics.yaml topic_id 重复")
    for t in topics:
        for k in _REQUIRED_KEYS:
            if not getattr(t, k):
                raise ValueError(f"话题 {t.topic_id!r} 缺必备键 {k}（01 §7：topic_id/分类/引用模板）")
        if t.category not in CATEGORIES:
            raise ValueError(f"话题 {t.topic_id!r} 分类 {t.category!r} 不在 6 类内（01 §7）")
        if t.dynamic and t.dynamic != "gossip":
            raise ValueError(f"话题 {t.topic_id!r} dynamic 仅支持 'gossip'")
        if t.category == "八卦" and t.dynamic != "gossip":
            raise ValueError(f"八卦类话题 {t.topic_id!r} 须标 dynamic: gossip（动态引用 gossip 记忆种子模板，01 §7）")
    counts = {c: sum(1 for t in topics if t.category == c) for c in CATEGORIES}
    expect = doc["meta"]["category_counts"]
    for cat, n in expect.items():
        if counts.get(cat, 0) != n:
            raise ValueError(f"topics.yaml 分类配比不符：{cat} 期望 {n} 实得 {counts.get(cat, 0)}（01 §7 规模行）")
    if len(topics) < doc["meta"]["total_min"]:
        raise ValueError(f"话题库规模不足：{len(topics)} < {doc['meta']['total_min']}（01 §7 ≥60）")
    return topics


def load_rules(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["rules"]


class TopicSystem:
    """话题选择/冷却/trigger_point 引擎。`pool` = asyncpg pool；`rules` = topics.yaml rules 段。"""

    def __init__(self, pool: Any, topics: list[Topic], rules: dict[str, Any], per_dialogue: tuple[int, int] = (1, 2)) -> None:
        self._pool = pool
        self._topics = topics
        self._rules = rules
        self._per_dialogue = per_dialogue  # 每场对话话题数区间（01 §7：1~2，topics.yaml meta.per_dialogue）

    # ---- 解锁与权重（01 §7） --------------------------------------------------------

    def eligible(self, topic: Topic, *, max_affinity: int, max_tension: int, initiator_active: bool) -> bool:
        """分类解锁门限：情感 affinity≥20；秘密 affinity≥60 且一方主动；其余常开。"""
        if topic.category == "情感":
            return max_affinity >= int(self._rules["deep_unlock_affinity"])
        if topic.category == "秘密":
            return max_affinity >= int(self._rules["secret_unlock_affinity"]) and initiator_active
        return True

    def weight(self, topic: Topic, *, max_tension: int) -> float:
        """冲突类 tension≥30 时权重 ×3（01 §7）；其余 1.0。"""
        if topic.category == "冲突" and max_tension >= int(self._rules["conflict_tension_threshold"]):
            return float(self._rules["conflict_weight_factor"])
        return 1.0

    # ---- 选择（每场 1~2 个；跨 agent 冷却排除） ---------------------------------------

    async def select(
        self, *, a_id: str, b_id: str, sim_now: dt.datetime, rng: random.Random,
        initiator: str | None = None, count: int | None = None,
    ) -> list[Topic]:
        """为 (A,B) 对话选 1~2 个话题：解锁过滤 → 冷却排除（任一参与者冷却中即排除）→ 加权抽样。

        返回空列表 = 全部在冷却（调用方兜底：T-ADJ-04 可放宽 count 或复用最旧话题，M1 口径 D21）。
        """
        max_aff, max_ten = await self._pair_peaks(a_id, b_id)
        cooled = await self._cooled_ids({a_id, b_id}, sim_now)
        pool = [
            (t, self.weight(t, max_tension=max_ten))
            for t in self._topics
            if t.topic_id not in cooled and self.eligible(t, max_affinity=max_aff, max_tension=max_ten,
                                                          initiator_active=initiator is not None)
        ]
        if not pool:
            return []
        lo, hi = self._per_dialogue
        n = count if count is not None else rng.randint(lo, hi)
        n = min(max(lo, n), hi, len(pool))
        picked: list[Topic] = []
        rest = pool
        for _ in range(n):
            total = sum(w for _, w in rest)
            roll = rng.uniform(0, total)
            acc = 0.0
            for i, (t, w) in enumerate(rest):
                acc += w
                if roll <= acc:
                    picked.append(t)
                    rest = [*rest[:i], *rest[i + 1:]]
                    break
        return picked

    async def mark_used(self, *, agent_ids: list[str], topic_ids: list[str], sim_now: dt.datetime) -> dt.datetime:
        """对话使用话题后记冷却：同一 agent 同一 topic_id 72 模拟小时（D11；mood JSON topic_cooldowns 键）。"""
        until = sim_now + dt.timedelta(hours=float(self._rules["cooldown_hours"]))
        for aid in agent_ids:
            mood = await self._read_mood(aid)
            cds = dict(mood.get("topic_cooldowns") or {})
            for tid in topic_ids:
                cds[tid] = until.isoformat()
            mood["topic_cooldowns"] = cds
            await self._pool.execute("UPDATE agents SET mood=$2::jsonb WHERE id=$1", aid, json.dumps(mood, ensure_ascii=False))
        return until

    async def cooldown_until(self, agent_id: str, topic_id: str) -> dt.datetime | None:
        iso = (await self._read_mood(agent_id)).get("topic_cooldowns", {}).get(topic_id)
        return dt.datetime.fromisoformat(iso) if iso else None

    async def _cooled_ids(self, agent_ids: set[str], sim_now: dt.datetime) -> set[str]:
        cooled: set[str] = set()
        for aid in agent_ids:
            cds = (await self._read_mood(aid)).get("topic_cooldowns") or {}
            for tid, iso in cds.items():
                if dt.datetime.fromisoformat(iso) > sim_now:
                    cooled.add(tid)
        return cooled

    async def _pair_peaks(self, a_id: str, b_id: str) -> tuple[int, int]:
        """双向关系读数取大值（工程口径 D21；缓存列读数，T-REL-02 口径）。"""
        row = await self._pool.fetchrow(
            """
            SELECT max(affinity) AS aff, max(tension) AS ten FROM relations
            WHERE (a_id=$1 AND b_id=$2) OR (a_id=$2 AND b_id=$1)
            """,
            a_id, b_id,
        )
        return (int(row["aff"]), int(row["ten"])) if row and row["aff"] is not None else (0, 0)

    async def _read_mood(self, agent_id: str) -> dict[str, Any]:
        row = await self._pool.fetchrow("SELECT mood FROM agents WHERE id=$1", agent_id)
        if row is None:
            raise KeyError(f"agent {agent_id!r} 不存在")
        mood = row["mood"]
        if isinstance(mood, str):
            mood = json.loads(mood)
        return dict(mood or {})

    # ---- trigger_point 强制切入（01 §7/§11.1；仅供内核内部通道，永不出站） ----------------

    @staticmethod
    def trigger_keywords(trigger_point: str) -> list[str]:
        """雷区关键词提取：引号内片段优先，无引号取整串（工程口径 D21）。"""
        quoted = [m.strip() for m in _QUOTED_RE.findall(trigger_point) if m.strip()]
        return quoted or ([trigger_point.strip()] if trigger_point.strip() else [])

    def find_trigger_hit(self, *, personas: dict[str, dict[str, Any]], texts: list[str]) -> str | None:
        """台词戳中谁的人设雷区：任一 trigger_point 关键词出现在台词中 → 返回该 agent_id。"""
        for aid, persona in personas.items():
            tp = (persona or {}).get("trigger_point") or ""
            for kw in self.trigger_keywords(str(tp)):
                if kw and any(kw in t for t in texts):
                    return aid
        return None

    async def forced_conflict_topic(
        self, *, a_id: str, b_id: str, sim_now: dt.datetime, rng: random.Random,
    ) -> Topic | None:
        """trigger_point 被戳中时冲突话题强制切入（01 §7）：取未冷却冲突话题（rng 确定性）；
        全部在冷却时取最旧冷却者（强制语义优先于去重，D21）。返回 None = 库中无冲突类。"""
        conflicts = [t for t in self._topics if t.category == "冲突"]
        if not conflicts:
            return None
        cooled = await self._cooled_ids({a_id, b_id}, sim_now)
        open_ = [t for t in conflicts if t.topic_id not in cooled]
        if open_:
            return rng.choice(open_)
        untils = []
        for t in conflicts:
            u1 = await self.cooldown_until(a_id, t.topic_id)
            u2 = await self.cooldown_until(b_id, t.topic_id)
            untils.append((min([u for u in (u1, u2) if u] or [dt.datetime.min.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))]), t))
        return min(untils, key=lambda x: x[0])[1]
