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
from .adjudicator.dialogue import DialogueEngine
from .adjudicator.grade import Grader
from .adjudicator.pipeline import Pipeline, adjudication_loop
from .adjudicator.queue import AdjudicationQueue
from .adjudicator.state_events import StateAggregator
from .adjudicator.validators import ActionValidator
from .invite.state_machine import InviteMachine
from .llm_gateway import LLMGateway
from .llm_gateway.providers.mock import MockProvider
from .memory.hygiene import MemoryHygiene
from .memory.reflect import Reflector
from .memory.retrieval import make_retrieve_hook
from .relations.cooldown import CooldownEngine
from .relations.goals import GoalEngine, load_goals
from .relations.needs import NeedsEngine, load_needs_config
from .relations.relations import RelationEngine, load_relations_config
from .relations.topics import TopicSystem, load_rules as load_topic_rules, load_topics
from .scheduler.lod import LodScheduler
from .scheduler.residence import ResidenceEngine
from .scheduler.rotation import EventDrivenLOD, StarRotation
from .time_engine.batch_hooks import register_batch_hook
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


async def drive_invite_due(invite_machine: Any, *, tick: int, sim_now: dt.datetime) -> None:
    """T-LLM-FIX-01（02 文档遗漏接线，02 偏差表 D34）：邀约提醒/爽约判定每 tick 驱动。

    T-30min 提醒与 T+15min 宽限后爽约结算的唯一主循环入口（01 §5.3 时序；rng_seed = 本 tick 规则骰子）。
    """
    await invite_machine.due_reminders(sim_now=sim_now, tick=tick, rng_seed=tick)
    await invite_machine.due_stood_ups(sim_now=sim_now, tick=tick, rng_seed=tick)


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
    p.add_argument("--llm", choices=["mock", "routed"], default="mock",
                   help="LLM 供给：mock（默认，M1 确定性口径）/ routed（真接入：models.yaml 路由+桶+降级链，T-LLM-12）")
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

        models_path = args.models_config or os.environ.get("WSIM_MODELS_CONFIG") or str(SERVER_ROOT / "config" / "models.yaml")
        models_cfg = _load_yaml(args.models_config, "WSIM_MODELS_CONFIG", "models.yaml")
        from .world_agent.config import load_world_config  # T-WA-01：全量校验，失败拒绝启动

        world_cfg = load_world_config(args.world_config or os.environ.get("WSIM_WORLD_CONFIG") or None)
        needs_cfg = load_needs_config(str(SERVER_ROOT / "config" / "needs.yaml"))
        relations_cfg = load_relations_config(str(SERVER_ROOT / "config" / "relations.yaml"))
        goals_cfg = load_goals(str(SERVER_ROOT / "config" / "goals.yaml"))
        if args.llm == "routed":
            # T-LLM-12 真接入：models.yaml 路由 + RPM 桶/退避 + 撞墙降级链 + SIGHUP 热更（04 §8/§12.4）
            from .llm_gateway.breaker import FailoverBreaker
            from .llm_gateway.providers.local_embed import LocalEmbedProvider
            from .llm_gateway.providers.zhipu import ZhipuProvider
            from .llm_gateway.router import ModelRouter

            router = ModelRouter(models_cfg, path=models_path)
            router.install_sighup()
            embed_cfg = models_cfg.get("providers", {}).get("local_embed", {})
            gateway = LLMGateway(
                pool, models_cfg,
                providers={
                    "zhipu": ZhipuProvider(model="glm-4.5-flash"),
                    "local_embed": LocalEmbedProvider(model=embed_cfg.get("model", "BAAI/bge-m3")),
                    "mock": MockProvider(),
                },
                router=router, breaker=FailoverBreaker(),
            )
            log.info("LLM 供给 = routed（models.yaml 路由真接入；embed=本地 bge-m3）")
            # 预热本地 embedding 模型（bge-m3 首次加载 ~30s CPU，避免启动段阻塞事件循环
            # 抬高 chat 端到端延迟触发撞墙条件③误判，03 §6 D42/D45）
            await gateway.embed(["预热"], seed=0)
            log.info("本地 embedding 模型预热完成")
        else:
            gateway = LLMGateway(pool, models_cfg, providers={"mock": MockProvider()}, default_provider="mock")
        # 波次 2a 接线：聚合器（04 §6.5）/ 检索（T-MEM-01）/ 反思（T-MEM-02）/ 治理（T-MEM-03）/
        # 需求衰减（T-REL-01）/ 关系（T-REL-02）/ 目标（T-REL-03）/ 冷却（T-REL-04）
        # T-DIR-04：grade 阈值从 world.yaml director.grade 段注入（01 §6.4 部署镜像），
        # Grader/聚合器/时钟共用同一份阈值（全事件 ui.grade 覆盖口径）
        grader = Grader(pool, thresholds=(world_cfg.get("director") or {}).get("grade"))
        clock.set_grader(grader)
        agg = StateAggregator(pool, grader=grader)
        reflector = Reflector(
            pool, gateway,
            threshold=int(models_cfg.get("thresholds", {}).get("reflection", {}).get("importance_acc", 20)),
            tick_of=clock.tick_of, grader=grader,
        )
        needs_engine = NeedsEngine(needs_cfg)
        relation_engine = RelationEngine(pool, relations_cfg)
        cooldown_engine = CooldownEngine(pool, relations_cfg)
        goal_engine = GoalEngine(
            pool, goals_cfg, cooldown=cooldown_engine, relations=relation_engine,
            reflect_fn=lambda aid, content, importance: reflector.write_generated_reflection(
                aid, content, importance=importance,
            ),
        )
        # T-LOD-02：事件驱动即时升格（被交互/被邀约/进镜头当 tick 升格；secondary ≤16 + LRU 挤出）
        lod_events = EventDrivenLOD(pool, models_cfg.get("thresholds", {}).get("lod", {}), grader=grader)

        async def after_settle(decision: Any, seqs: list[int]) -> None:
            """step5 收尾挂点：新落库事件触发路径二升格判定（当 tick 生效，04 §4.2）。"""
            for seq in seqs:
                row = await pool.fetchrow(
                    "SELECT seq, tick, sim_time, type, source, visibility, payload, ui FROM events WHERE seq=$1", seq,
                )
                if row is None:
                    continue
                await lod_events.on_event(tick=int(row["tick"]), sim_now=row["sim_time"], event=dict(row))

        # T-ADJ-03：19 动作校验器 + step5 结算总线（含 debts 写入/核销；通用规则表五条）
        residence = ResidenceEngine(pool, world_cfg)
        # grader 已在上方以 director.grade 阈值段构造（T-DIR-04）；全库只此一处写 ui.grade 初值
        invite_machine = InviteMachine(
            pool, gateway, relations_cfg,
            agg=agg, cooldown=cooldown_engine, relations=relation_engine, tick_of=clock.tick_of,
            grader=grader,
        )
        # T-ADJ-04：对话整段生成引擎（话题注入 T-REL-06 + 降速读取点 ThrottleState seam）
        topic_system = TopicSystem(
            pool, load_topics(str(SERVER_ROOT / "config" / "topics.yaml")),
            load_topic_rules(str(SERVER_ROOT / "config" / "topics.yaml")),
        )
        dialogue_engine = DialogueEngine(
            pool, gateway, topics=topic_system, relations=relation_engine, cooldown=cooldown_engine,
            needs_engine=needs_engine, agg=agg,
            default_daily_cap=int(models_cfg.get("thresholds", {}).get("dialogue", {}).get("daily_cap", 42)),
            grader=grader,
        )
        action_validator = ActionValidator(
            pool, world=world_cfg, needs_engine=needs_engine, cooldown=cooldown_engine,
            relations=relation_engine, relations_cfg=relations_cfg, agg=agg, gateway=gateway,
            invite=invite_machine, residence=residence,
            wakeup=lambda t, aid: queue.put_wakeup(t, aid, reason="interaction"),
            dialogue_settle=dialogue_engine.settle_chat,
            grader=grader,
        )
        pipeline = Pipeline(
            pool, gateway, world_cfg,
            retrieve=make_retrieve_hook(pool, gateway, models_cfg),
            reflect=reflector.hook,
            after_settle=after_settle,
            validate=action_validator.check,
            settler=lambda obs, decision, tick, sim_now, seed: action_validator.settle(
                obs, decision, tick=tick, sim_now=sim_now, rng_seed=seed,
            ),
        )

        # ---- M3 接线（04 文档 T-WA/T-DIR）：日历引擎 + 世界 Agent 全作业 + 编剧导演 --------------
        from .llm_gateway.prompts import PromptRegistry
        from .world_agent.calendar import (
            CalendarEngine, register_career_jobs, register_evening_jobs, register_layoff_rumor_job,
        )
        from .world_agent.director.arcs import ArcEngine, load_arcs
        from .world_agent.director.intervene import InterventionFramework
        from .world_agent.director.review import ReviewEngine
        from .world_agent.disturb import register_disturb_jobs
        from .world_agent.economy import register_economy_jobs, register_overdue_jobs, register_stock_jobs

        calendar = CalendarEngine(pool, world_cfg, clock, agg=agg, grader=grader, gateway=gateway)
        calendar.register_core_jobs()          # T-WA-02（8:00 打卡内部结算）
        register_stock_jobs(calendar)          # T-WA-05
        register_layoff_rumor_job(calendar)    # T-WA-06
        register_career_jobs(calendar)         # T-WA-07
        register_evening_jobs(calendar)        # T-WA-08
        register_disturb_jobs(calendar)        # T-WA-09
        arcs_cfg = load_arcs()
        director_fw = InterventionFramework(
            pool, calendar, clock, arcs_cfg=arcs_cfg,
            intervention_rate_cap=float(models_cfg.get("thresholds", {}).get("intervention_rate_cap", 0.15)),
            event_lod=lod_events, gateway=gateway,
        )
        arc_engine = ArcEngine(pool, arcs_cfg, clock, intervene=director_fw.intervene)
        review_engine = ReviewEngine(
            pool, gateway, clock, revise_cfg=world_cfg.get("director", {}).get("revise", {}),
            registry=PromptRegistry.load(),
            call_factor=lambda: 1.0,  # 降速读取点 seam：ThrottleState.director_call_factor 接线归 08 T-OPS-02
            grader=grader,
        )

        async def _on_rent_crisis(cal, fire_time, agent_id, owed) -> int:
            """退租危机 L1（01 §1.5 欠租链末级 → 01 §6.3 L1 计干预率；动作映射=company_crisis，D-31）。"""
            return await director_fw.intervene(
                "L1", "company_crisis", reason=f"退租危机：{agent_id} 连续 2 周期欠租（挂账 {owed} 分）",
                params={"scope": "floor", "severity": "high"}, sim_now=fire_time)

        register_economy_jobs(calendar)        # T-WA-03
        register_overdue_jobs(calendar, relations_cfg=relations_cfg, on_crisis=_on_rent_crisis)  # T-WA-04

        async def director_daily(cal, fire_time) -> None:
            """编剧日界作业（T-DIR-01/05）：K3 复核昨日 → 无 A 级自动启弧线检查（01 §6.4）。"""
            day = fire_time.date() - dt.timedelta(days=1)
            await review_engine.run_daily_review(day)
            await arc_engine.daily_check()

        calendar.register_job("director.daily", "00:05", lambda d: True, director_daily)
        log.info("M3 接线完成：日历作业 %d 项 + 弧线 %d 模板 + 干预框架 + K3 复核",
                 len(calendar.jobs), len(arcs_cfg.get("arcs", [])))

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

        # ---- 波次 2a：tick 收尾驱动（需求衰减/聚合 flush/每日兜底反思） ------------------
        last_decay: dict[str, dt.datetime] = {}
        trial_snap = {"last_day": sim_start.date()}  # T-WEB-20：试跑模式日界快照（见下 D25 注）

        async def after_tick(tick: int, sim_now: dt.datetime) -> None:
            # T-LOD-04：驻留规则每 tick 评估（不占认知循环；移位落 agent.move trigger='system'）
            await residence.evaluate(tick=tick, sim_now=sim_now, rng_seed=tick)
            # T-WA-02：日历引擎连续段驱动（日界/排程项结算；产 needs_delta 先入 agg 缓冲）
            await calendar.tick(sim_now)
            # T-WEB-20（05 文档 D25）：试跑模式不进 batch 段（02 文档口径），快照在试跑模式
            # 改由日界翻转时点直调同一 dump_snapshot 实现（batch 段挂载点语义不变；paced 模式仍走 batch 钩子）
            if trial and sim_now.date() != trial_snap["last_day"]:
                from .snapshot.dump import dump_snapshot
                ended_day = trial_snap["last_day"]
                trial_snap["last_day"] = sim_now.date()
                await dump_snapshot(
                    pool, sim_day=ended_day, out_dir=REPO_ROOT / "var" / "snapshot",
                    tick=tick, sim_now=sim_now,
                    compression_ratio=clock.ratio, schedule=needs_cfg.get("schedule"),
                )
            # T-REL-01：全员六需求被动衰减（只读 sim_time；cause = 本裁决点前最后一事件 seq，工程口径 D22）
            cause = str(await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events"))
            for aid in agent_ids:
                last = last_decay.get(aid, sim_start)
                if sim_now > last:
                    await needs_engine.settle_decay(agg, agent_id=aid, from_sim=last, to_sim=sim_now, cause=cause)
                    last_decay[aid] = sim_now
            # 04 §6.5：本 tick 聚合状态事件合并落库（≤2 条）
            await agg.flush(tick=tick, sim_now=sim_now, trigger="system", rng_seed=tick)
            # T-MEM-02：每模拟日 23:00 批量兜底反思（按 agent 日幂等）
            await reflector.run_due_daily_fallbacks(agent_ids, sim_now, rng_seed=tick)
            # T-LLM-FIX-01（02 遗漏接线，D34）：邀约提醒/爽约判定每 tick 驱动
            await drive_invite_due(invite_machine, tick=tick, sim_now=sim_now)
            # T-DIR-01：弧线状态机每 tick 评估（谓词只读 DB；钩子经 T-DIR-03 唯一入口）
            await arc_engine.tick(sim_now)

        # ---- 波次 2a：batch 段钩子（唯一挂载点，02 T-TIME-03） --------------------------
        hygiene = MemoryHygiene(pool, gateway)
        hygiene.register_batch_hook()  # T-MEM-03 摘要合并（每模拟日）

        async def kernel_calendar_hook(ctx: Any) -> None:
            """日界/周界作业：目标周刷新+关系周回归（周一界）、挫败值日恢复（日界）。"""
            now = ctx.clock.now_sim()
            before = now - dt.timedelta(hours=ctx.sim_hours)
            cause = str(await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events"))
            if now.date() != before.date():
                await goal_engine.daily_recovery()
                # T-LOD-02 回落：事件驱动升入 secondary 连续 2 模拟日零新交互 → cooldown 回落（日界扫描）
                await lod_events.demote_inactive(tick=ctx.clock.current_tick, sim_now=now)
            if goal_engine.week_of(now) != goal_engine.week_of(before):
                await goal_engine.refresh_weekly(agg, sim_now=now, cause=cause)
                await relation_engine.weekly_regression(agg, cause=cause)
                await agg.flush(tick=ctx.clock.current_tick, sim_now=now, trigger="system",
                                rng_seed=ctx.clock.current_tick)

        register_batch_hook("kernel.calendar", kernel_calendar_hook)

        # T-WA-02：世界 Agent 日历跨日结算单入口（04 §3.3 batch 段唯一挂载点）
        register_batch_hook("world_agent.calendar", calendar.run_batch_calendar)

        # T-LOD-03：基尼驱动明星轮换（路径一，每模拟日 1 次，迟滞）；挂 batch 段回调注册表（唯一挂载点）
        star_rotation = StarRotation(
            pool, gateway,
            thresholds_rotation=models_cfg.get("thresholds", {}).get("rotation", {}),
            thresholds_lod=models_cfg.get("thresholds", {}).get("lod", {}),
            reflector=reflector, event_lod=lod_events, grader=grader,
        )

        async def kernel_rotation_hook(ctx: Any) -> None:
            await star_rotation.rotation_tick(tick=ctx.clock.current_tick, sim_now=ctx.clock.now_sim())

        register_batch_hook("kernel.rotation", kernel_rotation_hook)

        # T-ADJ-09：每模拟日 world_state 全量快照落盘（模拟日界 00:00，05 §3.6；挂 batch 段注册表唯一挂载点）
        from .snapshot.dump import dump_snapshot

        async def kernel_snapshot_hook(ctx: Any) -> None:
            now = ctx.clock.now_sim()  # batch 段补进后时点；快照对刚结束的模拟日取数（05 §3.6 日界口径）
            sim_day = (now - dt.timedelta(minutes=1)).date()
            await dump_snapshot(
                pool, sim_day=sim_day, out_dir=REPO_ROOT / "var" / "snapshot",
                tick=ctx.clock.current_tick, sim_now=now,
                compression_ratio=ctx.clock.ratio, schedule=needs_cfg.get("schedule"),
            )

        register_batch_hook("kernel.snapshot", kernel_snapshot_hook)

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
                    after_tick=after_tick,
                    director_preempt=lambda: arc_engine.tick(),  # T-DIR-01：batch 段弧线插队（04 §3.3）
                    lod=LodScheduler(pool),  # T-LOD-01：next_due 时钟兜底排程（三层分发接口就位）
                )
            )
            tg.create_task(hygiene.hygiene_loop(clock=clock, stop=stop))  # T-MEM-03 日界归档（04 §2.2）
            tg.create_task(_watch(clock_task))
            # sync/audit/rotation 协程挂接点（M6/M3/T-LOD-03 接，04 §2.2 伪码行）

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
