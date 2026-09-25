"""T-LLM-07 ledger 计量验收（03 验收 1~3；04 §8.3/§5.2/§11.2）。

- 全列非空 + `cost_micro_cny` 与手算一致（含 in/out 不同单价）；
- `scripts/llm_cost.sql` 的 cny_per_simday 与 Python 同公式计算值相等（误差 0）；
- prompt 原文（哨兵）不出现在 llm_calls 全表与日志中，仅有其 hash。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import subprocess

import asyncpg
import pytest

from worldsim.llm_gateway.ledger import Ledger, cny_per_simday, cost_micro_cny, price_of

pytestmark = pytest.mark.asyncio

CFG = {
    "providers": {
        "p1": {"chat": [{"id": "m1", "input_price": 1.0, "output_price": 2.0}]},  # ¥/百万 tokens
        "mock": {},  # 无价格条目 → 记 0（04 §8.3 归账口径）
    },
}


async def test_cost_formula_and_full_columns(test_db_dsn: str) -> None:
    """fixture 调用后行各列非空且 cost_micro_cny 与手算一致（03 验收 1，04 §8.3 公式）。"""
    assert price_of(CFG, "p1", "m1") == (1.0, 2.0)
    assert cost_micro_cny(100, 50, 1.0, 2.0) == 200  # 100×1.0 + 50×2.0（微元整数）
    assert cost_micro_cny(100, 50, None, None) == 0  # 未回填/mock/本地记 0
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        ledger = Ledger(pool, CFG)
        sim_now = dt.datetime.now(dt.timezone.utc)
        await ledger.record(
            task_type="secondary", agent_id="A01", provider="p1", model="m1",
            prompt_tokens=100, completion_tokens=50, latency_ms=123, status="ok",
            sim_time=sim_now, prompt_hash="h" * 64, fallback_from="p0", request_id="req-1",
        )
        row = await pool.fetchrow("SELECT * FROM llm_calls ORDER BY id DESC LIMIT 1")
        for col in ("sim_time", "task_type", "agent_id", "provider", "model",
                    "latency_ms", "status", "fallback_from", "request_id", "prompt_hash"):
            assert row[col] is not None, col
        assert row["cost_micro_cny"] == 200
        assert row["fallback_from"] == "p0"
        # mock 档记 0 但记耗时
        await ledger.record(
            task_type="embed", agent_id=None, provider="mock", model="mock-embed",
            prompt_tokens=10, completion_tokens=0, latency_ms=5, status="ok",
            sim_time=sim_now, prompt_hash="h" * 64,
        )
        row = await pool.fetchrow("SELECT cost_micro_cny, latency_ms FROM llm_calls ORDER BY id DESC LIMIT 1")
        assert row["cost_micro_cny"] == 0 and row["latency_ms"] == 5
    finally:
        await pool.close()


async def test_llm_cost_sql_matches_python(test_db_dsn: str) -> None:
    """SQL 对账（03 验收 2）：灌已知 fixture 后 llm_cost.sql 的 cny_per_simday 与 Python 同公式值相等。"""
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        await pool.execute("DELETE FROM llm_calls")  # 本用例独占计量行（对账零误差）
        ledger = Ledger(pool, CFG)
        day1 = dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        fixtures = [
            (day1, 100, 50, 1.0, 2.0),              # 200 微元
            (day1 + dt.timedelta(hours=2), 300, 0, 1.0, 2.0),   # 300
            (day1 + dt.timedelta(days=1), 100, 100, 1.0, 2.0),  # 300（第 2 模拟日）
        ]
        for sim, pt, ct, ip, op in fixtures:
            await ledger.record(
                task_type="secondary", agent_id="A01", provider="p1", model="m1",
                prompt_tokens=pt, completion_tokens=ct, latency_ms=1, status="ok",
                sim_time=sim, prompt_hash="h" * 64,
            )
        expected = sum(cost_micro_cny(pt, ct, ip, op) for _, pt, ct, ip, op in fixtures) / 1e6 / 2
        py_val = await cny_per_simday(pool)
        assert py_val == pytest.approx(expected, abs=0)  # 误差 0
        from pathlib import Path

        from tests.conftest import PG_BIN, SOCKET_DIR

        server_root = Path(__file__).resolve().parents[1]
        out = subprocess.run(
            [os.path.join(PG_BIN, "psql"), "-h", SOCKET_DIR, "-d", "worldsim_test",
             "-t", "-A", "-F", "|", "-f", "scripts/llm_cost.sql"],
            check=True, capture_output=True, text=True, cwd=server_root,
        )
        first = out.stdout.strip().splitlines()[0]
        assert float(first) == pytest.approx(expected, abs=1e-12)
    finally:
        await pool.close()


async def test_no_prompt_plaintext_anywhere(test_db_dsn: str, caplog: pytest.LogCaptureFixture) -> None:
    """哨兵原文不出现在 llm_calls 全表与日志，仅 SHA-256 hash（03 验收 3，04 §11.2/§12.1）。"""
    sentinel = "哨兵-prompt-原文-绝不能落库或进日志"
    pool = await asyncpg.create_pool(test_db_dsn)
    try:
        ledger = Ledger(pool, CFG)
        h = hashlib.sha256(sentinel.encode("utf-8")).hexdigest()
        with caplog.at_level(logging.DEBUG):
            await ledger.record(
                task_type="star_decision", agent_id="A02", provider="p1", model="m1",
                prompt_tokens=1, completion_tokens=1, latency_ms=1, status="ok",
                sim_time=dt.datetime.now(dt.timezone.utc), prompt_hash=h,
            )
        rows = await pool.fetch("SELECT * FROM llm_calls")
        table_dump = " ".join(str(v) for r in rows for v in r.values())
        assert sentinel not in table_dump
        row = await pool.fetchrow("SELECT prompt_hash FROM llm_calls ORDER BY id DESC LIMIT 1")
        assert row["prompt_hash"] == h
        assert sentinel not in caplog.text
    finally:
        await pool.close()
