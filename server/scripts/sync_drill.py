#!/usr/bin/env python
"""T-SYN-11 断网追平演练驱动（04 §13 W1：正常推送/断网缓冲/恢复追平/digest 重灌闭环/必过项①②）。

口径与偏差：
- 断网以"停摄入 API 进程"模拟（loopback 无防火墙语义，04 §13 演练口径）；断网时长默认读
  `WSIM_DRILL_OUTAGE_S`（默认 3600，07 D2），演练可用小值加速并在报告中注明。
- 事件负载由本驱动直接写主库（确定性高吞吐；内核写路径不被 sync 阻塞已由
  tests/sync/test_ws_client.py::test_kernel_unblocked_when_down 单元覆盖）。
- 必过项① block→internal 判定 = internal 且 payload 携 text_raw（07 D3 工程默认）。
- 证据全部落 `--evidence` 目录（默认 server/scripts/sync_drill_evidence/）。

用法（cwd=server/，00 §1 A14）：`uv run python scripts/sync_drill.py [--outage-s N]`
退出码 0 = 全项通过。编排入口 = sync_drill.sh。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import asyncpg
import httpx

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from worldsim.ingest.derived import worker  # noqa: E402
from worldsim.snapshot.canonical import canonical, sha256_hex  # noqa: E402
from worldsim.snapshot.dump import dump_snapshot  # noqa: E402
from worldsim.sync.digest import build_push_body  # noqa: E402
from worldsim.sync.outbox import fetch_events_after, read_sync_state  # noqa: E402
from worldsim.sync.ws_client import SyncClient  # noqa: E402
from worldsim.time_engine.clock import LOCAL_TZ  # noqa: E402

PG_BIN = os.path.expanduser("~/pgsql/bin")
SOCKET = "/tmp"
MAIN_DB = "worldsim_drill"
REPLICA_DB = "worldsim_drill_replica"
TOKEN = "drill-token"
INGEST_PORT = 9177
SENTINEL = "机密原文SENTINEL禁止出站"

DAY1 = dt.date(2026, 11, 1)
DAY2 = dt.date(2026, 11, 2)
DAY3 = dt.date(2026, 11, 3)   # T（DAY1 ≤ T−2 入比对窗口）

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(f"{name}\t{detail}")
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)


def psql(sql: str, db: str = "postgres") -> str:
    r = subprocess.run([os.path.join(PG_BIN, "psql"), "-h", SOCKET, "-d", db,
                        "-v", "ON_ERROR_STOP=1", "-tAc", sql],
                       check=True, capture_output=True, text=True)
    return r.stdout.strip()


def psql_file(path: Path, db: str) -> None:
    subprocess.run([os.path.join(PG_BIN, "psql"), "-h", SOCKET, "-d", db,
                    "-v", "ON_ERROR_STOP=1", "-f", str(path)],
                   check=True, capture_output=True, text=True)


def build_dbs() -> None:
    ddl = SERVER_ROOT / "ddl"
    for name in (MAIN_DB, REPLICA_DB):
        psql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        psql(f"CREATE DATABASE {name}")
    psql("CREATE SCHEMA IF NOT EXISTS partman", db=MAIN_DB)
    psql("CREATE EXTENSION IF NOT EXISTS vector", db=MAIN_DB)
    psql("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman", db=MAIN_DB)
    psql_file(ddl / "schema_v1.sql", MAIN_DB)
    psql_file(ddl / "seed_8.sql", MAIN_DB)
    for f in ("replica_v1.sql", "replica_grants.sql", "replica_digest_log.sql",
              "replica_obs_views.sql"):
        psql_file(ddl / f, REPLICA_DB)


class EventGen:
    """合成事件负载（9 类型族；gossip 链/grade/blocked 特例单独构造）。"""

    TYPES = [
        ("agent.move", "internal", {"from": "home.a", "to": "corp.tech", "sim_cost_min": 5},
         ["A01"]),
        ("agent.work", "internal", {"task_id": "T-1", "sim_cost_min": 60}, ["A01"]),
        ("agent.eat", "internal", {"venue": "corp.canteen", "with": ["A02"],
                                   "amount_cents": 1500}, ["A01"]),
        ("dialogue.chat", "public",
         {"participants": ["A01", "A02"], "mode": "chat", "topic_ids": ["t1"],
          "lines": [{"speaker": "A01", "text": "今天中午吃什么", "at_offset_s": 0}],
          "witnesses": [], "text_display": "A01 和 A02 闲聊"}, ["A01", "A02"]),
        ("dialogue.argue", "public", {"participants": ["A03", "A04"], "reason_hint": "排期",
                                      "lines": [], "witnesses": []}, ["A03", "A04"]),
        ("social.refuse", "internal", {"from": "A05", "to": "A06", "request_ref": "1",
                                       "politeness": 1}, ["A05", "A06"]),
        ("state.needs_delta", "internal",
         {"changes": [{"agent_id": "A01", "need": "hunger", "delta": -3, "new_value": 60,
                       "cause": "work"}]}, ["A01"]),
        ("relation.changed", "internal",
         {"changes": [{"a_id": "A01", "b_id": "A02", "delta_affinity": 2, "delta_tension": 1,
                       "cause": "chat"}]}, ["A01", "A02"]),
        ("world.announce", "public", {"title": "公告", "body": "周五团建", "scope": "all",
                                      "text_display": "周五团建"}, []),
    ]

    def __init__(self) -> None:
        self.tick = 0
        self.rng = random.Random(20261101)

    def next(self, sim_time: dt.datetime) -> dict:
        self.tick += 1
        type_, vis, payload, actors = self.rng.choice(self.TYPES)
        ui = {"grade": self.rng.choice(["A", "B", "C"])} if type_.startswith("dialogue.") else None
        return {"tick": self.tick, "sim_time": sim_time, "type": type_, "visibility": vis,
                "payload": payload, "actors": actors, "ui": ui}


async def insert_event(pool, ev: dict) -> int:
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, actors, payload, visibility, ui)
        VALUES ($1,$2,$3,'agent:A01','autonomous',$4,$5::jsonb,$6,$7::jsonb) RETURNING seq
        """, ev["tick"], ev["sim_time"], ev["type"], ev["actors"],
        json.dumps(ev["payload"], ensure_ascii=False), ev["visibility"],
        json.dumps(ev["ui"]) if ev["ui"] else None)


