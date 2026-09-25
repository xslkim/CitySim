#!/usr/bin/env python3
"""obs 派生三实体表按模拟日重算（05 T-WEB-01；05 §4.2 派生任务的主库侧对应物）。

- `obs.world_state_snapshot`：内核快照白名单子集文件（`var/snapshot/snapshot_<day>.whitelist.json.gz`，
  02 T-ADJ-09 落盘）按 05 §3.6 字段白名单投影入库；digest 复用 sidecar/重算（canonical 唯一实现 =
  `worldsim/snapshot/canonical.py`，05 §2.2）。
- `obs.relation_daily`：递推口径 05 §3.4——`relation_daily(d) = relation_daily(d−1) + Σ relation_change_log(d)`；
  labels 不递推，取当日 `world_state_snapshot(d)` 关系矩阵；首日 = 基线矩阵。按 sim_day DELETE+INSERT 单事务幂等。
- `obs.ripple_edge`：gossip `payload.cites` × `obs.memory_projection.source_event_seq` 递归（05 §3.7/§4.2
  compute_ripple_edge）：cites 无 source_event_seq（纯反思/摘要/计划记忆）跳过（P2-9）；源为 gossip → 取其已有
  全部 root、hop+1，超上限弃链（上限读 `config/relations.yaml` gossip.hop_max，01 §4.1 镜像，不写死字面量）；
  distortion = levenshtein(dst text_display, 被引用记忆 content_display)/max(len)（归一化口径 05 文档 D3）。
- health_daily 不在自算范围（唯一权威 = 08 T-AUD-08 metrics.py；obs 侧为透传 VIEW，05 T-WEB-01）。

用法（00 §1 A14）：
  cd server && uv run python scripts/obs_refresh.py --day 2026-10-12
  cd server && uv run python scripts/obs_refresh.py --from 2026-10-12 --to 2026-10-14
  cd server && uv run python scripts/obs_refresh.py --all
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gzip
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg
import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "var" / "snapshot"
RELATIONS_CFG = SERVER_ROOT / "config" / "relations.yaml"

log = logging.getLogger("obs_refresh")

LOCAL_TZ_NAME = "Asia/Shanghai"  # 模拟日聚合时区（01 文档 §6 D4；与 obs_derived_v1.sql 同口径）


def load_hop_max(cfg_path: Path = RELATIONS_CFG) -> int:
    """涟漪 hop 深度上限：唯一持有方 01 §4.1，镜像键 = relations.yaml gossip.hop_max（05 文档偏差表登记）。"""
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    try:
        return int(cfg["gossip"]["hop_max"])
    except (KeyError, TypeError) as e:
        raise ValueError(f"{cfg_path} 缺 gossip.hop_max（01 §4.1 镜像）") from e


def load_snapshot_whitelist(snapshot_dir: Path, sim_day: dt.date) -> tuple[dict[str, Any], str]:
    """读内核快照白名单子集 + digest（sidecar 优先，缺则按 canonical 重算）。"""
    day = sim_day.isoformat()
    wl_path = snapshot_dir / f"snapshot_{day}.whitelist.json.gz"
    if not wl_path.exists():
        raise FileNotFoundError(f"缺内核快照白名单文件：{wl_path}（该模拟日不入库 world_state_snapshot）")
    with gzip.open(wl_path, "rt", encoding="utf-8") as f:
        state = json.load(f)
    dg_path = snapshot_dir / f"snapshot_{day}.digest"
    if dg_path.exists():
        digest = dg_path.read_text(encoding="utf-8").strip()
    else:
        from worldsim.snapshot.canonical import canonical, sha256_hex

        digest = "sha256:" + sha256_hex(canonical(state))
    return state, digest


async def refresh_world_state_snapshot(pool: Any, snapshot_dir: Path, sim_day: dt.date) -> int:
    try:
        state, digest = load_snapshot_whitelist(snapshot_dir, sim_day)
    except FileNotFoundError as e:
        log.warning("%s", e)
        return 0
    await pool.execute(
        """
        INSERT INTO obs.world_state_snapshot (sim_day, state, digest)
        VALUES ($1, $2::jsonb, $3)
        ON CONFLICT (sim_day) DO UPDATE SET state = $2::jsonb, digest = $3
        """,
        sim_day, json.dumps(state, ensure_ascii=False), digest,
    )
    return 1


async def refresh_relation_daily(pool: Any, sim_day: dt.date) -> int:
    """05 §3.4 递推：prev(d−1) + Σ relation_change_log(d)；labels 取当日快照关系矩阵；首日 = 快照基线。"""
    prev_day = sim_day - dt.timedelta(days=1)
    prev = {
        (r["a_id"], r["b_id"]): [int(r["affinity"]), int(r["tension"])]
        for r in await pool.fetch(
            "SELECT a_id, b_id, affinity, tension FROM obs.relation_daily WHERE sim_day = $1", prev_day,
        )
    }
    snap_labels: dict[tuple[str, str], list[str]] = {}
    if not prev:
        # 首日基线 = 当日快照 relations 矩阵（05 §3.4"首日 = 基线矩阵"）
        state = await pool.fetchval(
            "SELECT state::text FROM obs.world_state_snapshot WHERE sim_day = $1", sim_day,
        )
        if state is None:
            log.warning("relation_daily(%s)：无前一日递推基线且无当日快照，跳过", sim_day)
            return 0
        for rel in json.loads(state).get("relations") or []:
            prev[(rel["a"], rel["b"])] = [int(rel["affinity"]), int(rel["tension"])]
    snap_state = await pool.fetchval(
        "SELECT state::text FROM obs.world_state_snapshot WHERE sim_day = $1", sim_day,
    )
    if snap_state is not None:
        for rel in json.loads(snap_state).get("relations") or []:
            snap_labels[(rel["a"], rel["b"])] = sorted(rel.get("labels") or [])
    deltas = await pool.fetch(
        """
        SELECT a_id, b_id, SUM(delta_affinity) AS da, SUM(delta_tension) AS dtn
        FROM obs.relation_change_log WHERE sim_day = $1 GROUP BY a_id, b_id
        """,
        sim_day,
    )
    for r in deltas:
        cell = prev.setdefault((r["a_id"], r["b_id"]), [0, 0])
        cell[0] += int(r["da"])
        cell[1] += int(r["dtn"])
    rows = [
        (sim_day, a, b, aff, ten, snap_labels.get((a, b), []))
        for (a, b), (aff, ten) in sorted(prev.items())
    ]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM obs.relation_daily WHERE sim_day = $1", sim_day)
        await conn.executemany(
            "INSERT INTO obs.relation_daily (sim_day, a_id, b_id, affinity, tension, labels)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            rows,
        )
    return len(rows)


async def refresh_ripple_edge(pool: Any, sim_day: dt.date, hop_max: int) -> int:
    """05 §3.7/§4.2 compute_ripple_edge：当日 gossip 事件按 seq 序逐条算入边（DELETE+重算幂等）。"""
    gossip_rows = await pool.fetch(
        """
        SELECT seq, payload::text AS payload, sim_time FROM obs.events
        WHERE type = 'dialogue.gossip'
          AND (sim_time AT TIME ZONE $2)::date = $1
        ORDER BY seq
        """,
        sim_day, LOCAL_TZ_NAME,
    )
    edges: list[tuple] = []
    local_roots: dict[int, list[tuple[int, int]]] = {}  # 当日已算入边（dst_seq → [(root, hop)]），供同日链递归
    for g in gossip_rows:
        payload = json.loads(g["payload"])
        cites = [int(c) for c in (payload.get("cites") or []) if str(c).isdigit()]
        if not cites:
            continue
        teller = payload.get("teller") or ""
        listener = payload.get("listener") or ""
        dst_text = payload.get("text_display") or ""
        mems = await pool.fetch(
            """
            SELECT memory_id, source_event_seq, content_display FROM obs.memory_projection
            WHERE memory_id = ANY($1::bigint[])
            """,
            cites,
        )
        for m in mems:
            src_seq = m["source_event_seq"]
            if src_seq is None:
                continue  # P2-9：纯反思/摘要/计划类记忆不进 ripple_edge，仅事件详情展示
            src_type = await pool.fetchval("SELECT type FROM obs.events WHERE seq = $1", src_seq)
            if src_type == "dialogue.gossip":
                roots = await pool.fetch(
                    "SELECT root_event_seq, hop FROM obs.ripple_edge WHERE dst_event_seq = $1", src_seq,
                )
                candidates = [(r["root_event_seq"], int(r["hop"]) + 1) for r in roots]
                candidates += [(root, hop + 1) for root, hop in local_roots.get(src_seq, [])]
            else:
                candidates = [(src_seq, 1)]
            for root_seq, hop in candidates:
                if hop > hop_max:
                    continue  # 超深度上限弃链（01 §4.1）
                src_text = m["content_display"] or ""
                distortion = await pool.fetchval(
                    """
                    SELECT CASE WHEN greatest(length($1), length($2)) = 0 THEN 0
                           ELSE levenshtein($1, $2)::numeric / greatest(length($1), length($2)) END
                    """,
                    dst_text, src_text,
                )
                edge = (root_seq, int(g["seq"]), src_seq, hop, teller, listener,
                        round(float(distortion), 3), g["sim_time"], sim_day)
                edges.append(edge)
                local_roots.setdefault(int(g["seq"]), [])
                if (root_seq, hop) not in local_roots[int(g["seq"])]:
                    local_roots[int(g["seq"])].append((root_seq, hop))
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM obs.ripple_edge WHERE sim_day = $1", sim_day)
        await conn.executemany(
            """
            INSERT INTO obs.ripple_edge
              (root_event_seq, dst_event_seq, src_event_seq, hop, teller_id, listener_id,
               distortion, sim_time, sim_day)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (root_event_seq, dst_event_seq) DO NOTHING
            """,
            edges,
        )
    return len(edges)


async def refresh_days(pool: Any, days: list[dt.date], snapshot_dir: Path, hop_max: int) -> None:
    for d in days:
        t0 = dt.datetime.now()
        n_snap = await refresh_world_state_snapshot(pool, snapshot_dir, d)
        n_rel = await refresh_relation_daily(pool, d)
        n_rip = await refresh_ripple_edge(pool, d, hop_max)
        log.info("obs_refresh day=%s snapshot=%s relation_daily=%s ripple_edge=%s 耗时=%.2fs",
                 d, n_snap, n_rel, n_rip, (dt.datetime.now() - t0).total_seconds())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="obs 派生三实体表按模拟日重算（05 T-WEB-01）")
    ap.add_argument("--day", action="append", default=[], help="模拟日 YYYY-MM-DD（可重复）")
    ap.add_argument("--from", dest="from_day", help="起始模拟日（含）")
    ap.add_argument("--to", dest="to_day", help="截止模拟日（含）")
    ap.add_argument("--all", action="store_true", help="覆盖全部已有快照的模拟日")
    ap.add_argument("--dsn", default=os.environ.get("WSIM_PG_DSN", ""))
    ap.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    return ap.parse_args(argv)


def resolve_days(args: argparse.Namespace) -> list[dt.date]:
    if args.all:
        days = []
        for p in sorted(Path(args.snapshot_dir).glob("snapshot_*.whitelist.json.gz")):
            day = p.name.removeprefix("snapshot_").removesuffix(".whitelist.json.gz")
            days.append(dt.date.fromisoformat(day))
        return days
    if args.from_day and args.to_day:
        d0, d1 = dt.date.fromisoformat(args.from_day), dt.date.fromisoformat(args.to_day)
        return [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]
    return [dt.date.fromisoformat(s) for s in args.day]


async def amain(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("WSIM_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    if not args.dsn:
        print("FAIL: 缺 DSN（--dsn 或 WSIM_PG_DSN）", file=sys.stderr)
        return 1
    days = resolve_days(args)
    if not days:
        print("FAIL: 未指定模拟日（--day/--from/--to/--all）", file=sys.stderr)
        return 1
    hop_max = load_hop_max()
    pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
    try:
        await refresh_days(pool, days, Path(args.snapshot_dir), hop_max)
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
