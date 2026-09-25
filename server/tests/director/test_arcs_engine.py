"""T-DIR-01 弧线状态机引擎验收（04 文档 T-DIR-01 验收 1~5）。

私有 scratch 库；模板用例内置合成 arcs.yaml（tmp_path），六骨架模板验收归 T-DIR-02。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import FakeClock
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.director.arcs import ArcEngine, ArcSchemaError, load_arcs

_DB = "worldsim_dir_arcs"


@pytest.fixture(scope="module")
def arcs_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


def _meta() -> dict:
    return {"concurrency_cap": 3, "cooldown_days": 14, "a_drought_days": 2}


def _tpl(arc_id: str, *, stages=None, fail_forward=None, payoff_type: str = "告白成功",
         setup_days=(2, 10), budget=None) -> dict:
    return {
        "arc_id": arc_id, "title": f"测试弧 {arc_id}",
        "stage_machine": stages or [
            {"stage": "S1", "exit": {"pred": "count_events", "args": {"type": "dialogue.chat", "min": 1}}},
            {"stage": "S2", "exit": {"pred": "count_events", "args": {"type": "dialogue.confess", "min": 1}},
             "hooks": [{"intervene": {"level": "L0", "action": "announce",
                                      "params": {"title": "t", "body": "b", "scope": "all"}}}]},
            {"stage": "S3", "exit": {"pred": "count_events", "args": {"type": "world.announce", "min": 1}}},
        ],
        "fail_forward": fail_forward if fail_forward is not None else [
            {"at": "S2", "if_blocked": {"pred": "stage_age_days", "args": {"min": 5}}, "degrade_to": "done"}],
        "intervention_budget": budget or {"L1": 2, "L2": 1},
        "actors": {"main": ["A01"], "support": []},
        "min_days": setup_days[0], "max_days": setup_days[1],
        "payoff_beat": {"type": payoff_type, "setup_days": list(setup_days),
                        "burst_event": "dialogue.confess", "aftermath": "关系重构"},
    }


def _write_arcs(tmp_path, arcs: list[dict]) -> str:
    p = tmp_path / "arcs.yaml"
    p.write_text(yaml.safe_dump({"meta": _meta(), "arcs": arcs}, allow_unicode=True), encoding="utf-8")
    return str(p)


def test_schema_validation(tmp_path) -> None:
    """验收 1：缺 payoff_beat / 非法爽点枚举 / 缺 fail_forward 的模板拒绝加载。"""
    good = _tpl("ARC-T1")
    assert load_arcs(_write_arcs(tmp_path, [good]))["arcs"][0]["arc_id"] == "ARC-T1"
    bad1 = _tpl("ARC-B1")
    del bad1["payoff_beat"]
    with pytest.raises(ArcSchemaError):
        load_arcs(_write_arcs(tmp_path, [bad1]))
    bad2 = _tpl("ARC-B2", payoff_type="天降横财")
    with pytest.raises(ArcSchemaError):
        load_arcs(_write_arcs(tmp_path, [bad2]))
    bad3 = _tpl("ARC-B3")
    bad3["fail_forward"] = []
    # 空 fail_forward 列表本身合法（schema 必填字段存在性）；改为删除字段
    del bad3["fail_forward"]
    with pytest.raises(ArcSchemaError):
        load_arcs(_write_arcs(tmp_path, [bad3]))
    # 未注册 predicate（D-16 白名单）拒绝
    bad4 = _tpl("ARC-B4", stages=[
        {"stage": "S1", "exit": {"pred": "free_text_evil", "args": {}}},
        {"stage": "S2"},
    ])
    with pytest.raises(ArcSchemaError):
        load_arcs(_write_arcs(tmp_path, [bad4]))


async def _insert_event(pool, type_: str, sim_now: dt.datetime, *, grade: str | None = None) -> int:
    ui = json.dumps({"grade": grade}) if grade else None
    return await pool.fetchval(
        """
        INSERT INTO events (tick, sim_time, type, source, trigger, visibility, payload, ui)
        VALUES (0, $1, $2, 'world', 'world', 'internal', '{}'::jsonb, $3::jsonb) RETURNING seq
        """, sim_now, type_, ui)


@pytest.mark.asyncio
async def test_state_machine_advance(arcs_dsn, tmp_path) -> None:
    """验收 2：注入事件流驱动 S1→末态，hooks 逐次触发（经 intervene 统一入口）、关联 arc_id。"""
    import asyncpg

    pool = await asyncpg.create_pool(arcs_dsn, min_size=1, max_size=2)
    try:
        clock = FakeClock(dt.datetime(2027, 3, 1, 8, 0, tzinfo=LOCAL_TZ))
        cfg = load_arcs(_write_arcs(tmp_path, [_tpl("ARC-T1")]))
        calls: list[dict] = []

        async def fake_intervene(level, action, *, arc_id=None, reason="", params=None):
            calls.append({"level": level, "action": action, "arc_id": arc_id, "reason": reason})
            return await _insert_event(pool, "world.announce", clock.now_sim())

        eng = ArcEngine(pool, cfg, clock, intervene=fake_intervene)
        inst = await eng.start_arc("ARC-T1", reason="test")
        assert inst["stage"] == "S1"
        # S1 exit：注入 dialogue.chat
        await _insert_event(pool, "dialogue.chat", clock.now_sim())
        await eng.tick()
        assert (await eng.get_instance("ARC-T1"))["stage"] == "S2"
        assert calls and calls[0]["arc_id"] == "ARC-T1" and calls[0]["level"] == "L0", "进入 S2 钩子触发且带 arc_id"
        # S2→S3 受 min_days 铺垫门禁（01 §11.3 <min 不爆发）
        await _insert_event(pool, "dialogue.confess", clock.now_sim())
        await eng.tick()
        assert (await eng.get_instance("ARC-T1"))["stage"] == "S2", "铺垫 <min_days 不进入末态"
        clock.set(clock.now_sim() + dt.timedelta(days=3))
        await eng.tick()
        assert (await eng.get_instance("ARC-T1"))["stage"] == "S3"
        # S3 exit（world.announce 已由 hook 落库）→ 完成
        await eng.tick()
        done = await eng.get_instance("ARC-T1")
        assert done["status"] == "done"
        audit = await eng._get("arcs.audit_log")
        actions = [a["action"] for a in audit if a["arc_id"] == "ARC-T1"]
        assert actions[:2] == ["start", "advance"] and "complete" in actions
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fail_forward_on_max_days(arcs_dsn, tmp_path) -> None:
    """验收 3：超 max_days 未推进自动走 degrade_to 分支且收尾（审计留痕全程）。"""
    import asyncpg

    pool = await asyncpg.create_pool(arcs_dsn, min_size=1, max_size=2)
    try:
        clock = FakeClock(dt.datetime(2027, 4, 1, 8, 0, tzinfo=LOCAL_TZ))
        tpl = _tpl("ARC-FF", stages=[
            {"stage": "S1", "exit": {"pred": "count_events", "args": {"type": "never.happens", "min": 1}}},
            {"stage": "S2"},
        ], setup_days=(2, 4))
        cfg = load_arcs(_write_arcs(tmp_path, [tpl]))
        eng = ArcEngine(pool, cfg, clock, intervene=None)
        await eng.start_arc("ARC-FF", reason="test")
        clock.set(clock.now_sim() + dt.timedelta(days=5))  # > max_days(=setup_days[1])
        await eng.tick()
        inst = await eng.get_instance("ARC-FF")
        assert inst["status"] == "done" and inst["via"] == "fail_forward"
        audit = await eng._get("arcs.audit_log")
        assert any(a["action"] == "force_fail_forward" and a["arc_id"] == "ARC-FF" for a in audit)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_concurrency_cap_and_cooldown(arcs_dsn, tmp_path) -> None:
    """验收 4：超并发上限排队；冷却期内同模板重启被拒（口径读配置）。"""
    import asyncpg

    pool = await asyncpg.create_pool(arcs_dsn, min_size=1, max_size=2)
    try:
        clock = FakeClock(dt.datetime(2027, 5, 10, 8, 0, tzinfo=LOCAL_TZ))
        cfg = load_arcs(_write_arcs(tmp_path, [_tpl(f"ARC-C{i}") for i in range(1, 5)]))
        eng = ArcEngine(pool, cfg, clock)
        for i in range(1, 4):
            assert await eng.start_arc(f"ARC-C{i}") is not None
        assert await eng.start_arc("ARC-C4") is None, "超并发上限排队（01 §6.2 ≤3）"
        audit = await eng._get("arcs.audit_log")
        assert any(a["action"] == "queued_over_concurrency" and a["arc_id"] == "ARC-C4" for a in audit)
        # 完成一条 → 冷却期内同模板拒绝重启
        clock.set(clock.now_sim() + dt.timedelta(days=99))  # 超 max_days
        await eng.tick()
        assert (await eng.get_instance("ARC-C1"))["status"] == "done"
        assert await eng.start_arc("ARC-C1") is None, "冷却期内拒绝重启"
        audit = await eng._get("arcs.audit_log")
        assert any(a["action"] == "start_rejected_cooldown" and a["arc_id"] == "ARC-C1" for a in audit)
        # 冷却期满可重启
        clock.set(clock.now_sim() + dt.timedelta(days=int(cfg["meta"]["cooldown_days"]) + 1))
        assert await eng.start_arc("ARC-C1") is not None
        # 释放出名额后排队弧可启动
        assert await eng.start_arc("ARC-C4") is not None
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_auto_start_on_a_drought(arcs_dsn, tmp_path) -> None:
    """验收 5：连续无 A 级达阈值 → 自动启动一条休眠弧线并落审计日志（01 §6.4）。"""
    import asyncpg

    pool = await asyncpg.create_pool(arcs_dsn, min_size=1, max_size=2)
    try:
        clock = FakeClock(dt.datetime(2027, 6, 20, 8, 0, tzinfo=LOCAL_TZ))
        cfg = load_arcs(_write_arcs(tmp_path, [_tpl("ARC-D1")]))
        eng = ArcEngine(pool, cfg, clock)
        # 清干净窗口内的 A 级痕迹：本用例库内 grade_revise 为空，ui.grade 只由本用例控制
        await pool.execute("DELETE FROM world_state WHERE key IN ('arcs.active', 'arc.inst.ARC-D1')")
        # 当日有 A 级 → 不启动
        await _insert_event(pool, "dialogue.chat", clock.now_sim(), grade="A")
        assert await eng.daily_check() is None
        # 无 A 级连续 ≥ 阈值（阈值读配置）：时钟前进到无 A 的次日/次次日
        clock.set(clock.now_sim() + dt.timedelta(days=int(cfg["meta"]["a_drought_days"]) + 1))
        inst = await eng.daily_check()
        assert inst is not None and inst["arc_id"] == "ARC-D1"
        audit = await eng._get("arcs.audit_log")
        assert any(a["action"] == "auto_start_on_a_drought" for a in audit)
    finally:
        await pool.close()
