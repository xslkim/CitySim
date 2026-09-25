"""快照流组帧出站（07 T-SYN-02；04 §1.3 快照流 / 05 §3.6）。

- `state` 构造复用 02 T-ADJ-09 `snapshot/dump.py`（read_full_state + build_whitelist_state +
  fetch_announcements/fetch_latest_activity），禁第二实现（R2 §A.7）；
  digest = 'sha256:' + sha256(canonical(state))（canonical 唯一实现 = snapshot/canonical.py）。
- 日界实时组帧走 `build_snapshot_frame`；断档补发从 `var/snapshot/snapshot_<day>.whitelist.json.gz`
  + sidecar digest 回放（与 04 §12.1 本地快照同源，幂等覆盖语义不变）。
- 位点 = `sync_state.last_acked_snapshot_day`。
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
from typing import Any

from ..snapshot.canonical import canonical, sha256_hex
from ..snapshot.dump import (
    build_whitelist_state,
    fetch_announcements,
    fetch_latest_activity,
    read_full_state,
)

SCHEMA_VERSION = 1  # 04 §1.3 约束块：每帧带 schema_version

DEFAULT_SNAPSHOT_DIR = Path(__file__).resolve().parents[3] / "var" / "snapshot"


async def build_snapshot_frame(
    pool: Any, *, sim_day: dt.date, sim_now: dt.datetime,
    compression_ratio: float = 1.0, schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """日界实时组帧：读全量 → 白名单子集 → 随帧 digest（05 §3.6）。返回 04 §1.3 snapshot 帧。"""
    full = await read_full_state(pool)
    state = build_whitelist_state(
        full, sim_day=sim_day, sim_time=sim_now, compression_ratio=compression_ratio,
        schedule=schedule,
        latest_activity=await fetch_latest_activity(pool, sim_day=sim_day, sim_now=sim_now),
        announcements=await fetch_announcements(pool),
    )
    return {"frame": "snapshot", "schema_version": SCHEMA_VERSION, "sim_day": sim_day.isoformat(),
            "digest": "sha256:" + sha256_hex(canonical(state)), "state": state}


def load_snapshot_frame_from_disk(
    sim_day: dt.date, *, out_dir: str | Path = DEFAULT_SNAPSHOT_DIR,
) -> dict[str, Any] | None:
    """断档补发：从 var/snapshot 白名单 gzip + sidecar digest 回放（无文件 → None）。"""
    day = sim_day.isoformat()
    wl_path = Path(out_dir) / f"snapshot_{day}.whitelist.json.gz"
    dg_path = Path(out_dir) / f"snapshot_{day}.digest"
    if not wl_path.exists() or not dg_path.exists():
        return None
    with gzip.open(wl_path, "rt", encoding="utf-8") as f:
        state = json.load(f)
    digest = dg_path.read_text(encoding="utf-8").strip()
    if digest != "sha256:" + sha256_hex(canonical(state)):
        raise ValueError(f"快照 {day} sidecar digest 与文件内容不符（{wl_path}）")
    return {"frame": "snapshot", "schema_version": SCHEMA_VERSION, "sim_day": day,
            "digest": digest, "state": state}


def pending_snapshot_days(
    last_acked_day: dt.date | None, *, out_dir: str | Path = DEFAULT_SNAPSHOT_DIR,
) -> list[dt.date]:
    """断档清单：var/snapshot 中 sim_day 晚于位点的快照日（升序）。"""
    days = []
    for p in sorted(Path(out_dir).glob("snapshot_*.whitelist.json.gz")):
        day = dt.date.fromisoformat(p.name.removeprefix("snapshot_").removesuffix(".whitelist.json.gz"))
        if last_acked_day is None or day > last_acked_day:
            days.append(day)
    return days
