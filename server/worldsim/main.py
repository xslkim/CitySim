"""WorldSim 内核入口：连接池、协程组、优雅停机（02 T-ADJ-02 主循环 owner；04 §2.1/§2.2 伪码）。

装配形态对齐 04 §2.2 伪码：

    db = await create_pool(WSIM_PG_DSN)
    clock = TimeEngine(db, load(WSIM_SPEED_TABLE))
    queue = AdjudicationQueue()
    gw = LLMGateway(db, load(WSIM_MODELS_CONFIG))
    async with asyncio.TaskGroup() as tg:
        tg.create_task(clock.run(on_tick=lambda t: queue.put(ClockTick(t))))
        tg.create_task(adjudication_loop(db, queue, gw))   # 唯一写协程，串行裁决

sync/audit/hygiene/rotation 协程挂接点留接口（M6/M3/T-MEM-03/T-LOD-03 接）。

运行模式：
- 持续模式（默认）：按变速表 paced 跑，段切换/batch 段/SIGHUP 热更全生效，直到 SIGINT/SIGTERM。
- 试跑模式（`--sim-hours N`）：unthrottled 尽快递 tick（仍 sim 网格锚定、逐 tick 落库串行），
  推进 N 模拟小时后优雅停机；不做段切换与 batch 段自动进入（开发试跑口径，README 同步）。

env：WSIM_PG_DSN（必填，根 .env 兜底加载）/ WSIM_SPEED_TABLE / WSIM_MODELS_CONFIG /
WSIM_WORLD_CONFIG / WSIM_REPLAY_MODE（replay 下本入口拒跑，重放归 T-ADJ-08 scripts/replay_check.py）。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

import asyncpg
import yaml

from . import logconf
from .adjudicator.pipeline import Pipeline, adjudication_loop
from .adjudicator.queue import AdjudicationQueue
from .llm_gateway import LLMGateway
from .llm_gateway.providers.mock import MockProvider
from .time_engine.clock import LOCAL_TZ, TimeEngine
from .time_engine.speed_table import SpeedTableReloader, load as load_speed_table

log = logging.getLogger(__name__)

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent


def _load_root_env() -> None:
    """仓库根 .env 兜底加载（仅补缺 WSIM_*，真实环境变量优先；uv run 不自动载 .env）。"""
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key.startswith("WSIM_") and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="worldsim.main", description="WorldSim 内核主循环（04 §2.2）")
    p.add_argument("--sim-hours", type=float, default=None,
                   help="试跑模式：推进 N 模拟小时后优雅停机（缺省 = 按变速表持续运行）")
    p.add_argument("--ratio", type=float, default=None,
                   help="覆盖压缩比（调试用；缺省读变速表当前段 ratio）")
    p.add_argument("--dsn", default=None, help="主库 DSN（缺省 env WSIM_PG_DSN）")
    p.add_argument("--speed-table", default=None, help="speed_table.yaml 路径（缺省 env WSIM_SPEED_TABLE → config/）")
    p.add_argument("--models-config", default=None, help="models.yaml 路径（缺省 env WSIM_MODELS_CONFIG → config/）")
    p.add_argument("--world-config", default=None, help="world.yaml 路径（缺省 env WSIM_WORLD_CONFIG → config/）")
    return p


def _load_yaml(path: str | None, env_name: str, default_name: str) -> dict[str, Any]:
    candidate = Path(path or os.environ.get(env_name, "") or SERVER_ROOT / "config" / default_name)
    with candidate.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


async def _run(args: argparse.Namespace) -> int:
    logconf.setup()
    _load_root_env()
    if os.environ.get("WSIM_REPLAY_MODE", "off") == "replay":
        log.error("WSIM_REPLAY_MODE=replay：live 主循环拒跑（重放归 T-ADJ-08 scripts/replay_check.py，04 §5.3）")
        return 2
    dsn = args.dsn or os.environ.get("WSIM_PG_DSN")
    if not dsn:
        log.error("缺主库 DSN：--dsn 或 env WSIM_PG_DSN（根 .env）")
        return 2

    speed_table_path = args.speed_table or os.environ.get("WSIM_SPEED_TABLE") or str(SERVER_ROOT / "config" / "speed_table.yaml")
    table = load_speed_table(speed_table_path)
    reloader = SpeedTableReloader(speed_table_path, table)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    try:
        trial = args.sim_hours is not None
        clock = await TimeEngine.start(pool, table, unthrottled=trial)
        if args.ratio is not None:
            await clock.set_ratio(args.ratio)
        target_sim = clock.now_sim() + dt.timedelta(hours=args.sim_hours) if trial else None

        queue = AdjudicationQueue()
        notify = asyncio.Event()
        queue.bind_notify_event(notify)
        drained = asyncio.Event() if trial else None

        models_cfg = _load_yaml(args.models_config, "WSIM_MODELS_CONFIG", "models.yaml")
        world_cfg = _load_yaml(args.world_config, "WSIM_WORLD_CONFIG", "world.yaml")
        gateway = LLMGateway(pool, models_cfg, providers={"mock": MockProvider()}, default_provider="mock")
        pipeline = Pipeline(pool, gateway, world_cfg)

        agent_ids = tuple(r["id"] for r in await pool.fetch("SELECT id FROM agents ORDER BY id"))
        log.info("世界装载：%d agents（%s）", len(agent_ids), "、".join(agent_ids))

        async def batch_summarize(agent_id: str, sim_hours: float) -> None:
            """M1 batch 摘要器：mock bgsummary → 摘要记忆（04 §4.3 背景层日摘要结构）。"""
            sim_now = clock.now_sim()
            seed = clock.tick_of(sim_now)
            result = await gateway.chat(
                "bgsummary",
                [
                    {"role": "system", "content": "总结该角色今天的模拟日。输出 JSON。"},
                    {"role": "user", "content": f'OBS_JSON={{"agent_id":"{agent_id}"}}\n总结最近 {sim_hours} 模拟小时。'},
                ],
                seed=seed, agent_id=agent_id, sim_time=sim_now,
            )
            diary = json.loads(result.text).get("diary", "")
            vec = await gateway.embed([diary], seed=seed, agent_id=agent_id, sim_time=sim_now)
            await pool.execute(
                """
                INSERT INTO memories (agent_id, sim_time, kind, content, content_display, importance, embedding)
                VALUES ($1, $2, 'summary', $3, $3, 4, $4::vector)
                """,
                agent_id, sim_now, diary, "[" + ",".join(repr(v) for v in vec.vectors[0]) + "]",
            )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # 非 Unix 平台兜底
                pass

        baseline_events = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        sim_start = clock.now_sim()
        log.info(
            "主循环启动：mode=%s sim_start=%s target=%s",
            "trial" if trial else "paced", sim_start.isoformat(),
            target_sim.isoformat() if target_sim else "∞",
        )

        async def _watch(t: asyncio.Task) -> None:
            try:
                await t
            finally:
                stop.set()  # 试跑到点/时钟退出 → 通知裁决协程收尾

        async with asyncio.TaskGroup() as tg:
            clock_task = tg.create_task(
                clock.run(
                    on_tick=lambda t: queue.put_clock_tick(t),
                    stop=stop,
                    target_sim=target_sim,
                    drain_event=drained,
                    on_enter_batch=lambda hours: queue.put_world_event(
                        clock.current_tick, {"enter_batch": {"sim_hours": hours}}
                    ),
                    reloader=reloader,
                )
            )
            tg.create_task(
                adjudication_loop(
                    pool, queue, gateway, clock, pipeline,
                    stop=stop, notify=notify, drained=drained,
                    agent_ids=agent_ids, batch_summarize=batch_summarize,
                )
            )
            tg.create_task(_watch(clock_task))
            # sync/audit/hygiene/rotation 协程挂接点（M6/M3/T-MEM-03/T-LOD-03 接，04 §2.2 伪码行）

        sim_end = clock.now_sim()
        new_events = await pool.fetchval("SELECT count(*) FROM events WHERE seq > $1", baseline_events)
        log.info(
            "主循环停机：sim %s → %s（%+.2f sim h），新增事件 %d 条",
            sim_start.isoformat(), sim_end.isoformat(),
            (sim_end - sim_start).total_seconds() / 3600, new_events,
        )
        return 0
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    """入口（`python -m worldsim.main`）；返回进程退出码。"""
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
