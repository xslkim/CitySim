"""T-ADJ-09 每模拟日 world_state 全量快照落盘验收。

口径：02 文档 T-ADJ-09 验收 1~3（连续 2 模拟日各产 1 个 gzip 且 JSON 可解析 / 同 sim_day 幂等覆盖 /
白名单子集键集合逐字对照 05 §3.6 且不含禁出键 + test_digest_recomputable + world_state snapshot.* 留痕键）。
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import subprocess
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from worldsim.snapshot.canonical import canonical, canonical_event_payload, digest_events, sha256_hex
from worldsim.snapshot.dump import build_whitelist_state, contrast_public, dump_snapshot, speech_style_public
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_snapshot_test"
PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET_DIR = "/tmp"
ROOT = Path(__file__).resolve().parents[2]
T0 = dt.datetime(2026, 10, 12, 23, 59, 0, tzinfo=LOCAL_TZ)

_PERSONA = {
    "name": "测试A20", "big_five": {"openness": 72}, "backstory": [{"age": 12, "event": "父母离异"}],
    "appearance": {"hair": "F03"}, "signature_quirk": "转笔",
    "contrast": "白天安静像素画手，深夜网文写手", "secret": "马甲身份",
    "trigger_point": "被说画画没用",
    "speech_style": {"tone": "克制", "sentence_len": "短", "catchphrase": "也行吧", "taboo": ["炫耀收入"]},
    "weekly_routine_bias": {"sleep": "1:00~7:30"},
}
_NEEDS = {"energy": 70, "hunger": 70, "mood": 70, "social": 70, "wealth": 70, "achievement": 70}


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )


@pytest_asyncio.fixture
async def pool(tmp_path):
    _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
    _psql(f"CREATE DATABASE {DB_NAME}")
    _psql("CREATE SCHEMA IF NOT EXISTS partman", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS vector", db=DB_NAME)
    _psql("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman", db=DB_NAME)
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", DB_NAME, "-v", "ON_ERROR_STOP=1",
         "-f", str(ROOT / "ddl" / "schema_v1.sql")],
        check=True, capture_output=True, text=True,
    )
    p = await asyncpg.create_pool(f"postgresql:///{DB_NAME}?host={SOCKET_DIR}", min_size=1, max_size=4)
    await p.execute(
        """
        INSERT INTO agents (id, name, gender, age, room_no, department, job_title, cognition_tier, persona,
                            needs, mood, balance_cents, position, holdings)
        VALUES ('A20', '测试A20', 'F', 26, '203', '技术部', 'P1', 'star', $1::jsonb, $2::jsonb,
                '{"importance_acc": 7, "grudges": {"A21": "x"}, "topic_cooldowns": {}}'::jsonb,
                1234567, 'apt.L2.203', '{"XLKJ": 3}'::jsonb)
        """,
        json.dumps(_PERSONA, ensure_ascii=False), json.dumps(_NEEDS),
    )
    await p.execute(
        """
        INSERT INTO agents (id, name, gender, age, room_no, department, job_title, cognition_tier, persona, needs,
                            mood, balance_cents, position, holdings)
        VALUES ('A21', '测试A21', 'M', 28, '201', '技术部', 'P2', 'star', $1::jsonb, $2::jsonb,
                NULL, 500, 'apt.L2.201', '{}'::jsonb)
        """,
        json.dumps({"name": "测试A21", "big_five": {}}, ensure_ascii=False), json.dumps(dict(_NEEDS)),
    )
    await p.execute(
        "INSERT INTO relations (a_id, b_id, affinity, tension, labels, one_line) "
        "VALUES ('A20', 'A21', 35, 10, '{暗恋}', '不敢表露')"
    )
    await p.execute(
        "INSERT INTO goals (agent_id, sim_week, goal) VALUES ('A20', 1, '找机会单独吃饭')"
    )
    await p.execute(
        "INSERT INTO world_state (key, value, updated_tick) VALUES "
        "('economy.stocks', '{\"XLKJ\": 1800, \"LJBK\": 950}'::jsonb, 0)"
    )
    try:
        yield p, tmp_path
    finally:
        await p.close()
        _psql(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")


async def test_two_days_dump_and_idempotent(pool) -> None:
    """用例一：连续 2 模拟日各产 1 个 gzip 且 JSON 可解析；同 sim_day 重跑幂等覆盖。"""
    p, tmp = pool
    day1 = dt.date(2026, 10, 12)
    out1 = await dump_snapshot(p, sim_day=day1, out_dir=tmp, tick=1, sim_now=T0, compression_ratio=3.0)
    out2 = await dump_snapshot(p, sim_day=day1 + dt.timedelta(days=1), out_dir=tmp, tick=2,
                               sim_now=T0 + dt.timedelta(days=1), compression_ratio=3.0)
    files = sorted(tmp.glob("snapshot_*.json.gz"))
    assert len(files) == 4  # 2 日 ×（全量 + 白名单）
    for f in files:
        json.loads(gzip.decompress(f.read_bytes()).decode("utf-8"))  # JSON 可解析
    # 同 sim_day 重跑幂等覆盖（文件数不变，内容随最新态）
    await dump_snapshot(p, sim_day=day1, out_dir=tmp, tick=3, sim_now=T0, compression_ratio=3.0)
    assert len(list(tmp.glob("snapshot_*.json.gz"))) == 4
    assert json.loads(gzip.decompress(Path(out1["path"]).read_bytes()).decode("utf-8"))["world_state"]
    assert Path(out2["whitelist_path"]).exists() and Path(out2["whitelist_path"]).stat().st_size > 0


async def test_whitelist_keys_and_forbidden_absent(pool) -> None:
    """用例二：白名单子集键集合逐字对照 05 §3.6；禁出键（余额/持仓/secret/trigger_point/contrast
    私下半/taboo/agents.mood 内部键/记忆原文）不出现。"""
    p, tmp = pool
    day1 = dt.date(2026, 10, 12)
    out = await dump_snapshot(p, sim_day=day1, out_dir=tmp, tick=1, sim_now=T0, compression_ratio=3.0)
    state = json.loads(gzip.decompress(Path(out["whitelist_path"]).read_bytes()).decode("utf-8"))
    assert set(state) == {"sim", "agents", "relations", "economy", "health", "announcements"}
    assert set(state["sim"]) == {"day", "sim_time", "compression_ratio"}
    a = next(x for x in state["agents"] if x["id"] == "A20")
    assert set(a) == {"id", "name", "gender", "age", "room_no", "department", "job_title",
                      "cognition_tier", "persona_display", "routine", "needs", "mood",
                      "position", "activity", "goals"}
    assert set(a["persona_display"]) == {"big_five", "backstory", "appearance",
                                         "signature_quirk", "contrast_public", "speech_style_public"}
    assert set(a["routine"]) == {"regular", "weekly_bias_public"}
    assert set(a["goals"][0]) == {"goal", "blocked_count", "frustration"}
    assert set(state["relations"][0]) == {"a", "b", "affinity", "tension", "labels", "one_line"}
    assert set(state["economy"]["stocks"][0]) == {"symbol", "price"}
    assert set(state["health"]) == {"cost_daily_micro_cny"}
    # 禁出键
    raw = json.dumps(state, ensure_ascii=False)
    for forbidden in ("balance_cents", "holdings", "secret", "trigger_point", "taboo",
                      "importance_acc", "grudges", "topic_cooldowns"):
        assert forbidden not in raw, f"白名单子集不得含 {forbidden}"
    assert "深夜网文写手" not in raw and a["persona_display"]["contrast_public"] == "白天安静像素画手"
    assert a["mood"] == 70  # 情绪值（D32）
    assert state["economy"]["stocks"] == [{"symbol": "LJBK", "price": 950}, {"symbol": "XLKJ", "price": 1800}]
    assert state["health"]["cost_daily_micro_cny"] is None  # M1 缺省（08 T-AUD-08 起携带）


async def test_digest_recomputable(pool) -> None:
    """验收 2 指定用例：对落盘的白名单子集重算 sha256(canonical(state)) 与 sidecar digest 逐字节一致。"""
    p, tmp = pool
    out = await dump_snapshot(p, sim_day=dt.date(2026, 10, 12), out_dir=tmp, tick=1, sim_now=T0,
                              compression_ratio=3.0)
    state_text = gzip.decompress(Path(out["whitelist_path"]).read_bytes()).decode("utf-8")
    recomputed = "sha256:" + sha256_hex(canonical(json.loads(state_text)))
    dg = tmp / "snapshot_2026-10-12.digest"
    assert dg.read_text(encoding="utf-8").strip() == out["digest"] == recomputed
    # canonical 无冗余空白、键递归典序
    assert canonical({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'


async def test_world_state_snapshot_trace_key(pool) -> None:
    """验收 3（SQL 口径）：dump 后 world_state 含 snapshot.* 留痕键（sim_day + 文件路径）。"""
    p, tmp = pool
    out = await dump_snapshot(p, sim_day=dt.date(2026, 10, 12), out_dir=tmp, tick=1, sim_now=T0,
                              compression_ratio=3.0)
    rows = await p.fetch("SELECT key, value::text FROM world_state WHERE key LIKE 'snapshot.%'")
    assert rows and rows[0]["key"] == "snapshot.latest"
    value = json.loads(rows[0]["value"])
    assert value["sim_day"] == "2026-10-12" and value["path"] == out["path"]


async def test_digest_events_day_grouping(pool) -> None:
    """events 流 digest_events（05 §2.2 公式）：按本地模拟日分组、确定性、键白名单剥除生效。"""
    p, _tmp = pool
    day = dt.date(2026, 10, 12)
    for i, (sim, typ, vis, payload) in enumerate([
        (T0, "agent.move", "public", {"from": "a", "to": "b", "sim_cost_min": 5, "text_raw": "剥除我"}),
        (T0 - dt.timedelta(minutes=30), "agent.think", "internal", {"topic_hint": "x"}),
        (T0 + dt.timedelta(days=1), "agent.think", "internal", {"topic_hint": "次日"}),
    ]):
        await p.execute(
            """
            INSERT INTO events (tick, sim_time, type, source, trigger, actors, rng_seed, visibility, payload)
            VALUES ($1, $2, $3, 'system', 'system', '{}', 1, $4, $5::jsonb)
            """,
            i + 1, sim, typ, vis, json.dumps(payload, ensure_ascii=False),
        )
    r1 = await digest_events(p, day)
    r2 = await digest_events(p, day)
    assert r1 == r2, "同输入同 digest（确定性）"
    assert r1["count"] == 2 and r1["sum_seq"] == 1 + 2
    assert r1["digest"].startswith("sha256:")
    # 手工复算：md5(canonical(剥除后 payload)) 按 seq 拼接再 sha256
    c1 = canonical_event_payload({"from": "a", "to": "b", "sim_cost_min": 5, "text_raw": "剥除我"},
                                 type_="agent.move", visibility="public")
    assert "剥除我" not in c1, "text_raw 恒剥除（保底 internal，06 §1.2）"
    import hashlib
    expect = "sha256:" + hashlib.sha256(
        (hashlib.md5(c1.encode()).hexdigest()
         + hashlib.md5(canonical_event_payload({"topic_hint": "x"}, type_="agent.think",
                                               visibility="internal").encode()).hexdigest()).encode()
    ).hexdigest()
    assert r1["digest"] == expect, "digest 公式 = 05 §2.2（sha256(string_agg(md5(canonical)) ORDER BY seq)）"
    r_next = await digest_events(p, day + dt.timedelta(days=1))
    assert r_next["count"] == 1 and r_next["digest"] != r1["digest"]


async def test_persona_public_derivations() -> None:
    """persona_display 派生口径：contrast_public 公开半 / speech_style_public 三要素（01 §11.1）。"""
    assert contrast_public("白天是画手，深夜是写手") == "白天是画手"
    assert contrast_public(None) is None
    assert speech_style_public({"tone": "克制", "sentence_len": "短", "catchphrase": "也行吧",
                                "taboo": ["炫耀"]}) == "克制 · 短句 · 口癖'也行吧'"
    assert "炫耀" not in (speech_style_public({"taboo": ["炫耀"], "tone": "平", "sentence_len": "短",
                                               "catchphrase": ""}) or "")
