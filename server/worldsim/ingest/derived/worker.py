"""derived worker（07 T-SYN-07/08；05 §4.1 架构）：SKIP LOCKED 消费 derived_task，微批落派生表。

- 消费：`SELECT … FOR UPDATE SKIP LOCKED`（投影类单次 ≤500 任务，05 §4.2 追平模式行）；
  失败指数退避 1m/5m/30m，3 次置 `failed` 告警（05 §4.3）。
- 任务映射：project_relation_change / compute_ripple_edge / materialize_event_grade（投影类，
  dedupe `<prefix>:e<seq>`）；refresh_relation_daily / refresh_health_daily（聚合类，T-SYN-08，
  按 sim_day 全量重算）。
- 每日兜底（05 §4.2 各行"每日兜底"）：`enqueue_daily_sweep` 对近 2 模拟日重排投影类全量重算 +
  聚合类重算任务；调度由 run_forever 内 05:00 定时器承担。
- 起停段编排归 08 T-OPS-05 `start_local.sh` 增补段（R1 §A.9）；本模块只交付 worker 本体。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Any

from . import compute_ripple_edge, materialize_event_grade, project_relation_change

log = logging.getLogger(__name__)

PULL_LIMIT = 500                    # 投影类单次拉取上限（05 §4.2）
BACKOFF_S = (60.0, 300.0, 1800.0)   # 1m/5m/30m（05 §4.3）
MAX_ATTEMPTS = 3

_EVENT_TASK = re.compile(r"^(?:sweep:)?(relchg|ripple|grade):e(\d+)$")


async def claim_tasks(conn: Any, *, limit: int = PULL_LIMIT) -> list[dict[str, Any]]:
    """SKIP LOCKED 拉取到期 pending 任务并置 running（调用方须持事务）。"""
    rows = await conn.fetch(
        """
        UPDATE derived_task SET status='running', attempts=attempts+1, run_at=now()
        WHERE id IN (
          SELECT id FROM derived_task
          WHERE status='pending' AND (run_at IS NULL OR run_at <= now())
          ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED
        )
        RETURNING id, task_type, dedupe_key, sim_day, attempts
        """, limit)
    return [dict(r) for r in rows]


async def run_task(conn: Any, task: dict[str, Any]) -> None:
    """按 task_type 分发（聚合类 refresh_* 归 T-SYN-08 模块，import 未交付即报错置 failed）。"""
    tt, key = task["task_type"], task["dedupe_key"]
    m = _EVENT_TASK.match(key)
    if tt == "project_relation_change" and m:
        await project_relation_change.project(conn, int(m.group(2)))
    elif tt == "compute_ripple_edge" and m:
        await compute_ripple_edge.compute(conn, int(m.group(2)))
    elif tt == "materialize_event_grade" and m:
        await materialize_event_grade.materialize(conn, int(m.group(2)))
    elif tt == "refresh_relation_daily":
        from . import refresh_relation_daily
        await refresh_relation_daily.refresh(conn, task["sim_day"])
    elif tt == "refresh_health_daily":
        from . import refresh_health_daily
        await refresh_health_daily.refresh(conn, task["sim_day"])
    else:
        raise ValueError(f"未知任务 {tt}/{key}")


async def run_once(pool: Any, *, limit: int = PULL_LIMIT) -> int:
    """消费一轮：返回处理任务数。失败退避重排，3 次置 failed + 告警。"""
    async with pool.acquire() as conn, conn.transaction():
        tasks = await claim_tasks(conn, limit=limit)
    done = 0
    for task in tasks:
        try:
            async with pool.acquire() as conn, conn.transaction():
                await run_task(conn, task)
                await conn.execute(
                    "UPDATE derived_task SET status='done', done_at=now() WHERE id=$1", task["id"])
            done += 1
        except Exception as e:  # noqa: BLE001 - 失败任务隔离，不阻塞队列
            delay = BACKOFF_S[min(task["attempts"], MAX_ATTEMPTS) - 1]
            failed = task["attempts"] >= MAX_ATTEMPTS
            log.warning("derived_task %s(%s) 第%d次失败：%s → %s",
                        task["task_type"], task["dedupe_key"], task["attempts"], e,
                        "置 failed 告警" if failed else f"{delay:.0f}s 后重试")
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    """
                    UPDATE derived_task SET status=$2, last_error=$3,
                      run_at = CASE WHEN $2='pending' THEN now() + make_interval(secs => $4)
                                    ELSE run_at END
                    WHERE id=$1
                    """,
                    task["id"], "failed" if failed else "pending", str(e)[:2000], delay)
            if failed:
                try:
                    from ...audit.alerts import alert
                    alert("ERROR", f"derived_task.{task['task_type']}",
                          f"任务 3 次失败置 failed：{task['dedupe_key']}", {"error": str(e)[:500]})
                except Exception:  # noqa: BLE001 - 告警通道缺席不阻塞 worker
                    pass
    return done


async def enqueue_daily_sweep(pool: Any, *, today: dt.date, days: int = 2) -> int:
    """每日兜底（05 §4.2/05:00 定时器调用）：近 2 模拟日投影类全量重算任务 + 聚合类重算任务。

    投影类重算以 dedupe 重排同键任务实现（ON CONFLICT DO NOTHING 幂等：已 done 的同键任务
    由 sweep 专用键 `sweep:<prefix>:e<seq>` 独立成行，处理器同源）。
    """
    n = 0
    async with pool.acquire() as conn, conn.transaction():
        for d in range(days):
            day = today - dt.timedelta(days=d)
            for tt, prefix, src in (
                ("project_relation_change", "relchg", "relation.changed"),
                ("compute_ripple_edge", "ripple", "dialogue.gossip"),
                ("materialize_event_grade", "grade", "director.grade_revise"),
            ):
                rows = await conn.fetch(
                    """
                    SELECT seq FROM events
                    WHERE type=$2 AND (sim_time AT TIME ZONE 'Asia/Shanghai')::date=$1
                    """, day, src)
                for r in rows:
                    await conn.execute(
                        """
                        INSERT INTO derived_task (task_type, dedupe_key, sim_day)
                        VALUES ($1,$2,$3) ON CONFLICT (dedupe_key) DO NOTHING
                        """, tt, f"sweep:{prefix}:e{int(r['seq'])}", day)
                    n += 1
            # ui.grade 基线兜底（07 D6）
            rows = await conn.fetch(
                """
                SELECT seq FROM events
                WHERE ui ? 'grade' AND (sim_time AT TIME ZONE 'Asia/Shanghai')::date=$1
                """, day)
            for r in rows:
                await conn.execute(
                    """
                    INSERT INTO derived_task (task_type, dedupe_key, sim_day)
                    VALUES ('materialize_event_grade',$1,$2) ON CONFLICT (dedupe_key) DO NOTHING
                    """, f"sweep:grade:e{int(r['seq'])}", day)
                n += 1
            for tt, prefix in (("refresh_relation_daily", "relation_daily"),
                               ("refresh_health_daily", "health_daily")):
                await conn.execute(
                    """
                    INSERT INTO derived_task (task_type, dedupe_key, sim_day)
                    VALUES ($1,$2,$3) ON CONFLICT (dedupe_key) DO NOTHING
                    """, tt, f"sweep:{prefix}:{day.isoformat()}", day)
                n += 1
    return n


async def run_forever(pool: Any, *, poll_s: float = 1.0) -> None:  # pragma: no cover - 常驻循环
    """常驻消费循环 + 每日 05:00 兜底调度（05 §4.2）。"""
    last_sweep: dt.date | None = None
    while True:
        try:
            await run_once(pool)
            now = dt.datetime.now()
            if now.hour >= 5 and last_sweep != now.date() and now.minute >= 0 and now.hour == 5:
                await enqueue_daily_sweep(pool, today=now.date())
                last_sweep = now.date()
        except Exception:  # noqa: BLE001
            log.exception("derived worker 轮询异常")
        await asyncio.sleep(poll_s)


def main() -> None:  # pragma: no cover - 进程入口（起停编排归 08 T-OPS-05 start_local.sh 增补段）
    """`uv run python -m worldsim.ingest.derived.worker`（DSN 读 WSIM_REPLICA_PG_DSN）。"""
    import os

    import asyncpg

    from ... import logconf
    from .. import replica_dsn  # ingest 包 DSN 解析（07 D2）

    async def _run() -> None:
        logconf.setup()
        pool = await asyncpg.create_pool(replica_dsn(), min_size=1, max_size=4)
        try:
            log.info("derived worker 启动（DSN=WSIM_REPLICA_PG_DSN）")
            await run_forever(pool)
        finally:
            await pool.close()

    if not os.environ.get("WSIM_REPLICA_PG_DSN"):
        raise SystemExit("缺 WSIM_REPLICA_PG_DSN（07 D2）")
    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()

