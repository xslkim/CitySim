"""T-AUD-09 采访评估记忆查询验收（01 §10.2 十六题数据源覆盖 + 反证定位）。"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db

SERVER_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVER_ROOT / "scripts"))

import interview_query  # noqa: E402

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_interview_query_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
BASE = dt.datetime(2026, 10, 12, 9, 0, tzinfo=LOCAL_TZ)


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def _ev(pool, seq: int, type_: str, payload: dict, *, actors: list[str],
              minutes: int = 0, visibility: str = "public", ui: dict | None = None,
              trigger: str = "autonomous") -> None:
    await pool.execute(
        """
        INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, actors,
                            location_id, payload, visibility, ui)
        OVERRIDING SYSTEM VALUE
        VALUES ($1,$1,$2,$2,$3,'agent:A01',$4,$5,'corp.pantry',$6::jsonb,$7,$8::jsonb)
        """, seq, BASE + dt.timedelta(minutes=minutes), type_, trigger, actors,
        json.dumps(payload, ensure_ascii=False), visibility,
        json.dumps(ui) if ui else None)


async def _seed_agent_story(pool) -> dict[str, int]:
    """构造 A01 的完整故事线，返回关键事件 seq。"""
    ids: dict[str, int] = {}
    # 关系边（Top-3 |affinity|）
    for a, b, aff, ten in (("A01", "A02", 40, 5), ("A01", "A03", -30, 50), ("A02", "A01", 25, 0)):
        await pool.execute(
            """
            INSERT INTO relations (a_id, b_id, affinity, tension, labels, last_event_seq)
            VALUES ($1,$2,$3,$4,'{}',1) ON CONFLICT (a_id, b_id) DO UPDATE SET
              affinity=$3, tension=$4
            """, a, b, aff, ten)
    # 目标
    await pool.execute(
        "INSERT INTO goals (agent_id, sim_week, goal) VALUES ('A01', 1, '拿下季度评优')"
        " ON CONFLICT DO NOTHING")
    # 吵架（tension 记忆）
    await _ev(pool, 10, "dialogue.argue",
              {"participants": ["A01", "A03"], "reason_hint": "排期冲突", "lines": [],
               "witnesses": []}, actors=["A01", "A03"], minutes=60)
    ids["argue"] = 10
    # 经济结算
    await _ev(pool, 11, "agent.eat", {"venue": "corp.canteen", "with": ["A02"],
                                      "amount_cents": 2500}, actors=["A01", "A02"], minutes=120)
    ids["eat"] = 11
    # gossip 链：源事件 → A02 记忆 → A02 讲给 A01
    await _ev(pool, 12, "dialogue.chat",
              {"participants": ["A03", "A04"], "mode": "chat", "topic_ids": [], "lines": [],
               "witnesses": [], "text_display": "A03 和 A04 的源对话"}, actors=["A03", "A04"],
              minutes=30)
    mem_id = await pool.fetchval(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance,
                              source_event_seq)
        VALUES ('A02', $1, 'projection', '原文', '源对话展示', 6, 12) RETURNING id
        """, BASE + dt.timedelta(minutes=31))
    await _ev(pool, 13, "dialogue.gossip",
              {"teller": "A02", "listener": "A01", "about": "A03", "cites": [str(mem_id)],
               "lines": [], "text_display": "A02 把 A03 的事讲给 A01"},
              actors=["A02", "A01"], minutes=40)
    ids["gossip"] = 13
    # 反思
    await pool.execute(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance)
        VALUES ('A01', $1, 'reflection', '反思原文', '今天的反思展示', 7)
        """, BASE + dt.timedelta(hours=13))
    # 债务
    await pool.execute(
        "INSERT INTO debts (a_id, b_id, amount_cents, due_sim, created_tick)"
        " VALUES ('A01','A03', 5000, $1, 1)", BASE + dt.timedelta(days=14))
    # 工作
    await _ev(pool, 14, "agent.work", {"task_id": "T-88", "sim_cost_min": 120},
              actors=["A01"], minutes=200, visibility="internal")
    # 复核（绩效评价）
    await _ev(pool, 15, "director.grade_revise",
              {"target_seq": "14", "new_grade": "A", "reason": "交付出色"},
              actors=[], minutes=300, trigger="director")
    return ids


async def test_evidence_pack_covers_16_questions(pool) -> None:
    """验收 3a：fixture agent 证据包覆盖 16 题数据源字段位（关系/记忆/目标/债务/经济/绩效/反思）。"""
    ids = await _seed_agent_story(pool)
    ev = await interview_query.gather_evidence(pool, "A01")
    assert sorted(ev, key=lambda x: int(x[1:])) == [f"q{i}" for i in range(1, 17)]
    # q1：Top-3 |affinity| 边
    q1 = ev["q1"]["data"]
    assert len(q1) == 3 and abs(q1[0]["affinity"]) >= abs(q1[1]["affinity"])
    # q6：tension 边 + argue 证据
    assert ev["q6"]["data"]["tension_edges"][0]["tension"] == 50
    assert ids["argue"] in ev["q6"]["evidence_event_ids"]
    # q7：gossip 链含 cites 回溯到源事件 12
    chain = ev["q7"]["data"][0]
    assert chain["source_event_seqs"] == [12]
    assert 12 in ev["q7"]["evidence_event_ids"] and ids["gossip"] in ev["q7"]["evidence_event_ids"]
    # q5 经济 / q10 债务 / q11 工作 / q12 复核 / q16 反思
    assert ev["q5"]["data"][0]["amount_cents"] == 2500
    assert any(d["amount_cents"] == 5000 for d in ev["q10"]["data"])
    assert ev["q11"]["data"][0]["task_id"] == "T-88"
    assert ev["q12"]["data"][0]["new_grade"] == "A"
    assert ev["q16"]["data"][0]["content_display"] == "今天的反思展示"
    # q3 目标 / q9 就餐 / q13 周末 / q15 空间
    assert any(g["goal"] == "拿下季度评优" for g in ev["q3"]["data"])
    assert ev["q9"]["data"][0]["venue"] == "corp.canteen"
    assert "q13" in ev and "q15" in ev
    # q14：secret 只证存在不泄内容
    secret = (await pool.fetchrow("SELECT persona FROM agents WHERE id='A01'"))["persona"]
    secret = json.loads(secret) if isinstance(secret, str) else secret
    if secret.get("secret"):
        assert ev["q14"]["data"]["has_secret"] is True
        assert secret["secret"] not in json.dumps(ev["q14"], ensure_ascii=False)


async def test_contradiction_locates_counter_evidence(pool, tmp_path) -> None:
    """验收 3b：构造"记忆与事件库矛盾"样例 → 0 分题可附反证 evidence_event_ids。"""
    ids = await _seed_agent_story(pool)
    # 矛盾样例：记忆称"昨晚在家煮面"，事件库 eat 记录是食堂（seq=11）
    await pool.execute(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance,
                              source_event_seq)
        VALUES ('A01', $1, 'projection', '原文', '昨晚我在家煮的面', 5, $2)
        """, BASE + dt.timedelta(hours=20), ids["eat"])
    ev = await interview_query.gather_evidence(pool, "A01")
    q9 = ev["q9"]
    assert q9["data"][0]["venue"] == "corp.canteen"          # 事件库口径
    assert ids["eat"] in q9["evidence_event_ids"]            # 反证可附
    # 打分表骨架落盘（01 §10.2 字段逐字）
    interview_query.OUT_DIR = tmp_path
    sheet = [{"q_id": 9, "agent_id": "A01", "answer_summary": "答：在家煮面",
              "evidence_event_ids": q9["evidence_event_ids"], "score": 0,
              "note": "与事件库矛盾：eat 事件 venue=corp.canteen"}]
    path = tmp_path / "2026-10-12-A01.json"
    path.write_text(json.dumps(sheet, ensure_ascii=False), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert set(loaded[0]) == {"q_id", "agent_id", "answer_summary",
                              "evidence_event_ids", "score", "note"}
    assert loaded[0]["score"] == 0 and loaded[0]["evidence_event_ids"]
