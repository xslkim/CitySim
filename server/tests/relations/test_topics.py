"""T-REL-06 话题系统验收。

口径：02 文档 T-REL-06 验收 1~3（yaml 加载校验：6 类计数配比/总数 ≥60/topic_id 唯一/必备键齐全 +
情感·秘密解锁门限 / 冲突类 tension 权重 ×3 / 72 模拟小时冷却命中与到期解除 / trigger_point 强制切入 +
test_topic_cooldown_sim_time 压缩比切换后冷却口径不变）。
数值一律从 config/topics.yaml 读值复算（00 §7 DoD 6）。
"""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml

from worldsim.relations.topics import TopicSystem, load_topics, load_rules
from worldsim.time_engine.clock import LOCAL_TZ

T0 = dt.datetime(2026, 10, 12, 20, 0, 0, tzinfo=LOCAL_TZ)  # 周一 20:00（黄金档）
TOPIC_AGENT_IDS = ["A08", "A09"]
TOPICS_PATH = Path(__file__).resolve().parents[2] / "config" / "topics.yaml"
TOPICS = load_topics(str(TOPICS_PATH))
RULES = load_rules(str(TOPICS_PATH))
META = yaml.safe_load(TOPICS_PATH.read_text(encoding="utf-8"))["meta"]


@pytest_asyncio.fixture
async def pool(test_db_dsn: str):
    p = await asyncpg.create_pool(test_db_dsn, min_size=1, max_size=4)
    for aid in TOPIC_AGENT_IDS:
        await p.execute(
            """
            INSERT INTO agents (id, name, gender, age, cognition_tier, persona, needs, balance_cents, position, mood)
            VALUES ($1, $2, 'F', 25, 'star', '{}'::jsonb, '{}'::jsonb, 100000, 'apt.lobby', NULL)
            ON CONFLICT (id) DO UPDATE SET mood=NULL
            """,
            aid, f"测试{aid}",
        )
    await p.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", TOPIC_AGENT_IDS)
    try:
        yield p
    finally:
        await p.close()
        conn = await asyncpg.connect(test_db_dsn)
        try:
            await conn.execute("DELETE FROM relations WHERE a_id = ANY($1) OR b_id = ANY($1)", TOPIC_AGENT_IDS)
            await conn.execute("DELETE FROM agents WHERE id = ANY($1)", TOPIC_AGENT_IDS)
        finally:
            await conn.close()


def _ts(pool, topics=None) -> TopicSystem:
    return TopicSystem(pool, topics if topics is not None else TOPICS, RULES,
                       per_dialogue=tuple(META["per_dialogue"]))


def test_yaml_load_validation() -> None:
    """验收 1 用例一：yaml 加载校验（6 类计数配比、总数 ≥60、topic_id 唯一、必备键齐全）。"""
    assert len(TOPICS) >= META["total_min"]
    for cat, n in META["category_counts"].items():
        assert sum(1 for t in TOPICS if t.category == cat) == n, f"{cat} 配比 {n}（01 §7 规模行）"
    assert len({t.topic_id for t in TOPICS}) == len(TOPICS), "topic_id 唯一"
    assert all(t.title and t.template for t in TOPICS), "必备键齐全（分类/引用模板）"
    assert all(t.dynamic == "gossip" for t in TOPICS if t.category == "八卦"), "八卦类为动态引用 gossip 种子模板"


