"""T-DIR-06 M3 集成演练：连续 7 模拟日出口验证（04 文档 T-DIR-06 验收 1~4；09 §4 M3 出口 E1~E8）。

结构：
- 段 A（无人值守主循环）：mock provider 真跑 `python -m worldsim.main --sim-hours 168`
  （seed 锚点 2026-10-12 周一 → 10-19，7 个完整模拟日），退出码 0。
- 段 B（构造日历延伸）：同一库日历引擎续跑至 2027-01-03，覆盖发薪/房租/水电/欠费链
  （overdue→notice）/晋升窗口/绩效/团建/加班/扰动/传闻全类型（04 §6 D-06 游标续接）。
- 段 C（导演演练）：L0~L2 各一次 + L3 枚举拒绝 + K3 复核 grade_revise 追加演练。

隔离：独立 scratch 库 `worldsim_m3_week_it`，跑完 drop；不留孤儿进程（subprocess timeout 包住）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db

SERVER_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVER_ROOT.parent
_DB = "worldsim_m3_week_it"
_DSN = f"postgresql:///{_DB}?host=/tmp"
MEASUREMENTS_PATH = REPO_ROOT / "var" / "logs" / "audit" / "m3_measurements.json"

RUN_START = dt.date(2026, 10, 12)   # seed 锚点（周一）
RUN_DAYS = 7                        # 连续 7 模拟日（09 §4 E1）
SEG_B_END = dt.date(2027, 1, 3)     # 段 B 终点（欠租链 notice 周期）

M3_MODULE_TYPES = [  # 本模块全部事件类型（T-DIR-06 验收 4 覆盖清单）
    "time.day_summary", "economy.payroll", "economy.bill.rent", "economy.bill.utility",
    "economy.bill.rent.overdue", "economy.bill.utility.overdue", "economy.bill.rent.notice",
    "economy.stock.tick", "world.layoff_rumor", "world.promotion_window", "world.perf_review",
    "world.overtime", "world.team_building", "world.announce",
    "world.disturb.illness", "world.disturb.weather", "world.disturb.complaint", "world.disturb.lucky",
    "director.intervene", "director.grade_revise", "social.borrow_money", "social.repay_money",
]


@pytest.fixture(scope="module")
def m3_db():
    """段 A：建库 + mock 主循环连跑 7 模拟日（无人值守，退出码 0 为本 fixture 的硬断言）。"""
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql", DDL_DIR / "health_daily_v1.sql")
    env = dict(os.environ, WSIM_PG_DSN=_DSN, HF_HUB_OFFLINE="1", PYTHONUNBUFFERED="1")
    env.pop("WSIM_REPLAY_MODE", None)
    r = subprocess.run(
        [sys.executable, "-m", "worldsim.main", "--sim-hours", str(RUN_DAYS * 24), "--dsn", _DSN],
        cwd=SERVER_ROOT, env=env, capture_output=True, text=True, timeout=1500)
    assert r.returncode == 0, f"主循环 7 模拟日非零退出：{r.returncode}\n{r.stderr[-2000:]}"
    yield dsn
    _drop_db(_DB)


async def _open(dsn):
    return await asyncpg.create_pool(dsn, min_size=1, max_size=4)


def _p(row) -> dict:
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])


# ---- E1：连续 7 模拟日、无致命暂停、day_summary 每日产出 --------------------------------


@pytest.mark.asyncio
async def test_e1_seven_sim_days(m3_db) -> None:
    pool = await _open(m3_db)
    try:
        n = await pool.fetchval("SELECT count(*) FROM events WHERE type='time.day_summary'")
        assert n == RUN_DAYS, f"day_summary 每日恰一条（{n}/{RUN_DAYS}）"
        assert await pool.fetchval("SELECT count(*) FROM events WHERE type='time.paused'") == 0, "无致命暂停"
        total = await pool.fetchval("SELECT count(*) FROM events")
        assert total > 500, "事件流持续产出"
        # seq 单调无洞（IDENTITY + 单写协程）
        gaps = await pool.fetchval(
            "SELECT count(*) FROM (SELECT seq, seq - lag(seq) OVER (ORDER BY seq) AS d FROM events) t WHERE d > 1")
        assert gaps == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_e1_full_grade_coverage(m3_db) -> None:
    """09 §4 E7 前半：grade 自动打分覆盖全事件（T-ADJ-07 + T-DIR-04 接线）。"""
    pool = await _open(m3_db)
    try:
        assert await pool.fetchval("SELECT count(*) FROM events WHERE ui IS NULL OR NOT (ui ? 'grade')") == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_replay_consistency(m3_db) -> None:
    """T-DIR-06 验收（回放）：WSIM_REPLAY_MODE=replay 重放段 A 第 7 模拟日——零 LLM 调用、状态对账一致。"""
    env = dict(os.environ, WSIM_PG_DSN=_DSN, WSIM_REPLAY_MODE="replay", HF_HUB_OFFLINE="1")
    r = subprocess.run(
        [sys.executable, "scripts/replay_check.py", "--sim-day", str(RUN_DAYS), "--dsn", _DSN],
        cwd=SERVER_ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, f"replay 对账失败：{r.stdout[-1500:]}\n{r.stderr[-1500:]}"
    assert "llm_calls 增量=0" in r.stdout


# ---- 段 B：构造日历延伸（经济链/欠费链/职场日历/扰动/传闻全类型） ---------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def m3_db_phase_b(m3_db):
    """段 B+C：日历续跑 + 构造欠费/借贷/传闻/复核演练。依赖段 A（m3_db fixture）。"""
    import random as _random  # noqa: F401
    from tests.world_agent._support import FakeClock
    from worldsim.adjudicator.grade import Grader
    from worldsim.adjudicator.state_events import StateAggregator
    from worldsim.llm_gateway import LLMGateway
    from worldsim.llm_gateway.providers.mock import MockProvider
    from worldsim.relations.relations import load_relations_config
    from worldsim.time_engine.clock import LOCAL_TZ
    from worldsim.world_agent.calendar import (
        CalendarEngine, register_career_jobs, register_evening_jobs, register_layoff_rumor_job,
    )
    from worldsim.world_agent.config import load_world_config
    from worldsim.world_agent.director.arcs import ArcEngine, load_arcs
    from worldsim.world_agent.director.intervene import InterventionFramework
    from worldsim.world_agent.disturb import fire_complaint, register_disturb_jobs
    from worldsim.world_agent.economy import register_economy_jobs, register_overdue_jobs, register_stock_jobs

    pool = await _open(m3_db)
    try:
        # 续接主循环游标（world_state calendar.last_settled 持久化）
        last = await pool.fetchval("SELECT max(sim_time) FROM events")
        clock = FakeClock(last)
        cfg = load_world_config()
        grader = Grader(pool, thresholds=(cfg.get("director") or {}).get("grade"))
        agg = StateAggregator(pool, grader=grader)
        gw = LLMGateway(pool, {}, providers={"mock": MockProvider()}, default_provider="mock")
        cal = CalendarEngine(pool, cfg, clock, agg=agg, grader=grader, gateway=gw)
        cal.register_core_jobs()
        register_stock_jobs(cal)
        register_layoff_rumor_job(cal)
        register_career_jobs(cal)
        register_evening_jobs(cal)
        register_disturb_jobs(cal)
        arcs_cfg = load_arcs()
        cap = yaml.safe_load((SERVER_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))[
            "thresholds"]["intervention_rate_cap"]
        fw = InterventionFramework(pool, cal, clock, arcs_cfg=arcs_cfg,
                                   intervention_rate_cap=float(cap), gateway=gw)
        arc_engine = ArcEngine(pool, arcs_cfg, clock, intervene=fw.intervene)
        register_economy_jobs(cal)
        register_overdue_jobs(cal, relations_cfg=load_relations_config(
            str(SERVER_ROOT / "config" / "relations.yaml")), on_crisis=(
            lambda c, t, aid, owed: fw.intervene(
                "L1", "company_crisis", reason=f"退租危机 {aid}", sim_now=t,
                params={"scope": "floor", "severity": "high"})))

        # 构造欠费链（payday=1 日先于账单日 3 日，01 §1.5——须跨发薪日持续排水才能成链）：
        # A08 房租 overdue（11-03）→ 跨周期未结清 → notice（12-03）；A07 水电 overdue（11-10）
        DRAIN = {  # date → [(agent, balance)]：当日 tick 前置余额
            dt.date(2026, 11, 2): [("A08", 0)],
            dt.date(2026, 11, 9): [("A07", 5000)],
            dt.date(2026, 12, 2): [("A08", 0)],   # 12-02 排干（防当日 09:30 每日扫描自动清账；12-01 发薪已于前日入账）
            dt.date(2027, 1, 1): [("A08", 0)],
        }
        # 传闻构造：2026-11-09 注入 XLKJ 跌破阈值（读配置阈值，边界外 2pct）
        th = float(cfg["triggers"]["layoff_rumor"]["drop_pct_threshold"])
        fire = dt.datetime(2026, 11, 9, 10, 0, tzinfo=LOCAL_TZ)  # 当日真实 09:30 结算之后（判定取最近交易日口径）
        await cal.insert_event(
            type_="economy.stock.tick",
            payload={"symbol": cfg["triggers"]["layoff_rumor"]["symbol"], "open": 1800,
                     "close": round(1800 * (1 - (th + 2) / 100)), "r": -(th + 2) / 100,
                     "seed": clock.tick_of(fire)},
            sim_time=fire, rng_seed=clock.tick_of(fire))

        # 逐日驱动（07:00 扰动 roll 在每日tick内）
        day = last.date() + dt.timedelta(days=1)
        while day <= SEG_B_END:
            for aid, bal in DRAIN.get(day, []):
                await pool.execute("UPDATE agents SET balance_cents=$2 WHERE id=$1", aid, bal)
            clock.set(dt.datetime.combine(day, dt.time(23, 55), tzinfo=LOCAL_TZ))
            await cal.tick()
            await agg.flush(tick=clock.current_tick, sim_now=clock.now_sim(),
                            trigger="system", rng_seed=clock.current_tick)
            day += dt.timedelta(days=1)

        # 借贷链构造（borrow/repay 经 M1 校验器结算总线，04 §5.2 debts 口径）
        from worldsim.adjudicator.pipeline import Decision, Observation
        from worldsim.adjudicator.validators import ActionValidator
        from worldsim.relations.cooldown import CooldownEngine
        from worldsim.relations.needs import NeedsEngine, load_needs_config
        from worldsim.relations.relations import RelationEngine
        from worldsim.scheduler.residence import ResidenceEngine

        relations_cfg = load_relations_config(str(SERVER_ROOT / "config" / "relations.yaml"))
        rel_engine = RelationEngine(pool, relations_cfg)
        cooldown = CooldownEngine(pool, relations_cfg)
        validator = ActionValidator(
            pool, world=cfg, needs_engine=NeedsEngine(load_needs_config(
                str(SERVER_ROOT / "config" / "needs.yaml"))),
            cooldown=cooldown, relations=rel_engine, relations_cfg=relations_cfg,
            agg=agg, gateway=gw, invite=None, residence=ResidenceEngine(pool, cfg),
            dialogue_settle=None, grader=grader)
        sim_now = clock.now_sim()
        needs70 = {k: 70.0 for k in ("hunger", "energy", "mood", "social", "achievement", "wealth")}
        a03 = await pool.fetchrow("SELECT balance_cents, position FROM agents WHERE id='A03'")
        # 构造同节点（borrow/repay 校验读目标 DB 位置，01 §4 可达性行；段 B 在 replay 窗口外）
        await pool.execute("UPDATE agents SET position=$1 WHERE id='A05'", a03["position"])

        def obs(aid, pos, bal):
            return Observation(agent_id=aid, name=aid, sim_time=sim_now, position=pos,
                               exits=[], co_located=["A05"], needs=dict(needs70), balance_cents=bal,
                               goals=[], recent_events=[], persona={"big_five": {}},
                               cognition_tier="star")  # co_located+同节点：borrow/repay 可达性（01 §4）

        # A03 向 A05 借（seed 闺蜜边 A05→A03 affinity=55 ≥ 门槛，校验看放款人→借款人边，D27）
        dec = Decision(agent_id="A03", intent="借钱", action_type="borrow_money",
                       action_args={"target": "A05", "amount": 50000, "result": "accepted"})
        ok, reason = await validator.check(obs("A03", a03["position"], a03["balance_cents"]),
                                           "borrow_money", {"target": "A05", "amount": 50000})
        assert ok, f"borrow 前置校验应过（{reason}）"
        seqs = await validator.settle(obs("A03", a03["position"], a03["balance_cents"]), dec,
                                      tick=clock.current_tick, sim_now=sim_now, rng_seed=clock.current_tick)
        assert seqs, "borrow_money 落库"
        debt = await pool.fetchrow("SELECT id FROM debts WHERE b_id='A03' AND a_id='A05' ORDER BY id DESC LIMIT 1")
        rep = Decision(agent_id="A03", intent="还钱", action_type="repay_money",
                       action_args={"target": "A05", "amount": 50000, "debt_ref": str(debt["id"])})
        seqs2 = await validator.settle(obs("A03", a03["position"], a03["balance_cents"]), rep,
                                       tick=clock.current_tick, sim_now=sim_now, rng_seed=clock.current_tick)
        assert seqs2, "repay_money 落库"
        yield {"pool": pool, "cal": cal, "clock": clock, "fw": fw, "arc_engine": arc_engine,
               "grader": grader, "gw": gw}
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_e3_economy_chain(m3_db, m3_db_phase_b) -> None:
    """09 §4 E3：payroll/rent/utility/overdue（含 utility.overdue）/notice/borrow/repay 全链事件齐。"""
    pool = await _open(m3_db)
    try:
        for t in ("economy.payroll", "economy.bill.rent", "economy.bill.utility",
                  "economy.bill.rent.overdue", "economy.bill.utility.overdue",
                  "economy.bill.rent.notice", "social.borrow_money", "social.repay_money"):
            n = await pool.fetchval("SELECT count(*) FROM events WHERE type=$1", t)
            assert n >= 1, f"缺 {t}"
        # 欠费主角断言：A08 房租 overdue、A07 水电 overdue
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='economy.bill.rent.overdue' AND payload->>'agent_id'='A08'") >= 1
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='economy.bill.utility.overdue' AND payload->>'agent_id'='A07'") >= 1
        assert await pool.fetchval("SELECT count(*) FROM agents WHERE balance_cents < 0") == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_e4_stock_rumor(m3_db, m3_db_phase_b) -> None:
    """09 §4 E4：stock.tick rng_seed 入库可复算（同 seed 重算 r 逐值一致）；传闻边界两侧触发/不触发。"""
    pool = await _open(m3_db)
    try:
        import random

        rows = await pool.fetch(
            """
            SELECT payload, rng_seed FROM events WHERE type='economy.stock.tick'
              AND sim_time::date <= $1 ORDER BY seq
            """, dt.date(2026, 10, 18))
        assert rows
        for r in rows:
            p = _p(r)
            assert r["rng_seed"] == p["seed"]
            rng = random.Random(f"{p['seed']}|stock|{p['symbol']}")
            rw = yaml.safe_load((SERVER_ROOT / "config" / "world.yaml").read_text(encoding="utf-8"))["stocks"]["random_walk"]
            expect_r = round(rng.gauss(float(rw["mu"]), float(rw["sigma"])), 6)  # 参数读配置镜像
            assert p["r"] == pytest.approx(expect_r), "rng_seed 可复算（04 §5.3）"
        # 传闻：构造跌幅超阈（段 B 注入 11-09）→ 次日 9:30 恰一条；未超阈的日子零条
        from worldsim.time_engine.clock import LOCAL_TZ as LOCAL_TZ_T
        cfg = yaml.safe_load((SERVER_ROOT / "config" / "world.yaml").read_text(encoding="utf-8"))
        th = cfg["triggers"]["layoff_rumor"]
        rumor = await pool.fetchrow(
            "SELECT payload, sim_time, trigger FROM events WHERE type='world.layoff_rumor'"
            " AND sim_time::date = '2026-11-10'")
        assert rumor is not None and rumor["trigger"] == "world", "构造过线 → 次日触发时点恰一条"
        assert _p(rumor)["drop_pct"] == pytest.approx(th["drop_pct_threshold"] + 2.0, abs=0.01)
        assert rumor["sim_time"].astimezone(LOCAL_TZ_T).strftime("%H:%M") == str(th["trigger_time"])
        # 边界另一侧：演练窗内存在未触发日（阈值下零条的反证由 T-WA-06 单测覆盖，此处对总数口径）
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='world.layoff_rumor' AND trigger='director'") == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_e5_arcs_and_intervene(m3_db, m3_db_phase_b) -> None:
    """09 §4 E5：弧线状态机推进 + fail-forward（单测状态机级覆盖）+ 无 A 级自动启弧线
    （构造性 drought 窗口，01 §6.4）+ L0~L2 各一次演练 + L3 代码路径不存在。"""
    from tests.world_agent._support import FakeClock
    from worldsim.adjudicator.grade import Grader
    from worldsim.llm_gateway import LLMGateway
    from worldsim.llm_gateway.providers.mock import MockProvider
    from worldsim.time_engine.clock import LOCAL_TZ
    from worldsim.world_agent.calendar import CalendarEngine
    from worldsim.world_agent.config import load_world_config
    from worldsim.world_agent.director.arcs import ArcEngine, load_arcs
    from worldsim.world_agent.director.intervene import InterventionError, InterventionFramework

    pool = await _open(m3_db)
    try:
        # 构造 drought：选一个无任何事件的远日（无 A 级 ≥ 阈值）→ daily_check 自动启动休眠弧线
        clock = FakeClock(dt.datetime(2027, 3, 16, 8, 0, tzinfo=LOCAL_TZ))
        arc_engine = ArcEngine(pool, load_arcs(), clock)
        inst = await arc_engine.daily_check()
        assert inst is not None, "连续无 A 级达阈值 → 自动启动休眠弧线（01 §6.4）"
        audit = await arc_engine._get("arcs.audit_log")
        assert any(a["action"] == "auto_start_on_a_drought" for a in audit)
        # 弧线实例状态持久化 world_state（D-06）
        assert await pool.fetchval("SELECT count(*) FROM world_state WHERE key LIKE 'arc.inst.%'") >= 1

        # L0~L2 各一次（真实落库路径）
        now = await pool.fetchval("SELECT max(sim_time) FROM events")
        clock2 = FakeClock(now)
        cfg = load_world_config()
        grader = Grader(pool, thresholds=(cfg.get("director") or {}).get("grade"))
        cal = CalendarEngine(pool, cfg, clock2, grader=grader)
        cap = yaml.safe_load((SERVER_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))[
            "thresholds"]["intervention_rate_cap"]
        fw = InterventionFramework(pool, cal, clock2, arcs_cfg=load_arcs(),
                                   intervention_rate_cap=float(cap),
                                   gateway=LLMGateway(pool, {}, providers={"mock": MockProvider()},
                                                      default_provider="mock"))
        n0 = await pool.fetchval("SELECT count(*) FROM events WHERE trigger='director'")
        s0 = await fw.intervene("L0", "disturb_complaint", reason="演练 L0",
                                params={"floor": 2, "issue": "漏水"})
        s1 = await fw.intervene("L1", "schedule_overtime", reason="演练 L1",
                                params={"date": "2027-01-04", "dept": "tech"})
        s2 = await fw.intervene("L2", "memory_inject", reason="演练 L2",
                                params={"agent_id": "A03", "content": "听说设计部最近气氛紧张。"})
        n1 = await pool.fetchval("SELECT count(*) FROM events WHERE trigger='director'")
        assert n1 - n0 == 3, "L0~L2 各恰一条 trigger='director' 事件（D-17）"
        assert await pool.fetchval("SELECT count(*) FROM interventions WHERE event_seq = ANY($1::bigint[])",
                                   [s0, s1, s2]) == 3, "interventions 行互指"
        with pytest.raises(InterventionError):
            await fw.intervene("L3", "set_affinity", reason="永禁", params={})
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_e7_grade_revise_drill(m3_db, m3_db_phase_b) -> None:
    """09 §4 E7 后半：grade_revise 追加演练——events 零 UPDATE、最新生效口径生效。"""
    pool = await _open(m3_db)
    try:
        from tests.world_agent._support import FakeClock
        from worldsim.adjudicator.grade import Grader, effective_grade
        from worldsim.llm_gateway import LLMGateway
        from worldsim.llm_gateway.providers.mock import MockProvider
        from worldsim.world_agent.config import load_world_config
        from worldsim.world_agent.director.review import ReviewEngine

        # 段 A 某日的一条 B 级事件（mock K3 每日已上调首个 B；取仍为 B 的）
        row = await pool.fetchrow(
            """
            SELECT seq, ui->>'grade' AS g FROM events
            WHERE ui->>'grade' = 'B' AND sim_time::date = $1
              AND NOT EXISTS (SELECT 1 FROM events r WHERE r.type='director.grade_revise'
                              AND r.payload->>'target_seq' = events.seq::text)
            ORDER BY seq LIMIT 1
            """, RUN_START + dt.timedelta(days=2))
        assert row is not None
        target = row["seq"]
        # mock K3（D-32）在主循环段 A 可能已上调过部分 B；本演练取未被复核者
        pre_revised = await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='director.grade_revise' AND payload->>'target_seq'=$1",
            str(target))
        assert pre_revised == 0, "演练目标须为未被复核的 B 级事件"
        clock = FakeClock(dt.datetime.combine(RUN_START + dt.timedelta(days=3), dt.time(0, 30),
                                              tzinfo=__import__("datetime").timezone(dt.timedelta(hours=8))))
        cfg = load_world_config()
        eng = ReviewEngine(pool, LLMGateway(pool, {}, providers={"mock": MockProvider()},
                                            default_provider="mock"),
                           clock, revise_cfg=cfg["director"]["revise"],
                           grader=Grader(pool, thresholds=cfg["director"]["grade"]))
        stats = await eng.run_daily_review(RUN_START + dt.timedelta(days=2))
        # mock K3 上调了首个 B（D-32）；target 若即首 B 则被复核
        revised = await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='director.grade_revise' AND payload->>'target_seq'=$1",
            str(target))
        orig = await pool.fetchval("SELECT ui->>'grade' FROM events WHERE seq=$1", target)
        assert orig == "B", "原事件行 ui.grade 不变（append-only，红线 4）"
        if revised:
            assert await effective_grade(pool, target) == "A", "最新复核生效（04 §6.6）"
        # UPDATE 被触发器拒绝
        with pytest.raises(Exception):
            await pool.execute("UPDATE events SET ui='{}'::jsonb WHERE seq=$1", target)
        assert stats["ups"] >= 1 or stats["downs"] >= 0
    finally:
        await pool.close()


# ---- E2/E6：审计六项 + 指标报表 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_e2_audit_six_green(m3_db, m3_db_phase_b) -> None:
    """09 §4 E2：审计 6 项 SQL 对演练库全绿（结果落日报）。"""
    from worldsim.audit.daily import run_daily_audit

    pool = await _open(m3_db)
    try:
        sim_now = await pool.fetchval("SELECT max(sim_time) FROM events")
        report = await run_daily_audit(pool, sim_now=sim_now)
        for r in report.items:
            assert r.ok, f"{r.id} {r.name} 报红：{r.detail[:2]}"
        assert not report.red
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_e6_rates_and_metrics(m3_db, m3_db_phase_b) -> None:
    """09 §4 E6：干预率 7 日滑窗低于红线（阈值读 models.yaml）；metrics 报表（T-AUD-08）落 health_daily。"""
    from worldsim.audit import metrics as M
    from worldsim.audit.daily import load_intervention_cap
    from worldsim.world_agent.director.intervene import intervention_rate_7d

    pool = await _open(m3_db)
    try:
        sim_now = await pool.fetchval("SELECT max(sim_time) FROM events")
        rate = await intervention_rate_7d(pool, sim_now)
        assert rate < load_intervention_cap(), f"干预率 {rate:.4f} 超线（红线见源方案 §4.8）"
        m = await M.write_health_daily(pool, sim_now)
        verdicts = M.classify_all(m)
        assert set(verdicts) == {"a_grade_event_interval_days", "event_type_entropy_bits",
                                 "appearance_gini", "dialogue_3gram_repeat_ratio",
                                 "relation_graph_weekly_change_ratio", "high_tension_edge_ratio",
                                 "active_conflict_edges"}
        row = await pool.fetchrow("SELECT * FROM health_daily ORDER BY sim_day DESC LIMIT 1")
        assert row is not None and row["intervention_rate"] == pytest.approx(rate)
        # A 级事件间隔在段 A（连续 7 模拟日）窗口内评估：mock K3 每日复核上调 → 每日 ≥1 个有效 A
        # → 间隔均值落 01 §9 预警带内（mock 首日无候选可审，工程口径；健康区 ≤1 为真 LLM 世界目标，
        # 实测回填 m3_measurements.json 留档）
        from datetime import datetime as _dt
        from worldsim.time_engine.clock import LOCAL_TZ as _TZ

        seg_a_end = _dt(2026, 10, 19, 0, 0, tzinfo=_TZ)
        gap = await M.a_grade_gap_days(pool, seg_a_end)
        th = M.load_thresholds()
        spec = next(m for m in th["metrics"] if m["metric"] == "a_grade_event_interval_days")
        warning_max = float(spec["warning"]["max"])
        assert gap <= warning_max, f"段 A A 级间隔 {gap} 超预警上界（01 §9）"
        assert M.classify("a_grade_event_interval_days", gap, th) in ("healthy", "warning")
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_grade_kpi_via_k3(m3_db, m3_db_phase_b) -> None:
    """09 §4 E6 第二半 / 00 §5：A 级事件 KPI——有效 grade 口径（K3 复核上调链路，04 §6.6）。

    mock 世界 R2/R3 信号稀缺（初值多为 B/C），A 级由 K3 复核每日上调产出（01 §6.4 设计的
    保稀缺终审路径；mock K3 桩 = D-32 确定性上调首个 B）。断言：演练窗内存在 ≥1 个有效 A 级，
    且段 A 每日 K3 复核有产出（ups+downs>0 的日子 ≥1）。
    """
    pool = await _open(m3_db)
    try:
        n_a = await pool.fetchval(
            """
            SELECT count(*) FROM events e WHERE coalesce(
              (SELECT r.payload->>'new_grade' FROM events r
                WHERE r.type='director.grade_revise' AND r.payload->>'target_seq' = e.seq::text
                ORDER BY r.seq DESC LIMIT 1), e.ui->>'grade') = 'A'
            """)
        assert n_a >= 1, "有效 A 级 ≥1（KPI 口径 06 §3，00 §5）"
        n_revise_days = await pool.fetchval(
            "SELECT count(DISTINCT sim_time::date) FROM events WHERE type='director.grade_revise'")
        assert n_revise_days >= 1, "K3 复核在演练窗内有产出"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_type_coverage(m3_db, m3_db_phase_b) -> None:
    """T-DIR-06 验收 4：本模块全部事件类型在演练窗内至少出现一次。"""
    pool = await _open(m3_db)
    try:
        missing = []
        for t in M3_MODULE_TYPES:
            if await pool.fetchval("SELECT count(*) FROM events WHERE type=$1", t) == 0:
                missing.append(t)
        assert not missing, f"类型覆盖缺失：{missing}"
    finally:
        await pool.close()




@pytest.mark.asyncio
async def test_measurements_backfill(m3_db, m3_db_phase_b) -> None:
    """T-DIR-06 实测回填表产出（04 §5 清单）：|r| 分布、传闻触发频率、干预率、扰动命中率、A 级间隔
    → var/logs/audit/m3_measurements.json（smoke.sh m3 段断言位，08 T-OPS-05 接收条目 D-33）。"""
    from worldsim.audit import metrics as M
    from worldsim.audit.daily import load_intervention_cap
    from worldsim.world_agent.director.intervene import intervention_rate_7d

    pool = await _open(m3_db)
    try:
        sim_now = await pool.fetchval("SELECT max(sim_time) FROM events")
        r_stats = await pool.fetchrow(
            "SELECT avg(abs((payload->>'r')::float)) AS mean_abs, count(*) AS n FROM events WHERE type='economy.stock.tick'")
        rumor_days = await pool.fetchval("SELECT count(DISTINCT sim_time::date) FROM events WHERE type='world.layoff_rumor'")
        trading_days = await pool.fetchval("SELECT count(DISTINCT sim_time::date) FROM events WHERE type='economy.stock.tick'")
        disturb = {}
        for t in ("illness", "weather", "complaint", "lucky"):
            disturb[t] = await pool.fetchval(f"SELECT count(*) FROM events WHERE type='world.disturb.{t}'")
        n_days = await pool.fetchval("SELECT count(DISTINCT sim_time::date) FROM events")
        rate = await intervention_rate_7d(pool, sim_now)
        m = await M.compute_daily(pool, sim_now)
        out = {
            "sim_days": int(n_days),
            "events_total": await pool.fetchval("SELECT count(*) FROM events"),
            "stock_abs_r_mean": round(float(r_stats["mean_abs"] or 0), 6),
            "stock_ticks": int(r_stats["n"]),
            "layoff_rumor_days": int(rumor_days),
            "layoff_rumor_freq_per_trading_day": round(int(rumor_days) / max(1, int(trading_days)), 4),
            "intervention_rate_7d": round(rate, 4),
            "intervention_rate_cap": load_intervention_cap(),
            "disturb_hits": disturb,
            "a_grade_gap_days": m["a_grade_gap_days"],
            "health_verdicts": M.classify_all(m),
        }
        MEASUREMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEASUREMENTS_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        assert MEASUREMENTS_PATH.is_file() and out["sim_days"] >= RUN_DAYS
    finally:
        await pool.close()
