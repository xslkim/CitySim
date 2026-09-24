"""T-DB-03 主库白名单视图测试（05 §2 脱敏口径，M0 最小集）。

跑 worldsim_test（schema_v1 之上，模块级 fixture 一次性灌 ddl/obs_views_v1.sql；
obs_views 内 \\ir 引入派生种子 obs_whitelist_seed.sql，单源生成链 00 §1 A11）。
行集隔离：agents 用 A04/A05（避开 test_ddl_contract 的 A01~A03/A40）。
注：asyncpg 默认将 jsonb 返回为 str，统一经 `_payload()` json.loads 后断言。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import asyncpg
import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
PG_BIN = os.path.expanduser("~/pgsql/bin")


@pytest.fixture(scope="module")
def obs_dsn(test_db_dsn: str) -> str:
    """在共享 worldsim_test 上一次性应用 obs 层（仅本模块消费）。"""
    subprocess.run(
        [os.path.join(PG_BIN, "psql"), "-h", "/tmp", "-d", "worldsim_test",
         "-v", "ON_ERROR_STOP=1", "-f", str(SERVER_ROOT / "ddl" / "obs_views_v1.sql")],
        check=True, capture_output=True, text=True,
    )
    return test_db_dsn


async def _insert_event(
    conn: asyncpg.Connection, tick: int, etype: str, visibility: str, payload: str
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, payload, visibility)
        VALUES ($1, now(), $2, 'system', 'system', $3::jsonb, $4)
        RETURNING seq
        """,
        tick, etype, payload, visibility,
    )


async def _payload(conn: asyncpg.Connection, seq: int) -> dict:
    raw = await conn.fetchval("SELECT payload::text FROM obs.events WHERE seq = $1", seq)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_whitelist_seed_applied(obs_dsn: str) -> None:
    """种子 INSERT 随 obs_views_v1.sql 落库（单源生成链派生物，非空）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        n = await conn.fetchval("SELECT count(*) FROM obs.payload_key_whitelist")
        assert n > 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_text_raw_never_out(obs_dsn: str) -> None:
    """public 事件含 text_raw：视图行 payload ? 'text_raw' 为假（00 §4 红线 7）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        seq = await _insert_event(
            conn, 9301, "world.announce", "public",
            '{"title":"停水通知","body":"今晚 22:00","scope":"all","text_display":"各位租客请注意","text_raw":"RAW_SECRET"}',
        )
        payload = await _payload(conn, seq)
        assert "text_raw" not in payload
        assert payload["title"] == "停水通知"
        assert payload["text_display"] == "各位租客请注意"  # public 事件 conditional 键放行
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_conditional_text_display(obs_dsn: str) -> None:
    """public dialogue.chat 保留 text_display；internal state.needs_delta 剥 text_display 但保留 changes[] 全结构。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        seq_pub = await _insert_event(
            conn, 9302, "dialogue.chat", "public",
            '{"participants":["A01","A02"],"mode":"small","topic_ids":["t1"],'
            '"lines":[{"speaker":"A01","text_display":"早","at_offset_s":0.0}],"witnesses":[],"text_display":"早晨闲聊"}',
        )
        pub = await _payload(conn, seq_pub)
        assert pub["text_display"] == "早晨闲聊"
        assert pub["lines"][0]["at_offset_s"] == 0.0  # lines[] 含逐句时间表（06 §2）

        seq_int = await _insert_event(
            conn, 9303, "state.needs_delta", "internal",
            '{"changes":[{"agent_id":"A01","need":"hunger","delta":-4,"new_value":61,"cause":"1089"}],'
            '"text_display":"不该出站"}',
        )
        internal = await _payload(conn, seq_int)
        assert "text_display" not in internal  # internal 事件不携带展示文本（05 §2.1）
        (change,) = internal["changes"]
        assert change == {"agent_id": "A01", "need": "hunger", "delta": -4, "new_value": 61, "cause": "1089"}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_unregistered_key_stripped(obs_dsn: str) -> None:
    """未登记键一律剥除（06 §1.2 默认剥除原则）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        seq = await _insert_event(
            conn, 9304, "world.announce", "public",
            '{"title":"x","scope":"floor","zzz_unregistered":42}',
        )
        payload = await _payload(conn, seq)
        assert "zzz_unregistered" not in payload
        assert payload["title"] == "x"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_promoted_split(obs_dsn: str) -> None:
    """agent.promoted：from_tier/to_tier/reason?/caused_by? 放行；score 分解/gini 剥除（06 §1.2 该行 internal 标注）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        seq = await _insert_event(
            conn, 9305, "agent.promoted", "internal",
            '{"from_tier":"secondary","to_tier":"star","reason":"event_driven","caused_by":"1089",'
            '"score":{"gini_part":0.4,"act_part":0.6},"gini":0.51}',
        )
        payload = await _payload(conn, seq)
        assert payload == {"from_tier": "secondary", "to_tier": "star", "reason": "event_driven", "caused_by": "1089"}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_reflection_display_only(obs_dsn: str) -> None:
    """agent.reflection 白名单仅 text_display 且条件放行（display-only，06 §1.2 N-P1-2）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        seq_pub = await _insert_event(
            conn, 9306, "agent.reflection", "public",
            '{"text_display":"💭 他今天话里有话","text_raw":"RAW","content":"反思长文不出站"}',
        )
        pub = await _payload(conn, seq_pub)
        assert pub == {"text_display": "💭 他今天话里有话"}

        seq_int = await _insert_event(
            conn, 9307, "agent.reflection", "internal",
            '{"text_display":"internal 不放行","text_raw":"RAW"}',
        )
        internal = await _payload(conn, seq_int)
        assert internal == {}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_obs_ro_readonly(obs_dsn: str) -> None:
    """SET ROLE obs_ro：可 SELECT 两视图；对 public.events SELECT/UPDATE 均拒绝（05 §5 双通道）。"""
    conn = await asyncpg.connect(obs_dsn)
    try:
        for aid in ("A04", "A05"):
            await conn.execute(
                "INSERT INTO agents (id, name, gender, age, persona, needs, position)"
                " VALUES ($1, '测试', 'F', 26, '{}', '{}', 'apt.L2.204')", aid,
            )
        await conn.execute(
            """
            INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance)
            VALUES ('A04', now(), 'reflection', 'RAW', '展示文本', 8),
                   ('A05', now(), 'reflection', 'RAW', NULL, 5)
            """
        )
        await conn.execute("SET ROLE obs_ro")
        assert await conn.fetchval("SELECT count(*) FROM obs.events") >= 1
        row = await conn.fetchrow("SELECT memory_id, agent_id, content_display FROM obs.memory_projection")
        assert row["agent_id"] == "A04" and row["content_display"] == "展示文本"
        # content_display IS NULL 的行不出站（"已出行"口径）
        assert await conn.fetchval("SELECT count(*) FROM obs.memory_projection WHERE agent_id = 'A05'") == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetchval("SELECT count(*) FROM public.events")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE public.events SET tick = 0")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_unique_filter_function_naming() -> None:
    """唯一函数/唯一表口径（00 §1 A12）：server 内无禁用命名（拼接构造，避免本文件自身命中验收 grep）。"""
    needles = ("obs_" + "sanitize_payload", "obs_" + "payload_whitelist")
    hits: list[str] = []
    for path in list(SERVER_ROOT.rglob("*.sql")) + list(SERVER_ROOT.rglob("*.py")):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                hits.append(f"{path.relative_to(SERVER_ROOT)}: {needle}")
    assert hits == []
