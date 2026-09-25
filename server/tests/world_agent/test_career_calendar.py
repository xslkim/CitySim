"""T-WA-07 职场日历验收（04 文档 T-WA-07 验收 1~5）。"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import flush_agg, make_engine
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.calendar import register_career_jobs, settle_perf_review
from worldsim.world_agent.economy import register_economy_jobs

_DB = "worldsim_wa_career"


def _p(row) -> dict:
    return json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])


def _registry_keys(type_name: str) -> set[str]:
    with (DDL_DIR.parent / "config" / "event_types.yaml").open(encoding="utf-8") as f:
        reg = yaml.safe_load(f)
    entry = next(t for t in reg["types"] if t["type"] == type_name)
    keys = entry["payload_keys"]
    return set(keys.get("public") or []) | set(keys.get("conditional") or [])


@pytest.fixture(scope="module")
def career_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


@pytest.mark.asyncio
async def test_promotion_window(career_dsn) -> None:
    """验收 1：窗口日每部门恰一条、slots 与配置一致、无 defense_at 键；答辩排期经 world.announce 承载。"""
    cal, clock, pool, agg = await make_engine(career_dsn, dt.datetime(2027, 3, 31, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_career_jobs(cal)
        cfg = cal.cfg["triggers"]["promotion_window"]
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime(2027, 4, 1, 23, 50, tzinfo=LOCAL_TZ))  # 季度首月 1 日（周四，非节假日）
        await cal.tick()
        rows = await pool.fetch("SELECT payload FROM events WHERE type='world.promotion_window'")
        depts = cal.cfg["company"]["departments"]
        assert len(rows) == len(depts), "每部门恰一条"
        seen = set()
        for r in rows:
            p = _p(r)
            assert "defense_at" not in p, "defense_at 键已删（06 §1.2 / D-01 转正）"
            assert set(p.keys()) == {"dept", "slots", "candidates"}
            assert p["slots"] == int(cfg["slots_per_dept"])
            seen.add(p["dept"])
            # candidates = 该部门全体 P1（D-09）
            expect = [
                r2["id"] for r2 in await pool.fetch(
                    "SELECT id FROM agents WHERE department=$1 AND job_title='P1' ORDER BY id", p["dept"])
            ]
            assert p["candidates"] == expect
        assert seen == {d["name"] for d in depts}
        # 答辩排期：窗口月第 3 个周六 19:00（01 §1.6），经 world.announce body 承载
        ann = await pool.fetchrow(
            "SELECT payload FROM events WHERE type='world.announce' ORDER BY seq DESC LIMIT 1")
        defense = ann and _p(ann)["body"]
        assert defense and "2027-04-17" in defense and str(cfg["defense"]["time"]) in defense
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_perf_review_calendar(career_dsn) -> None:
    """验收 2：触发日历（每月最后一个周五，读配置）全员各一条、grade ∈ {S,A,B,C}、manager_id 键在。"""
    cal, clock, pool, agg = await make_engine(career_dsn, dt.datetime(2027, 1, 29, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_career_jobs(cal)
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime(2027, 1, 29, 23, 50, tzinfo=LOCAL_TZ))  # 2027-01 最后一个周五
        await cal.tick()
        await flush_agg(agg, cal, clock.now_sim())
        rows = await pool.fetch("SELECT payload FROM events WHERE type='world.perf_review'")
        n_agents = await pool.fetchval("SELECT count(*) FROM agents")
        assert len(rows) == n_agents, "全员各一条"
        for r in rows:
            p = _p(r)
            assert p["grade"] in ("S", "A", "B", "C")
            assert "manager_id" in p  # 8 人小世界无 NPC 经理 → null（D-26）
        # 非触发日不落：次日（周六）零新增
        seq0 = await pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        clock.set(dt.datetime(2027, 1, 30, 23, 50, tzinfo=LOCAL_TZ))
        await cal.tick()
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE type='world.perf_review' AND seq > $1", seq0) == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_raise_lands_next_payroll(career_dsn) -> None:
    """验收 3：S/A delta_salary 与月薪档比例口径（01 §1.5）一致，下个发薪日 payroll 反映新月薪。"""
    cal, clock, pool, agg = await make_engine(career_dsn, dt.datetime(2027, 5, 28, 6, 0, tzinfo=LOCAL_TZ))
    try:
        register_career_jobs(cal)
        register_economy_jobs(cal)
        raise_pct = {k: float(v) for k, v in cal.cfg["economy"]["perf_review_raise_pct"].items()}
        salary0 = dict(await cal.get_state("economy.salary"))
        await cal.mark_settled(clock.now_sim())
        fire = dt.datetime(2027, 5, 28, 16, 0, tzinfo=LOCAL_TZ)
        grades = {f"A0{i}": ("S" if i == 1 else ("A" if i == 2 else "B")) for i in range(1, 9)}
        await settle_perf_review(cal, fire, grades=grades)
        row = await pool.fetchrow(
            "SELECT payload FROM events WHERE type='world.perf_review' AND payload->>'agent_id'='A01' ORDER BY seq DESC")
        p = _p(row)
        assert p["delta_salary"] == round(int(salary0["A01"]) * raise_pct["S"] / 100.0)
        salary1 = dict(await cal.get_state("economy.salary"))
        assert int(salary1["A01"]) == int(salary0["A01"]) + p["delta_salary"]
        assert int(salary1["A02"]) == int(salary0["A02"]) + round(int(salary0["A02"]) * raise_pct["A"] / 100.0)
        assert int(salary1["A03"]) == int(salary0["A03"])  # B 不涨
        # 下个发薪日 payroll 反映新月薪（实际入账经 economy.payroll，06 §1.2 注释口径）
        clock.set(dt.datetime(2027, 6, 1, 23, 50, tzinfo=LOCAL_TZ))
        await cal.tick()
        pr = await pool.fetchrow(
            "SELECT payload FROM events WHERE type='economy.payroll' AND payload->>'agent_id'='A01' ORDER BY seq DESC")
        commute = int(cal.cfg["economy"]["commute"]["amount_cents"])
        assert _p(pr)["amount_cents"] == int(salary1["A01"]) - commute
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_c_grade_chain(career_dsn) -> None:
    """验收 4：C 级情绪变更落 state.needs_delta（cause 链接）；连续 2C 后候选池状态为真。"""
    cal, clock, pool, agg = await make_engine(career_dsn, dt.datetime(2027, 7, 30, 6, 0, tzinfo=LOCAL_TZ))
    try:
        cfg = cal.cfg["triggers"]["perf_review"]
        grades1 = {f"A0{i}": ("C" if i == 2 else "B") for i in range(1, 9)}
        await settle_perf_review(cal, dt.datetime(2027, 7, 30, 16, 0, tzinfo=LOCAL_TZ), grades=grades1)
        await flush_agg(agg, cal, clock.now_sim())
        seq = await pool.fetchval(
            "SELECT seq FROM events WHERE type='world.perf_review' AND payload->>'agent_id'='A02' ORDER BY seq DESC LIMIT 1")
        rows = await pool.fetch("SELECT payload FROM events WHERE type='state.needs_delta'")
        linked = [
            c for r in rows for c in _p(r)["changes"]
            if c["cause"] == str(seq) and c["agent_id"] == "A02" and c["need"] == "mood"
        ]
        assert linked and linked[0]["delta"] == float(cfg["c_mood_delta"]), "C 级情绪 -15（读配置）"
        assert "A02" not in list(await cal.get_state("layoff_pool", [])), "1C 不进池"
        # 连续第 2 个 C
        grades2 = dict(grades1)
        await settle_perf_review(cal, dt.datetime(2027, 8, 27, 16, 0, tzinfo=LOCAL_TZ), grades=grades2)
        assert "A02" in list(await cal.get_state("layoff_pool")), "连续 2C 进裁员候选池（01 §1.5）"
    finally:
        await pool.close()


def test_payload_keys_match_registry() -> None:
    """验收 5：两类型 payload 键与 event_types.yaml 注册表（01 T-CFG-05 单源）逐行 diff 为空。"""
    from worldsim.world_agent.config import load_world_config

    load_world_config()  # 配置链路可加载（对拍前提）
    assert {"dept", "slots", "candidates"} == _registry_keys("world.promotion_window")
    assert {"agent_id", "manager_id", "grade", "delta_salary"} == _registry_keys("world.perf_review")
