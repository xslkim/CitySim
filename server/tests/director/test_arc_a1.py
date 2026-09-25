"""T-DIR-02 A1 暗恋链曝光 弧线骨架验收（04 文档 T-DIR-02；01 §6.2 表行 1）。

每模板 ≥2 例：主路径（铺垫→爆发→余波）+ fail-forward 分支；setup_days 窗口断言读配置。
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.director import _arc_support as S
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.director.arcs import load_arcs

_DB = "worldsim_dir_a1"
T1 = dt.datetime(2028, 3, 6, 8, 0, tzinfo=LOCAL_TZ)
T2 = dt.datetime(2028, 6, 8, 8, 0, tzinfo=LOCAL_TZ)
T3 = dt.datetime(2028, 9, 7, 8, 0, tzinfo=LOCAL_TZ)


@pytest.fixture(scope="module")
def dsn():
    d = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield d
    finally:
        _drop_db(_DB)


def test_schema_ok() -> None:
    """验收 3：六模板过 T-DIR-01 schema 校验（CI 加载断言，一次覆盖全集）。"""
    cfg = load_arcs()
    ids = [a["arc_id"] for a in cfg["arcs"]]
    assert ids == ["ARC-A1", "ARC-A2", "ARC-A3", "ARC-A4", "ARC-A5", "ARC-A6"]


@pytest.mark.asyncio
async def test_main_path(dsn) -> None:
    """主路径：S1 暗涌→S2 察觉→S3 八卦扩散→S4 对峙→S5 收场（confess 爆发），hooks 经 intervene 带 arc_id。"""
    calls: list[dict] = []

    async def fake_intervene(level, action, *, arc_id=None, reason="", params=None):
        calls.append({"level": level, "action": action, "arc_id": arc_id})
        return 0

    eng, tpl, clock, pool = await S.build(dsn, "ARC-A1", intervene=fake_intervene, now=T1)
    try:
        inst = await S.drive_main_path(eng, tpl, pool, clock)
        assert inst["via"] == "advance"
        assert calls and calls[0]["arc_id"] == "ARC-A1" and calls[0]["level"] == "L0", "S4 入场钩子"
        audit = await eng._get("arcs.audit_log")
        assert [a["action"] for a in audit][:1] == ["start"] and "complete" in [a["action"] for a in audit]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fail_forward(dsn) -> None:
    """fail-forward：S4 无人敢对峙 3 日 → 降级 S5 收场（01 §6.2 A1 要点）。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A1", now=T2)
    try:
        await S.reset_arc(pool, "ARC-A1")
        await eng.start_arc("ARC-A1", reason="test")
        await S.drive_to_stage(eng, tpl, pool, clock, "S4")
        ff = next(f for f in tpl["fail_forward"] if f["at"] == "S4")
        clock.set(clock.now_sim() + dt.timedelta(days=int(ff["if_blocked"]["args"]["min"])))
        await eng.tick()
        inst = await eng.get_instance("ARC-A1")
        assert inst["stage"] == ff["degrade_to"]
        audit = await eng._get("arcs.audit_log")
        assert any(a["action"] == "fail_forward" and a.get("to") == "S5" for a in audit)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_setup_days_window(dsn) -> None:
    """验收 2：setup_days 区间读配置——<min 不爆发（铺垫门禁）、>max 强制 fail-forward 收尾。"""
    eng, tpl, clock, pool = await S.build(dsn, "ARC-A1", now=T3)
    try:
        await S.reset_arc(pool, "ARC-A1")
        min_d, max_d = (int(x) for x in tpl["payoff_beat"]["setup_days"])
        await eng.start_arc("ARC-A1", reason="test")
        await S.drive_to_stage(eng, tpl, pool, clock, "S4")  # 末态前一站
        # 立即满足 S4 exit（对拔）但 <min_days → 不进入末态
        await S.satisfy(pool, clock, tpl["stage_machine"][3]["exit"])
        await eng.tick()
        assert (await eng.get_instance("ARC-A1"))["stage"] == "S4", "<min 不爆发（01 §11.3）"
        # 超 max_days → 强制 fail-forward 收尾
        clock.set(clock.now_sim() + dt.timedelta(days=max_d + 1))
        await eng.tick()
        inst = await eng.get_instance("ARC-A1")
        assert inst["status"] == "done" and inst["via"] == "fail_forward"
    finally:
        await pool.close()
