"""T-AUD-09 脱敏剧本导出验收（01 §10.1；红线 7 internal 零文本）。"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_export_script_test"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
D1, D2, D3, D4 = (dt.date(2026, 10, 12) + dt.timedelta(days=i) for i in range(4))
SENTINEL = "内部未过审原文绝不出现"


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def _chat(pool, seq: int, day: dt.date, hh: int, text: str, grade: str | None,
                *, visibility: str = "public", minutes: int = 0) -> None:
    st = dt.datetime.combine(day, dt.time(hh, minutes), tzinfo=LOCAL_TZ)
    await pool.execute(
        """
        INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, actors,
                            location_id, payload, visibility, ui)
        OVERRIDING SYSTEM VALUE
        VALUES ($1,$1,$2,$2,'dialogue.chat','agent:A01','autonomous','{A01,A02}','corp.pantry',
                $3::jsonb,$4,$5::jsonb)
        """, seq, st,
        json.dumps({"participants": ["A01", "A02"], "mode": "chat", "topic_ids": [],
                    "lines": [{"speaker": "A01", "text": text, "at_offset_s": 0}],
                    "witnesses": [], "text_display": text}, ensure_ascii=False),
        visibility, json.dumps({"grade": grade}) if grade else None)


async def test_window_extends_until_three_a_level(pool, tmp_path) -> None:
    """验收 2a：A 级不足 → 顺延取样窗（窗内 2 个 A，第 4 日第 3 个 A）。"""
    await _chat(pool, 1, D1, 10, "第一天的话", "A")
    await _chat(pool, 2, D2, 10, "第二天的话", "A")
    await _chat(pool, 3, D3, 10, "第三天普通", "C")
    await _chat(pool, 4, D4, 10, "第四天才有第三个名场面", "A")
    out = tmp_path / "script.txt"
    dsn = f"postgresql:///{DB_NAME}?host=/tmp"
    r = subprocess.run(
        ["uv", "run", "python", "scripts/export_script.py", "--from", D1.isoformat(),
         "--to", D3.isoformat(), "--out", str(out), "--dsn", dsn],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "顺延" not in r.stderr or "A 级 3 个" in r.stderr
    text = out.read_text(encoding="utf-8")
    assert "第四天才有第三个名场面" in text  # 窗顺延到 D4


async def test_internal_events_never_leak_text(pool, tmp_path) -> None:
    """验收 2b：visibility='internal' 事件零文本进剧本（红线 7）；脱敏 grep 无系统字段与真名。"""
    for i, day in enumerate((D1, D2, D3)):
        await _chat(pool, i + 1, day, 10, f"第{i + 1}天的名场面", "A")
    # internal + text_raw（被 block 形态）
    await pool.execute(
        """
        INSERT INTO events (seq, tick, sim_time, wall_time, type, source, trigger, actors,
                            payload, visibility)
        OVERRIDING SYSTEM VALUE
        VALUES (90, 90, $1, $1, 'dialogue.chat', 'agent:A01', 'autonomous', '{A01,A02}',
                $2::jsonb, 'internal')
        """, dt.datetime.combine(D2, dt.time(12, 0), tzinfo=LOCAL_TZ),
        json.dumps({"participants": ["A01", "A02"], "text_raw": SENTINEL}, ensure_ascii=False))
    out = tmp_path / "script.txt"
    dsn = f"postgresql:///{DB_NAME}?host=/tmp"
    r = subprocess.run(
        ["uv", "run", "python", "scripts/export_script.py", "--from", D1.isoformat(),
         "--to", D3.isoformat(), "--out", str(out), "--dsn", dsn],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    assert SENTINEL not in text
    assert not re.search(r"affinity|trigger|arc_id|tick", text)
    # 真名零泄露（seed_8 全名清单）
    names = [r["name"] for r in await pool.fetch("SELECT name FROM agents")]
    assert all(n not in text for n in names), "剧本含真名"
    assert re.search(r"[甲乙丙]", text)  # 化名生效


async def test_segments_under_300_chars(pool, tmp_path) -> None:
    """验收 2c：每段 ≤300 字（01 §10.1 步骤 3）。"""
    long_text = "今天我们在茶水间聊了很多很多关于项目排期的事情" * 3
    for i, day in enumerate((D1, D2, D3)):
        await _chat(pool, i + 1, day, 10, f"第{i + 1}天名场面", "A")
        for j in range(5):
            await _chat(pool, 10 + i * 10 + j, day, 14, long_text, None, minutes=j)
    out = tmp_path / "script.txt"
    dsn = f"postgresql:///{DB_NAME}?host=/tmp"
    r = subprocess.run(
        ["uv", "run", "python", "scripts/export_script.py", "--from", D1.isoformat(),
         "--to", D3.isoformat(), "--out", str(out), "--dsn", dsn],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    for block in re.split(r"\n\s*\n", text):
        body = "\n".join(line for line in block.splitlines() if not line.startswith("【"))
        assert len(body) <= 300, f"段落超长 {len(body)}：{body[:50]}…"