async def insert_memory(pool, sim_time: dt.datetime, *, display: bool = True,
                        source_seq: int | None = None) -> int:
    return await pool.fetchval(
        """
        INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance,
                              source_event_seq)
        VALUES ('A01', $1, 'projection', '内部原文', $2, 5, $3) RETURNING id
        """, sim_time, "展示文本" if display else None, source_seq)


def start_ingest(evidence: Path) -> subprocess.Popen:
    log_f = open(evidence / "ingest.log", "ab")
    env = dict(os.environ)
    env["WSIM_REPLICA_PG_DSN"] = f"postgresql:///{REPLICA_DB}?host={SOCKET}"
    env["WSIM_INGEST_TOKEN"] = TOKEN
    return subprocess.Popen(
        ["uv", "run", "uvicorn", "worldsim.ingest:app", "--host", "127.0.0.1",
         "--port", str(INGEST_PORT)],
        cwd=SERVER_ROOT, env=env, stdout=log_f, stderr=subprocess.STDOUT)


async def wait_ingest(timeout: float = 30.0) -> None:
    async with httpx.AsyncClient() as c:
        for _ in range(int(timeout * 10)):
            try:
                r = await c.get(f"http://127.0.0.1:{INGEST_PORT}/v1/health",
                                headers={"Authorization": f"Bearer {TOKEN}"})
                if r.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("摄入 API 未就绪")


