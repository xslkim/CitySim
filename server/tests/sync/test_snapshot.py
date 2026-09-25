"""T-SYN-02 快照流组帧验收（05 §3.6 白名单；构造复用 02 T-ADJ-09，禁第二实现）。"""

from __future__ import annotations

import datetime as dt
import gzip
import json

import asyncpg
import pytest
import pytest_asyncio

from tests.conftest import DDL_DIR, _build_db, _drop_db
from worldsim.snapshot.canonical import canonical, sha256_hex
from worldsim.snapshot.dump import PERSONA_DISPLAY_KEYS, dump_snapshot
from worldsim.sync.snapshot import (
    build_snapshot_frame,
    load_snapshot_frame_from_disk,
    pending_snapshot_days,
)
from worldsim.time_engine.clock import LOCAL_TZ

pytestmark = pytest.mark.asyncio

DB_NAME = "worldsim_sync_snapshot_test"
SIM_DAY = dt.date(2026, 10, 12)
SIM_NOW = dt.datetime(2026, 10, 13, 0, 0, tzinfo=LOCAL_TZ)

BANNED_KEYS = {"secret", "trigger_point", "balance_cents", "holdings", "contrast",
               "speech_style", "text_raw", "content", "embedding"}


def _scan_banned(obj, path: str = "") -> list[str]:
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in BANNED_KEYS:
                hits.append(f"{path}.{k}")
            hits += _scan_banned(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += _scan_banned(v, f"{path}[{i}]")
    return hits


@pytest_asyncio.fixture
async def pool():
    dsn = _build_db(DB_NAME, DDL_DIR / "schema_v1.sql", DDL_DIR / "seed_8.sql")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    yield p
    await p.close()
    _drop_db(DB_NAME)


async def test_snapshot_whitelist(pool) -> None:
    """验收 4：persona_display 恰六键；全 JSON 递归无 secret/trigger_point/balance_cents/holdings 等禁出键。"""
    frame = await build_snapshot_frame(pool, sim_day=SIM_DAY, sim_now=SIM_NOW)
    assert frame["frame"] == "snapshot" and frame["schema_version"] == 1
    state = frame["state"]
    assert set(state) == {"sim", "agents", "relations", "economy", "health", "announcements"}
    assert len(state["agents"]) == 8  # seed_8
    for a in state["agents"]:
        assert set(a["persona_display"]) == set(PERSONA_DISPLAY_KEYS)
    assert _scan_banned(state) == []
    assert frame["digest"] == "sha256:" + sha256_hex(canonical(state))
    assert frame["sim_day"] == SIM_DAY.isoformat()


async def test_snapshot_digest_reproducible(pool) -> None:
    """验收 5：同一 state 两次构造 digest 相同；改一键值 digest 变。"""
    f1 = await build_snapshot_frame(pool, sim_day=SIM_DAY, sim_now=SIM_NOW)
    f2 = await build_snapshot_frame(pool, sim_day=SIM_DAY, sim_now=SIM_NOW)
    assert f1["digest"] == f2["digest"]
    tampered = json.loads(json.dumps(f1["state"]))
    tampered["agents"][0]["mood"] = 1
    assert "sha256:" + sha256_hex(canonical(tampered)) != f1["digest"]


async def test_disk_backfill_roundtrip(pool, tmp_path) -> None:
    """断档补发：dump_snapshot 落盘 → load_snapshot_frame_from_disk 回放同 digest；位点过滤正确。"""
    await dump_snapshot(pool, sim_day=SIM_DAY, out_dir=tmp_path, tick=1, sim_now=SIM_NOW)
    day2 = SIM_DAY + dt.timedelta(days=1)
    await dump_snapshot(pool, sim_day=day2, out_dir=tmp_path, tick=2,
                        sim_now=SIM_NOW + dt.timedelta(days=1))
    days = pending_snapshot_days(SIM_DAY, out_dir=tmp_path)
    assert days == [day2]
    assert pending_snapshot_days(None, out_dir=tmp_path) == [SIM_DAY, day2]
    frame = load_snapshot_frame_from_disk(SIM_DAY, out_dir=tmp_path)
    assert frame is not None and frame["sim_day"] == SIM_DAY.isoformat()
    with gzip.open(tmp_path / f"snapshot_{SIM_DAY.isoformat()}.whitelist.json.gz", "rt",
                   encoding="utf-8") as f:
        assert frame["state"] == json.load(f)
    assert _scan_banned(frame["state"]) == []
    assert load_snapshot_frame_from_disk(dt.date(2020, 1, 1), out_dir=tmp_path) is None
