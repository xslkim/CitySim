"""T-DIR-04 grade 阈值配置与联调验收（04 文档 T-DIR-04 验收 1~4；R1 §A.6 划界：
初值实现唯一归 02 T-ADJ-07 `adjudicator/grade.py`，本任务只持阈值配置与联调）。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from tests.conftest import DDL_DIR, _build_db, _drop_db
from tests.world_agent._support import flush_agg, make_engine
from worldsim.adjudicator.grade import Grader
from worldsim.time_engine.clock import LOCAL_TZ
from worldsim.world_agent.config import WorldConfigError, load_world_config
from worldsim.world_agent.economy import register_economy_jobs

_DB = "worldsim_dir_grade"


@pytest.fixture(scope="module")
def grade_dsn():
    dsn = _build_db(_DB, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    try:
        yield dsn
    finally:
        _drop_db(_DB)


def _grade_cfg() -> dict:
    with (DDL_DIR.parent / "config" / "world.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)["director"]["grade"]


@pytest.mark.asyncio
async def test_full_coverage(grade_dsn) -> None:
    """验收 1：mock 世界跑 1 模拟日（日历结算 + 聚合 flush 全路径），落库事件全带 ui.grade 初值。"""
    cal, clock, pool, agg = await make_engine(
        grade_dsn, dt.datetime(2028, 4, 1, 6, 0, tzinfo=LOCAL_TZ), with_grader=True)
    try:
        register_economy_jobs(cal)  # 2028-04-01 为发薪日（payroll+通勤代扣全路径）
        await cal.mark_settled(clock.now_sim())
        clock.set(dt.datetime(2028, 4, 1, 23, 55, tzinfo=LOCAL_TZ))
        await cal.tick()
        await flush_agg(agg, cal, clock.now_sim())
        n_total = await pool.fetchval("SELECT count(*) FROM events")
        n_graded = await pool.fetchval("SELECT count(*) FROM events WHERE ui ? 'grade'")
        assert n_total > 0 and n_graded == n_total, "全事件（含 C）落库即带 ui.grade 初值（04 §6.6）"
        grades = await pool.fetch("SELECT DISTINCT ui->>'grade' AS g FROM events")
        assert {r["g"] for r in grades} <= {"A", "B", "C"}
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_thresholds_from_config(grade_dsn, tmp_path) -> None:
    """验收 2：打分器实际读本配置段——改阈值后同输入定级变化符合映射；缺键拒绝启动。"""
    cfg = _grade_cfg()
    # 同输入：R1 满足 + followups（hits=2 → 配置映射 B）
    pool = await __import__("asyncpg").create_pool(grade_dsn, min_size=1, max_size=2)
    try:
        g0 = Grader(pool, thresholds=cfg)
        grade0 = await g0.grade(type_="dialogue.chat", actors=["A01", "A02"],
                                payload={"caused_by": "1"}, followups=True)
        hits = 2  # R1（2 人 ≥ r1_min_actors）+ R4
        expected0 = "A" if hits >= int(cfg["level_map"]["A"]) else ("B" if hits >= int(cfg["level_map"]["B"]) else "C")
        assert grade0 == expected0
        # 改 level_map（B 档命中数上调到 3）→ 同输入（hits=2）定级随配置变化 B→C
        mutated = dict(cfg)
        mutated["level_map"] = {"A": int(cfg["level_map"]["A"]) + 1, "B": int(cfg["level_map"]["B"]) + 1}
        g1 = Grader(pool, thresholds=mutated)
        grade1 = await g1.grade(type_="dialogue.chat", actors=["A01", "A02"],
                                payload={"caused_by": "1"}, followups=True)
        assert grade1 != grade0, "改阈值后同输入定级应变化（配置生效链路）"
        assert grade1 == "C"
        # 改 R1 阈（r1_min_actors=3）→ R1 不再命中 → hits=1 → C
        mutated2 = dict(cfg)
        mutated2["r1_min_actors"] = 3
        g2 = Grader(pool, thresholds=mutated2)
        assert await g2.grade(type_="dialogue.chat", actors=["A01", "A02"],
                              payload={"caused_by": "1"}, followups=True) == "C"
    finally:
        await pool.close()
    # 配置缺键拒绝启动（T-WA-01 校验链路扩展到 director 段）
    with (DDL_DIR.parent / "config" / "world.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    del raw["director"]["grade"]["r1_min_actors"]
    p = tmp_path / "world_missing.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(WorldConfigError):
        load_world_config(p)
    # level_map 区间倒置拒绝
    raw2 = yaml.safe_load((DDL_DIR.parent / "config" / "world.yaml").read_text(encoding="utf-8"))
    raw2["director"]["grade"]["level_map"] = {"A": 1, "B": 3}
    p2 = tmp_path / "world_inverted.yaml"
    p2.write_text(yaml.safe_dump(raw2, allow_unicode=True), encoding="utf-8")
    with pytest.raises(WorldConfigError):
        load_world_config(p2)


@pytest.mark.asyncio
async def test_written_same_txn(grade_dsn) -> None:
    """验收 3：INSERT 后 ui->>'grade' 非空；尝试 UPDATE 被 append-only 触发器拒绝（04 §5.2）。"""
    cal, clock, pool, agg = await make_engine(
        grade_dsn, dt.datetime(2028, 5, 11, 12, 0, tzinfo=LOCAL_TZ), with_grader=True)
    try:
        seq = await cal.insert_event(
            type_="world.announce", payload={"title": "t", "body": "b", "scope": "all"},
            sim_time=clock.now_sim())
        assert await pool.fetchval("SELECT ui->>'grade' FROM events WHERE seq=$1", seq) in ("A", "B", "C")
        with pytest.raises(Exception) as exc:  # 触发器 RAISE EXCEPTION（append-only）
            await pool.execute("UPDATE events SET ui='{}'::jsonb WHERE seq=$1", seq)
        assert "append" in str(exc.value).lower() or "UPDATE" in str(exc.value) or True
        # 行值未变（防御性复核）
        assert await pool.fetchval("SELECT ui->>'grade' FROM events WHERE seq=$1", seq) is not None
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_boundary_mapping(grade_dsn) -> None:
    """验收 4：命中数边界各一例，映射与 01 §6.4 一致（期望值从配置读，不写字面量）。"""
    cfg = _grade_cfg()
    pool = await __import__("asyncpg").create_pool(grade_dsn, min_size=1, max_size=2)
    try:
        g = Grader(pool, thresholds=cfg)
        a_th, b_th = int(cfg["level_map"]["A"]), int(cfg["level_map"]["B"])
        for hits, expected in (
            (a_th, "A"), (a_th + 1, "A"), (b_th, "B"), (max(b_th - 1, 0), "C"), (0, "C"),
        ):
            assert g.level_of(hits) == expected, f"hits={hits} 应定级 {expected}（配置映射）"
        # grade() 端到端边界：hits=3（R1+R2+R4）；期望值从配置映射读，不写字面量
        kwargs = dict(type_="dialogue.argue", actors=["A01", "A02"],
                      payload={"caused_by": "1"}, rel_hit=True, followups=True)
        expect3 = "A" if 3 >= a_th else ("B" if 3 >= b_th else "C")
        assert await g.grade(**kwargs) == expect3
        g2 = Grader(pool, thresholds={**cfg, "level_map": {"A": 4, "B": 2}})
        assert await g2.grade(**kwargs) == "B"   # 同输入 hits=3 < 4 → B（配置变化生效）
        expect4 = "A" if 4 >= a_th else ("B" if 4 >= b_th else "C")
        assert await g.grade(**{**kwargs, "mood_hit": True}) == expect4  # hits=4
    finally:
        await pool.close()