async def wait_caught_up(mp, rp, expected_count: int, timeout: float = 120.0) -> float:
    """等副本追平（行数口径，blocked 行除外由调用方折算）；返回耗时秒。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        got = await rp.fetchval("SELECT count(*) FROM events")
        if int(got) >= expected_count:
            return time.monotonic() - t0
        await asyncio.sleep(0.2)
    raise TimeoutError(f"追平超时：副本 {await rp.fetchval('SELECT count(*) FROM events')} "
                       f"< 期望 {expected_count}")


def run_digest_check(evidence: Path, tag: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["WSIM_PG_DSN"] = f"postgresql:///{MAIN_DB}?host={SOCKET}"
    env["WSIM_REPLICA_PG_DSN"] = f"postgresql:///{REPLICA_DB}?host={SOCKET}"
    r = subprocess.run(["uv", "run", "python", "scripts/digest_check.py"],
                       cwd=SERVER_ROOT, env=env, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    (evidence / f"digest_check_{tag}.txt").write_text(out, encoding="utf-8")
    return r.returncode, out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outage-s", type=float,
                    default=float(os.environ.get("WSIM_DRILL_OUTAGE_S", "3600")))
    ap.add_argument("--evidence", default=str(SERVER_ROOT / "scripts" / "sync_drill_evidence"))
    ap.add_argument("--outage-rate", type=float, default=10.0,
                    help="断网期事件生成速率（条/秒；10/s × 3600s ≈ 3.6 万 ≈ 04 §9.2 容量假设）")
    args = ap.parse_args()
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    frames_dump = evidence / "frames.dump"
    frames_dump.write_text("", encoding="utf-8")
    snap_dir = evidence / "snapshots"
    snap_dir.mkdir(exist_ok=True)

    print(f"[drill] 断网时长 {args.outage_s:.0f}s（WSIM_DRILL_OUTAGE_S 可覆盖，默认 3600）",
          flush=True)
    build_dbs()
    mp = await asyncpg.create_pool(f"postgresql:///{MAIN_DB}?host={SOCKET}",
                                   min_size=1, max_size=6)
    rp = await asyncpg.create_pool(f"postgresql:///{REPLICA_DB}?host={SOCKET}",
                                   min_size=1, max_size=6)
    ingest = start_ingest(evidence)
    client_task: asyncio.Task | None = None
    try:
        await wait_ingest()
        gen = EventGen()

        # ---- P0 构造（DAY1 含必过项①②素材与 gossip 链） ------------------------------
        t = dt.datetime.combine(DAY1, dt.time(9, 0), tzinfo=LOCAL_TZ)
        cand_seq = await insert_event(mp, {
            "tick": 900001, "sim_time": t, "type": "dialogue.chat", "visibility": "public",
            "payload": {"participants": ["A01", "A02"], "mode": "chat", "topic_ids": ["t1"],
                        "lines": [{"speaker": "A01", "text": "名场面台词", "at_offset_s": 0}],
                        "witnesses": [], "text_display": "名场面发生"},
            "actors": ["A01", "A02"], "ui": {"grade": "B"}})
        mem1 = await insert_memory(mp, t + dt.timedelta(minutes=5), source_seq=cand_seq)
        g1_seq = await insert_event(mp, {
            "tick": 900002, "sim_time": t + dt.timedelta(minutes=6), "type": "dialogue.gossip",
            "visibility": "public",
            "payload": {"teller": "A03", "listener": "A04", "about": "A01",
                        "cites": [str(mem1)], "lines": [], "text_display": "名场面发生"},
            "actors": ["A03", "A04"], "ui": None})
        mem2 = await insert_memory(mp, t + dt.timedelta(minutes=7), source_seq=g1_seq)
        await insert_event(mp, {
            "tick": 900003, "sim_time": t + dt.timedelta(minutes=8), "type": "dialogue.gossip",
            "visibility": "public",
            "payload": {"teller": "A04", "listener": "A05", "about": "A01",
                        "cites": [str(mem2)], "lines": [], "text_display": "名场面转述的转述变体"},
            "actors": ["A04", "A05"], "ui": None})
        blocked_seq = await insert_event(mp, {
            "tick": 900004, "sim_time": t + dt.timedelta(minutes=9), "type": "dialogue.chat",
            "visibility": "internal",
            "payload": {"participants": ["A01", "A02"], "text_raw": SENTINEL},
            "actors": ["A01", "A02"], "ui": None})
        await insert_memory(mp, t + dt.timedelta(minutes=10), display=False)  # 未过审不出站
        for day, n in ((DAY1, 200), (DAY2, 200), (DAY3, 50)):
            base = dt.datetime.combine(day, dt.time(10, 0), tzinfo=LOCAL_TZ)
            for i in range(n):
                await insert_event(mp, gen.next(base + dt.timedelta(seconds=30 * i)))
                if i % 10 == 0:
                    await insert_memory(mp, base + dt.timedelta(seconds=30 * i))
        await insert_event(mp, {  # 改判（DAY2）：candidate 上调 A
            "tick": 900005, "sim_time": dt.datetime.combine(DAY2, dt.time(8, 0),
                                                            tzinfo=LOCAL_TZ),
            "type": "director.grade_revise", "visibility": "public",
            "payload": {"target_seq": str(cand_seq), "new_grade": "A", "reason": "终审上调名场面"},
            "actors": [], "ui": None})
        for day in (DAY1, DAY2):
            await dump_snapshot(mp, sim_day=day, out_dir=snap_dir, tick=gen.tick,
                                sim_now=dt.datetime.combine(day + dt.timedelta(days=1),
                                                            dt.time(0, 0), tzinfo=LOCAL_TZ))
        main_count = int(await mp.fetchval("SELECT count(*) FROM events"))
        expected_count = main_count - 1  # blocked 行永不出站（07 D3）
        print(f"[drill] 主库事件 {main_count}（含 1 blocked 不出站行 seq={blocked_seq}）",
              flush=True)

        # ---- P1 正常推送 -----------------------------------------------------------
        client = SyncClient(mp, url=f"ws://127.0.0.1:{INGEST_PORT}/v1/ingest/ws", token=TOKEN,
                            frame_dump=str(frames_dump), snapshot_dir=str(snap_dir))
        latencies: list[float] = []

        orig_send = client.send_events_batch

        async def timed_send(events: list[dict]) -> dict:
            t0 = time.monotonic()
            ack = await orig_send(events)
            latencies.append(time.monotonic() - t0)  # 发出→ack(=副本事务提交) 样本
            return ack

        client.send_events_batch = timed_send  # type: ignore[method-assign]
        client_task = asyncio.create_task(client.run_forever(poll_s=0.2))
        catchup_s = await wait_caught_up(mp, rp, expected_count)
        state = await read_sync_state(mp)
        replica_max = int(await rp.fetchval("SELECT max(seq) FROM events"))
        main_max = int(await mp.fetchval("SELECT max(seq) FROM events"))
        check("正常推送追平", replica_max == main_max,
              f"replica max(seq)={replica_max} == main {main_max}（{catchup_s:.1f}s）")
        p95 = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else float("nan")
        check("端到端延迟 p95 ≤5s", p95 <= 5.0,
              f"p95={p95:.3f}s n={len(latencies)}（发出→ack 提交，04 §9.2 采样）")
        (evidence / "latency.txt").write_text(
            f"p95={p95:.4f}s n={len(latencies)} max={max(latencies):.4f}\n", encoding="utf-8")
        # 快照流到达
        snap_n = int(await rp.fetchval("SELECT count(*) FROM world_state_snapshot"))
        check("快照流两日到达", snap_n == 2, f"world_state_snapshot={snap_n}")
        mem_n = int(await rp.fetchval("SELECT count(*) FROM memory_projection"))
        mem_main = int(await mp.fetchval(
            "SELECT count(*) FROM memories WHERE content_display IS NOT NULL"))
        check("记忆投影已出行一致", mem_n == mem_main, f"replica={mem_n} main 已出行={mem_main}")

        # ---- 必过项②前半：目标行 hash（改判物化前基线） ------------------------------
        async def row_hash(seq: int) -> str:
            row = await rp.fetchrow("SELECT * FROM events WHERE seq=$1", seq)

            def _def(o: object) -> str:
                if isinstance(o, (dt.datetime, dt.date)):
                    return o.isoformat()
                raise TypeError

            plain = json.loads(json.dumps(dict(row), default=_def, ensure_ascii=False))
            return "md5:" + __import__("hashlib").md5(canonical(plain).encode()).hexdigest()

        hash_before = await row_hash(cand_seq)
        (evidence / "grade_row_hash_before.txt").write_text(hash_before + "\n", encoding="utf-8")

        # ---- 派生 worker 消费（relchg/ripple/grade/refresh） ---------------------------
        for _ in range(20):
            if await worker.run_once(rp) == 0:
                break
        grade_row = await rp.fetchrow(
            "SELECT grade, revised_by_seq FROM event_grade_view WHERE seq=$1", cand_seq)
        check("必过项② event_grade_view 最新 grade 生效",
              grade_row and grade_row["grade"] == "A" and grade_row["revised_by_seq"] is not None,
              f"grade={grade_row['grade'] if grade_row else None} "
              f"revised_by={grade_row['revised_by_seq'] if grade_row else None}")
        hash_after = await row_hash(cand_seq)
        (evidence / "grade_row_hash_after.txt").write_text(hash_after + "\n", encoding="utf-8")
        check("必过项② 副本 events 零 UPDATE", hash_before == hash_after,
              f"行 hash 前后一致 {hash_before[:19]}")
        ripple_n = int(await rp.fetchval("SELECT count(*) FROM ripple_edge"))
        check("涟漪边派生（必过项③数据源）", ripple_n >= 2, f"ripple_edge={ripple_n} 行（两手链）")

        # ---- P2 断网段 ---------------------------------------------------------------
        pre_outage = await read_sync_state(mp)
        ingest.terminate()
        ingest.wait(timeout=10)
        print(f"[drill] 摄入 API 已停 → 断网 {args.outage_s:.0f}s，主库持续写入…", flush=True)
        outage_inserted = 0
        outage_start = time.monotonic()
        base = dt.datetime.combine(DAY3, dt.time(12, 0), tzinfo=LOCAL_TZ)
        kernel_ok = True
        mid_state = None
        while time.monotonic() - outage_start < args.outage_s:
            try:
                await insert_event(mp, gen.next(base + dt.timedelta(seconds=30 * outage_inserted)))
                outage_inserted += 1
            except Exception:  # noqa: BLE001
                kernel_ok = False
                break
            if mid_state is None and time.monotonic() - outage_start > args.outage_s / 2:
                mid_state = await read_sync_state(mp)
            await asyncio.sleep(1.0 / args.outage_rate)
        final_state = await read_sync_state(mp)
        check("断网段 sync_state 位点停走",
              mid_state is not None
              and mid_state["last_acked_seq"] == pre_outage["last_acked_seq"]
              and final_state["last_acked_seq"] == pre_outage["last_acked_seq"],
              f"位点={pre_outage['last_acked_seq']} 全程未动")
        check("断网段主库持续增长且写路径无错", kernel_ok and outage_inserted > 0,
              f"断网期插入 {outage_inserted} 条")
        check("断网不影响内核（sync down 标志）", client.down,
              "client.down=True（退避重连中）")

        # ---- P3 恢复追平 --------------------------------------------------------------
        ingest = start_ingest(evidence)
        await wait_ingest()
        expected2 = expected_count + outage_inserted
        t0 = time.monotonic()
        catchup_s2 = await wait_caught_up(mp, rp, expected2, timeout=max(600, args.outage_s * 2))
        throughput = outage_inserted / (catchup_s2 / 60) if catchup_s2 > 0 else 0.0
        (evidence / "catchup_throughput.txt").write_text(
            f"断网积压={outage_inserted} 追平耗时={catchup_s2:.1f}s "
            f"吞吐={throughput:.0f} events/min（预算 ≥2000，04 §9.2）\n", encoding="utf-8")
        check("恢复追平 COUNT 一致",
              int(await rp.fetchval("SELECT count(*) FROM events")) == expected2,
              f"副本={expected2}（主库 {await mp.fetchval('SELECT count(*) FROM events')} "
              f"含 1 blocked）")
        sum_main = int(await mp.fetchval(
            "SELECT sum(seq) FROM events WHERE NOT (visibility='internal' AND payload ? 'text_raw')"))
        sum_replica = int(await rp.fetchval("SELECT sum(seq) FROM events"))
        check("恢复追平 SUM(seq) 一致", sum_main == sum_replica, f"{sum_main} == {sum_replica}")
        check("追平吞吐 ≥2000 events/min", throughput >= 2000 or outage_inserted < 2000,
              f"{throughput:.0f} events/min（R1 回填）")
        # 追平帧 500 条/批证据
        big = [line for line in frames_dump.read_text(encoding="utf-8").splitlines()
               if '"frame": "batch"' in line or '"frame":"batch"' in line]
        has500 = any(json.loads(line)["to"] - json.loads(line)["from"] + 1 == 500
                     for line in big)
        check("追平模式 500 条/批帧证据", has500 or outage_inserted < 500, "frames.dump 核查")

        # ---- P4 幂等：重放一批已 ack 事件 ----------------------------------------------
        replay_rows = await fetch_events_after(mp, 0, limit=50)
        async with httpx.AsyncClient() as c:
            body = json.loads(json.dumps(
                {"from_seq": replay_rows[0]["seq"], "events": replay_rows}, default=str,
                ensure_ascii=False))
            r1 = await c.post(f"http://127.0.0.1:{INGEST_PORT}/v1/events:batch", json=body,
                              headers={"Authorization": f"Bearer {TOKEN}"})
            r2 = await c.post(f"http://127.0.0.1:{INGEST_PORT}/v1/events:batch", json=body,
                              headers={"Authorization": f"Bearer {TOKEN}"})
        check("幂等：重放已 ack 批行数不变",
              r1.status_code == 200 and r2.status_code == 200
              and int(await rp.fetchval("SELECT count(*) FROM events")) == expected2,
              f"两次 200，行数仍 {expected2}")

        # ---- 必过项①抓包与副本物理不存在 -----------------------------------------------
        dump_text = frames_dump.read_text(encoding="utf-8")
        c_sentinel = dump_text.count(SENTINEL)
        c_raw = sum(1 for line in dump_text.splitlines()
                    if '"text_raw"' in line and '"frame"' in line)
        replica_hits = int(await rp.fetchval(
            "SELECT count(*) FROM events WHERE payload::text LIKE $1", f"%{SENTINEL}%"))
        check("必过项① 帧落盘无原文/text_raw", c_sentinel == 0 and c_raw == 0,
              f"sentinel={c_sentinel} text_raw 帧={c_raw}")
        check("必过项① 副本库物理不存在原文", replica_hits == 0, f"副本命中={replica_hits} 行")

        # ---- P5 digest 不一致 → 重灌 → 复检闭环 ------------------------------------------
        await rp.execute("DELETE FROM events WHERE seq = (SELECT min(seq) FROM events"
                         " WHERE (sim_time AT TIME ZONE 'Asia/Shanghai')::date = $1)", DAY1)
        rc1, out1 = run_digest_check(evidence, "1_mismatch")
        check("digest 检出删改不一致", rc1 == 1 and "FAIL" in out1,
              f"exit={rc1}（digest_check_1_mismatch.txt）")
        # 云端重灌编排：推送本机 digest → mismatch → 当日 DELETE + 重算任务
        body = await build_push_body(mp, DAY1, out_dir=snap_dir)
        async with httpx.AsyncClient() as c:
            r = await c.post(f"http://127.0.0.1:{INGEST_PORT}/v1/digest", json=body,
                             headers={"Authorization": f"Bearer {TOKEN}"})
            resp = r.json()
        check("重灌编排响应 mismatch 含该日",
              resp.get("status") == "mismatch" and DAY1.isoformat() in resp.get("mismatch_days", []),
              json.dumps(resp, ensure_ascii=False))
        # 本机重发该日（mode=reproject 豁免乱序，07 D4）+ 快照重推
        day1_events = []
        lo = await mp.fetchval(
            "SELECT min(seq) FROM events"
            " WHERE (sim_time AT TIME ZONE 'Asia/Shanghai')::date = $1", DAY1)
        hi = await mp.fetchval(
            "SELECT max(seq) FROM events"
            " WHERE (sim_time AT TIME ZONE 'Asia/Shanghai')::date = $1", DAY1)
        cursor = int(lo) - 1
        async with httpx.AsyncClient() as c:
            while True:
                rows = await mp.fetch(
                    """
                    SELECT seq FROM events WHERE seq > $1 AND seq <= $2
                      AND NOT (visibility='internal' AND payload ? 'text_raw')
                    ORDER BY seq LIMIT 500
                    """, cursor, hi)
                if not rows:
                    break
                batch = await fetch_events_after(mp, cursor, limit=500)
                batch = [r for r in batch if r["seq"] <= int(hi)]
                r = await c.post(
                    f"http://127.0.0.1:{INGEST_PORT}/v1/events:batch",
                    content=json.dumps({"from_seq": batch[0]["seq"], "events": batch,
                                        "mode": "reproject"},
                                       default=str, ensure_ascii=False),
                    headers={"Authorization": f"Bearer {TOKEN}",
                             "Content-Type": "application/json"})
                assert r.status_code == 200, r.text
                cursor = batch[-1]["seq"]
            # 记忆与快照重发
            mems = await mp.fetch(
                """
                SELECT id AS memory_id, agent_id, sim_time, kind, content_display, importance,
                       source_event_seq, is_witness FROM memories
                WHERE content_display IS NOT NULL
                  AND (sim_time AT TIME ZONE 'Asia/Shanghai')::date = $1 ORDER BY id
                """, DAY1)
            if mems:
                r = await c.post(
                    f"http://127.0.0.1:{INGEST_PORT}/v1/memories:batch",
                    content=json.dumps({"from_id": mems[0]["memory_id"],
                                        "memories": [dict(m) for m in mems]},
                                       default=str, ensure_ascii=False),
                    headers={"Authorization": f"Bearer {TOKEN}",
                             "Content-Type": "application/json"})
                assert r.status_code == 200, r.text
            import gzip as _gz
            with _gz.open(snap_dir / f"snapshot_{DAY1.isoformat()}.whitelist.json.gz", "rt",
                          encoding="utf-8") as f:
                snap_state = json.load(f)
            r = await c.post(
                f"http://127.0.0.1:{INGEST_PORT}/v1/snapshot",
                json={"sim_day": DAY1.isoformat(),
                      "digest": "sha256:" + sha256_hex(canonical(snap_state)),
                      "state": snap_state},
                headers={"Authorization": f"Bearer {TOKEN}"})
            assert r.status_code == 200, r.text
        rc2, out2 = run_digest_check(evidence, "2_recheck")
        check("重灌后复检一致", rc2 == 0 and "FAIL" not in out2,
              f"exit={rc2}（digest_check_2_recheck.txt）")

        # ---- 汇总 ----------------------------------------------------------------------
        (evidence / "drill_report.txt").write_text(
            "T-SYN-11 断网追平演练报告\n"
            f"断网时长={args.outage_s:.0f}s（WSIM_DRILL_OUTAGE_S 口径）\n"
            f"PASS {len(PASS)} / FAIL {len(FAIL)}\n"
            + "\n".join(f"PASS {p}" for p in PASS)
            + "\n".join(f"FAIL {f}" for f in FAIL) + "\n",
            encoding="utf-8")
        print(f"[drill] 完成：PASS {len(PASS)} FAIL {len(FAIL)}，证据 → {evidence}", flush=True)
        return 0 if not FAIL else 1
    finally:
        if client_task is not None:
            client_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client_task
        if ingest.poll() is None:
            ingest.terminate()
            ingest.wait(timeout=10)
        await mp.close()
        await rp.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
