#!/usr/bin/env python3
"""WSIM_REPLAY_MODE=replay 重放对账（02 T-ADJ-08，M1 出口；04 §5.3/§13 W2 首条、09 §2 E3）。

用法（00 §1 A14，cwd = server/）：

    WSIM_REPLAY_MODE=replay uv run python scripts/replay_check.py --sim-day 1

对账流程（重放 = 从事件流重建，**不重算世界**，04 §5.3）：

1. 基线 = seed 确定性再生成（`scripts/seed.py` 同口径：agents.yaml needs/position/relations_initial；
   基线是"世界创世"的确定性产物，非重算）；
2. 重放 = 基线 + Σ 事件流（needs/mood 由 `state.needs_delta`、关系由 `relation.changed`、
   位置由 `agent.move` 链重建，04 §6.5/§5.3），规则骰子一律取 `events.rng_seed`，时钟以事件流
   `sim_time` 为准；
3. 与 `agents.needs`/`relations`/`agents.position` 缓存列逐项对账（04 §10.1 ⑥ 同口径），
   输出对账报告；任一不符 → 退出码 1。

零 LLM 调用：本脚本纯 SQL + 确定性推导，不构造网关；replay 模式下一切真实 LLM 调用被
门面 `ReplayViolation` 拦截（守卫唯一归 03 T-LLM-01，`worldsim/llm_gateway/__init__.py`）。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
sys.path.insert(0, str(SERVER_ROOT))           # worldsim 包
sys.path.insert(0, str(SERVER_ROOT / "scripts"))  # seed.py（基线同口径复用）

from worldsim.adjudicator.state_events import (  # noqa: E402
    AFFINITY_MAX, AFFINITY_MIN, NEED_MAX, NEED_MIN, TENSION_MAX, TENSION_MIN,
)
from worldsim.main import _load_root_env  # noqa: E402

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


# ---- 基线（seed 确定性再生成） ---------------------------------------------------


def baseline_state(agent_ids: list[str]) -> dict[str, Any]:
    """seed 基线：needs / position / relations（同对合并口径与 seed.py build_sql 一致）。"""
    from seed import _load_configs, _needs_of, _position_of

    all_agents, _world = _load_configs()
    by_id = {a["agent_id"]: a for a in all_agents}
    id_set = set(agent_ids)
    needs: dict[str, dict[str, float]] = {}
    positions: dict[str, str] = {}
    relations: dict[tuple[str, str], tuple[int, int, list[str]]] = {}
    for aid in agent_ids:
        a = by_id[aid]
        needs[aid] = {k: float(v) for k, v in _needs_of(a).items()}
        positions[aid] = _position_of(a)
        merged: dict[str, dict] = {}
        for r in a.get("relations_initial", []):
            if r["target"] not in id_set:
                continue
            m = merged.setdefault(r["target"], {"labels": [], "affinity": 0, "tension": 0})
            if r["type"] not in m["labels"]:
                m["labels"].append(r["type"])
            m["affinity"] += r["affinity"]
            m["tension"] += r["tension"]
        for target, m in merged.items():
            relations[(aid, target)] = (
                max(AFFINITY_MIN, min(AFFINITY_MAX, m["affinity"])),
                max(TENSION_MIN, min(TENSION_MAX, m["tension"])),
                sorted(m["labels"]),
            )
    return {"needs": needs, "positions": positions, "relations": relations}


# ---- 重放（基线 + Σ 事件流；04 §6.5/§5.3） ----------------------------------------


def replay_rng_seed(event: dict[str, Any]) -> int:
    """规则骰子一律从 `events.rng_seed` 取数（04 §5.3）。"""
    if event.get("rng_seed") is None:
        raise ValueError(f"事件 seq={event.get('seq')} 缺 rng_seed（04 §5.2 规则骰子留痕）")
    return int(event["rng_seed"])


async def replay_needs(pool: Any, agent_id: str, initial: dict[str, float], cutoff: dt.datetime) -> dict[str, float]:
    """needs/mood 重建 = 基线 + Σ state.needs_delta（cutoff 前，按 seq 序逐条施加，含 clamp）。"""
    values = {k: round(float(v), 2) for k, v in initial.items()}
    rows = await pool.fetch(
        """
        SELECT payload->'changes' AS changes FROM events
        WHERE type='state.needs_delta' AND $1 = ANY(actors) AND sim_time < $2 ORDER BY seq
        """,
        agent_id, cutoff,
    )
    for row in rows:
        changes = row["changes"]
        if isinstance(changes, str):
            changes = json.loads(changes)
        for c in changes:
            if c.get("agent_id") != agent_id:
                continue
            old = float(values.get(c["need"], 0.0))
            values[c["need"]] = round(max(NEED_MIN, min(NEED_MAX, old + float(c["delta"]))), 2)
    return values


async def replay_relation(
    pool: Any, a_id: str, b_id: str, initial: tuple[int, int, list[str]], cutoff: dt.datetime,
) -> tuple[int, int, list[str]]:
    """单条关系边重建 = 基线 + Σ relation.changed（cutoff 前，按 seq 序；labels 施加增删）。"""
    aff, ten, labels = initial[0], initial[1], set(initial[2])
    rows = await pool.fetch(
        """
        SELECT payload->'changes' AS changes FROM events
        WHERE type='relation.changed' AND $1 = ANY(actors) AND $2 = ANY(actors) AND sim_time < $3
        ORDER BY seq
        """,
        a_id, b_id, cutoff,
    )
    for row in rows:
        changes = row["changes"]
        if isinstance(changes, str):
            changes = json.loads(changes)
        for c in changes:
            if c.get("a_id") != a_id or c.get("b_id") != b_id:
                continue
            aff = max(AFFINITY_MIN, min(AFFINITY_MAX, aff + int(c["delta_affinity"])))
            ten = max(TENSION_MIN, min(TENSION_MAX, ten + int(c["delta_tension"])))
            labels = (labels | set(c.get("labels_added", []))) - set(c.get("labels_removed", []))
    return aff, ten, sorted(labels)


async def replay_positions(pool: Any, baseline: dict[str, str], cutoff: dt.datetime) -> dict[str, str]:
    """位置重建 = 基线 + agent.move 链（cutoff 前每 agent 最近一次 move 的 payload.to）。"""
    positions = dict(baseline)
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (u.agent_id) u.agent_id, payload->>'to' AS to FROM events e
        CROSS JOIN LATERAL unnest(e.actors) AS u(agent_id)
        WHERE e.type='agent.move' AND e.sim_time < $1
        ORDER BY u.agent_id, e.sim_time DESC, e.seq DESC
        """,
        cutoff,
    )
    for r in rows:
        if r["agent_id"] in positions:
            positions[r["agent_id"]] = r["to"]
    return positions