def test_yaml_load_rejects_bad(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({
        "meta": {"total_min": 2, "category_counts": {"日常": 2}},
        "topics": [
            {"topic_id": "T-DAY-01", "category": "日常", "title": "a", "template": "x"},
            {"topic_id": "T-DAY-01", "category": "日常", "title": "b", "template": "y"},
        ],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="topic_id 重复"):
        load_topics(str(bad))


@pytest.mark.asyncio
async def test_unlock_gates(pool) -> None:
    """验收 1 用例二：情感类 affinity≥20 解锁、秘密类 affinity≥60 且一方主动（01 §7）。"""
    ts = _ts(pool, topics=[t for t in TOPICS if t.category in ("情感", "秘密", "日常")])
    emo = next(t for t in TOPICS if t.category == "情感")
    sec = next(t for t in TOPICS if t.category == "秘密")
    day = next(t for t in TOPICS if t.category == "日常")
    th_deep, th_sec = RULES["deep_unlock_affinity"], RULES["secret_unlock_affinity"]
    assert not ts.eligible(emo, max_affinity=th_deep - 1, max_tension=0, initiator_active=True)
    assert ts.eligible(emo, max_affinity=th_deep, max_tension=0, initiator_active=True)
    assert not ts.eligible(sec, max_affinity=th_sec - 1, max_tension=0, initiator_active=True)
    assert not ts.eligible(sec, max_affinity=th_sec, max_tension=0, initiator_active=False), "秘密类需一方主动"
    assert ts.eligible(sec, max_affinity=th_sec, max_tension=0, initiator_active=True)
    assert ts.eligible(day, max_affinity=0, max_tension=0, initiator_active=False), "日常/工作/八卦常开"
    # 集成：relations 缓存列驱动（双向取大值，D21）
    await pool.execute("INSERT INTO relations (a_id, b_id, affinity, tension) VALUES ('A08', 'A09', 19, 0)")
    picked = await ts.select(a_id="A08", b_id="A09", sim_now=T0, rng=random.Random(1), initiator="A08", count=2)
    assert picked and all(t.category == "日常" for t in picked), "affinity 19：情感/秘密未解锁"
    await pool.execute("UPDATE relations SET affinity=65 WHERE a_id='A08' AND b_id='A09'")
    seen = set()
    for seed in range(60):
        for t in await ts.select(a_id="A08", b_id="A09", sim_now=T0, rng=random.Random(seed), initiator="A08", count=2):
            seen.add(t.category)
    assert {"情感", "秘密"} <= seen, "affinity 65：情感与秘密均解锁入选"


@pytest.mark.asyncio
async def test_conflict_tension_weight(pool) -> None:
    """验收 1 用例三：冲突类 tension≥30 时权重 ×3（01 §7；双向取大值）。"""
    ts = _ts(pool)
    cnf = next(t for t in TOPICS if t.category == "冲突")
    day = next(t for t in TOPICS if t.category == "日常")
    th = RULES["conflict_tension_threshold"]
    assert ts.weight(cnf, max_tension=th) == float(RULES["conflict_weight_factor"])
    assert ts.weight(cnf, max_tension=th - 1) == 1.0
    assert ts.weight(day, max_tension=100) == 1.0, "权重 ×3 只作用冲突类"
    # 统计层：tension 30 时冲突类中签率显著高于 tension 0（抽样分布方向性回归）
    await pool.execute("INSERT INTO relations (a_id, b_id, affinity, tension) VALUES ('A08', 'A09', 0, 30)")
    hits = 0
    for seed in range(120):
        picked = await ts.select(a_id="A08", b_id="A09", sim_now=T0, rng=random.Random(seed), initiator="A08", count=1)
        hits += sum(1 for t in picked if t.category == "冲突")
    assert hits > 0, "tension≥30 冲突类权重 ×3 后可被选中"


@pytest.mark.asyncio
async def test_topic_cooldown_sim_time(pool) -> None:
    """验收 1 用例四 + 验收 3 指定用例：72 模拟小时冷却命中与到期解除；压缩比切换后口径不变（锚定 sim_time）。"""
    tiny = [t for t in TOPICS if t.topic_id == "T-DAY-01"]
    ts = _ts(pool, topics=tiny)
    rng = random.Random(0)
    first = await ts.select(a_id="A08", b_id="A09", sim_now=T0, rng=rng, count=1)
    assert [t.topic_id for t in first] == ["T-DAY-01"]
    until = await ts.mark_used(agent_ids=["A08", "A09"], topic_ids=["T-DAY-01"], sim_now=T0)
    assert until == T0 + dt.timedelta(hours=RULES["cooldown_hours"]), "冷却 = 72 模拟小时（D11 口径）"
    # 72 模拟小时内（不同压缩比只改真实时长，模拟时刻相同 → 同样不选中，00 §4 红线 11）
    for ratio_sim_hours in (1, 24, 71):  # 压缩比 1×/3×/6× 下同一模拟时点
        still = T0 + dt.timedelta(hours=ratio_sim_hours)
        picked = await ts.select(a_id="A08", b_id="A09", sim_now=still, rng=rng, count=1)
        assert picked == [], f"72 模拟小时内不重复选中（已过 {ratio_sim_hours}h，与压缩比无关）"
    picked = await ts.select(a_id="A08", b_id="A09", sim_now=until, rng=rng, count=1)
    assert [t.topic_id for t in picked] == ["T-DAY-01"], "到期解除（until_sim > now 才算命中）"
    # 冷却持久化于 agents.mood JSON topic_cooldowns 键（D11 登记口径）
    mood = json.loads(await pool.fetchval("SELECT mood::text FROM agents WHERE id='A08'"))
    assert mood["topic_cooldowns"]["T-DAY-01"] == until.isoformat()


@pytest.mark.asyncio
async def test_trigger_point_forced_insertion(pool) -> None:
    """验收 1 用例五：trigger_point 被戳中时冲突话题强制切入（01 §7/§11.1；接口供 T-ADJ-04）。"""
    ts = _ts(pool)
    personas = {
        "A08": {"trigger_point": "被说\"新人懂什么\""},
        "A09": {"trigger_point": "被当众催债，或被说\"你不行\""},
    }
    assert ts.find_trigger_hit(personas=personas, texts=["你开心什么", "新人懂什么，听我一句"]) == "A08"
    assert ts.find_trigger_hit(personas=personas, texts=["今天天气不错"]) is None
    assert ts.find_trigger_hit(personas=personas, texts=["让数据说话，但你不行"]) == "A09", "引号内关键词命中"
    # 无引号雷区：整串包含兜底（D21 工程口径）
    personas_whole = {"A09": {"trigger_point": "被当众催债"}}
    assert ts.find_trigger_hit(personas=personas_whole, texts=["他被当众催债，脸都红了"]) == "A09"
    hit = ts.find_trigger_hit(personas=personas, texts=["新人懂什么"])
    assert hit == "A08"
    topic = await ts.forced_conflict_topic(a_id="A08", b_id="A09", sim_now=T0, rng=random.Random(1))
    assert topic is not None and topic.category == "冲突", "戳中雷区 → 冲突话题强制切入"
    # 全部冲突话题冷却中 → 强制语义优先于去重：仍返回一个（最旧冷却者，D21）
    for t in TOPICS:
        if t.category == "冲突":
            await ts.mark_used(agent_ids=["A08"], topic_ids=[t.topic_id], sim_now=T0)
    topic = await ts.forced_conflict_topic(a_id="A08", b_id="A09", sim_now=T0 + dt.timedelta(hours=1), rng=random.Random(1))
    assert topic is not None and topic.category == "冲突"
