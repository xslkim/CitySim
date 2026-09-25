"""T-SYN-07 compute_ripple_edge 验收（05 §3.7：hop 递增/4 手上限/无源 cites 跳过/失真度口径）。"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.derived._helpers import ins_event, ins_memory
from worldsim.ingest.derived import compute_ripple_edge
from worldsim.ingest.derived.levenshtein import levenshtein, normalized_levenshtein

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_derived_ripple_test"


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "replica_v1.sql", DDL_DIR / "replica_grants.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    yield p
    await p.close()
    _drop_db(DB_NAME)


def _gossip_payload(teller: str, listener: str, about: str, cites: list[str],
                    text: str = "听说了吗") -> dict:
    return {"teller": teller, "listener": listener, "about": about, "cites": cites,
            "lines": [], "text_display": text}


async def test_hop1_root(pool) -> None:
    """验收 3：源事件 + 投影记忆 + gossip（cites 指向该记忆）→ root=源事件 seq, hop=1, src=root。"""
    await ins_event(pool, 10, "dialogue.chat", {"participants": ["A01", "A02"], "lines": [],
                                                "text_display": "原话"}, visibility="public")
    await ins_memory(pool, 100, "A03", source_event_seq=10, content_display="原话")
    await ins_event(pool, 20, "dialogue.gossip",
                    _gossip_payload("A03", "A04", "A01", ["100"], text="原话"),
                    visibility="public")
    n = await compute_ripple_edge.compute(pool, 20)
    assert n == 1
    row = await pool.fetchrow("SELECT * FROM ripple_edge WHERE dst_event_seq=20")
    assert row["root_event_seq"] == 10 and row["hop"] == 1 and row["src_event_seq"] == 10
    assert row["teller_id"] == "A03" and row["listener_id"] == "A04"
    assert float(row["distortion"]) == 0.0  # 全同文本


async def test_chain_hop_increment_and_cap(pool) -> None:
    """验收 4：5 手转述链 → hop 1..4 落行，第 5 手弃链无行（01 §4.1）。"""
    await ins_event(pool, 10, "dialogue.chat", {"participants": ["A01", "A02"], "lines": [],
                                                "text_display": "源话"}, visibility="public")
    prev_seq = 10
    for hand in range(1, 6):  # 5 手 gossip
        mem_id = 100 + hand
        await ins_memory(pool, mem_id, f"A{hand + 2:02d}", source_event_seq=prev_seq,
                         content_display=f"第{hand}手转述")
        gseq = 20 + hand
        await ins_event(pool, gseq, "dialogue.gossip",
                        _gossip_payload(f"A{hand + 2:02d}", f"A{hand + 3:02d}", "A01",
                                        [str(mem_id)], text=f"第{hand}手转述变体"),
                        visibility="public", minutes=30 + hand)
        await compute_ripple_edge.compute(pool, gseq)
        prev_seq = gseq
    rows = await pool.fetch(
        "SELECT dst_event_seq, hop FROM ripple_edge WHERE root_event_seq=10 ORDER BY hop")
    assert [r["hop"] for r in rows] == [1, 2, 3, 4]
    assert await pool.fetchval("SELECT count(*) FROM ripple_edge WHERE dst_event_seq=25") == 0  # 弃链


async def test_cites_without_source_skipped(pool) -> None:
    """验收 5：cites 指向 kind='reflection'（无 source_event_seq）记忆 → 无 ripple_edge 行（P2-9）。"""
    await ins_memory(pool, 100, "A03", source_event_seq=None, kind="reflection",
                     content_display="纯反思")
    await ins_event(pool, 20, "dialogue.gossip", _gossip_payload("A03", "A04", "A01", ["100"]),
                    visibility="public")
    n = await compute_ripple_edge.compute(pool, 20)
    assert n == 0
    assert await pool.fetchval("SELECT count(*) FROM ripple_edge") == 0


async def test_distortion_value(pool) -> None:
    """验收 6：已知串对手算断言；值域 0~1。"""
    assert normalized_levenshtein("完全相同", "完全相同") == 0.0
    assert levenshtein("abcd", "abxd") == 1
    assert normalized_levenshtein("abcd", "abxd") == 0.25   # 1/4（分母 max(len)）
    assert normalized_levenshtein("", "") == 0.0
    assert 0.0 <= normalized_levenshtein("abc", "") <= 1.0 == 1.0
    # 落表路径：gossip 文本与记忆展示文本差一字 → distortion=0.25
    await ins_event(pool, 10, "dialogue.chat", {"participants": ["A01", "A02"], "lines": [],
                                                "text_display": "abcd"}, visibility="public")
    await ins_memory(pool, 100, "A03", source_event_seq=10, content_display="abcd")
    await ins_event(pool, 20, "dialogue.gossip",
                    _gossip_payload("A03", "A04", "A01", ["100"], text="abxd"),
                    visibility="public")
    await compute_ripple_edge.compute(pool, 20)
    d = await pool.fetchval("SELECT distortion FROM ripple_edge WHERE dst_event_seq=20")
    assert float(d) == 0.25
    await pool.fetchval("SELECT 1 FROM ripple_edge WHERE distortion BETWEEN 0 AND 1")


async def test_distortion_not_in_events(pool) -> None:
    """验收 7：副本 events 全表 payload ? 'distortion' 恒 false（06 §2 废弃键不回写）。"""
    await ins_event(pool, 10, "dialogue.chat", {"participants": ["A01"], "lines": [],
                                                "text_display": "x"}, visibility="public")
    await ins_memory(pool, 100, "A03", source_event_seq=10, content_display="x")
    await ins_event(pool, 20, "dialogue.gossip", _gossip_payload("A03", "A04", "A01", ["100"]),
                    visibility="public")
    await compute_ripple_edge.compute(pool, 20)
    assert await pool.fetchval(
        "SELECT bool_or(payload ? 'distortion') FROM events") is False