# ---- 对账 ------------------------------------------------------------------------


async def reconcile(pool: Any, *, sim_day: int) -> dict[str, Any]:
    anchor_raw = await pool.fetchval("SELECT value FROM world_state WHERE key='clock.anchor'")
    anchor = json.loads(anchor_raw) if isinstance(anchor_raw, str) else dict(anchor_raw or {})
    tick0 = dt.datetime.fromisoformat(anchor.get("tick0_sim") or anchor["anchor_sim"]).astimezone(LOCAL_TZ)
    day_start = (tick0 + dt.timedelta(days=sim_day - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = day_start + dt.timedelta(days=1)
    agent_ids = [r["id"] for r in await pool.fetch("SELECT id FROM agents ORDER BY id")]
    base = baseline_state(agent_ids)
    report: dict[str, Any] = {"sim_day": sim_day, "cutoff": cutoff.isoformat(), "mismatches": []}

    # 时钟以事件流 sim_time 为准（04 §5.3）：对账窗口外事件须为空（实跑恰为该模拟日）
    beyond = await pool.fetchval("SELECT count(*) FROM events WHERE sim_time >= $1", cutoff)
    if beyond:
        report["mismatches"].append(f"对账窗口外事件 {beyond} 条（实跑须恰为第 {sim_day} 模拟日）")
        return report

    # needs/mood（04 §10.1 ⑥ 同口径：缓存值 == 基线 + Σ state.needs_delta）
    for aid in agent_ids:
        rebuilt = await replay_needs(pool, aid, base["needs"][aid], cutoff)
        cache_raw = await pool.fetchval("SELECT needs FROM agents WHERE id=$1", aid)
        cache = {k: round(float(v), 2) for k, v in (json.loads(cache_raw) if isinstance(cache_raw, str) else dict(cache_raw)).items()}
        for k in sorted(set(rebuilt) | set(cache)):
            if abs(float(rebuilt.get(k, 0.0)) - float(cache.get(k, 0.0))) > 1e-9:
                report["mismatches"].append(
                    f"needs[{aid}][{k}] 缓存={cache.get(k)} 重放={rebuilt.get(k)}"
                )

    # relations（基线 + Σ relation.changed；覆盖基线对与被事件触碰的对）
    pairs = set(base["relations"])
    rows = await pool.fetch(
        """
        SELECT DISTINCT c->>'a_id' AS a, c->>'b_id' AS b FROM events e,
        LATERAL jsonb_array_elements(e.payload->'changes') c
        WHERE e.type='relation.changed' AND e.sim_time < $1
        """,
        cutoff,
    )
    pairs.update((r["a"], r["b"]) for r in rows)
    for a, b in sorted(pairs):
        rebuilt = await replay_relation(pool, a, b, base["relations"].get((a, b), (0, 0, [])), cutoff)
        row = await pool.fetchrow(
            "SELECT affinity, tension, labels FROM relations WHERE a_id=$1 AND b_id=$2", a, b,
        )
        cache = (int(row["affinity"]), int(row["tension"]), sorted(row["labels"])) if row else (0, 0, [])
        if rebuilt != cache:
            report["mismatches"].append(f"relations[{a}→{b}] 缓存={cache} 重放={rebuilt}")

    # positions（基线 + agent.move 链）
    positions = await replay_positions(pool, base["positions"], cutoff)
    for aid, pos in sorted(positions.items()):
        cache = await pool.fetchval("SELECT position FROM agents WHERE id=$1", aid)
        if cache != pos:
            report["mismatches"].append(f"position[{aid}] 缓存={cache} 重放={pos}")

    report["agents"] = len(agent_ids)
    report["events"] = await pool.fetchval("SELECT count(*) FROM events WHERE sim_time < $1", cutoff)
    return report


async def _main(args: argparse.Namespace) -> int:
    _load_root_env()
    mode = os.environ.get("WSIM_REPLAY_MODE", "off")
    if mode != "replay":
        print("本脚本须在 WSIM_REPLAY_MODE=replay 下运行（04 §5.3）", file=sys.stderr)
        return 2
    dsn = args.dsn or os.environ.get("WSIM_PG_DSN")
    if not dsn:
        print("缺主库 DSN：--dsn 或 env WSIM_PG_DSN（根 .env）", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        llm_before = await pool.fetchval("SELECT count(*) FROM llm_calls")
        report = await reconcile(pool, sim_day=args.sim_day)
        llm_after = await pool.fetchval("SELECT count(*) FROM llm_calls")
    finally:
        await pool.close()
    report["llm_calls_delta"] = llm_after - llm_before
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["llm_calls_delta"] != 0:
        print(f"FAIL：replay 期间 llm_calls 增量 = {report['llm_calls_delta']}（须为 0，04 §5.3）", file=sys.stderr)
        return 1
    if report["mismatches"]:
        print(f"FAIL：{len(report['mismatches'])} 项不符", file=sys.stderr)
        return 1
    print(f"OK：第 {report['sim_day']} 模拟日重放对账一致"
          f"（agents={report['agents']} events={report['events']} llm_calls 增量=0）")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="replay_check.py", description="WSIM_REPLAY_MODE=replay 重放对账（02 T-ADJ-08）")
    p.add_argument("--sim-day", type=int, required=True, help="对账的模拟日序号（1 起）")
    p.add_argument("--dsn", default=None, help="主库 DSN（缺省 env WSIM_PG_DSN，根 .env 兜底）")
    args = p.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
