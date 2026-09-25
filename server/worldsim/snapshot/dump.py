"""每模拟日 world_state 全量快照落盘（02 T-ADJ-09；04 §12.1、05 §3.6、04 §3.3 挂载点）。

- 时点 = 每模拟日结束（模拟日界 00:00，05 §3.6 刷新频率行），经 T-TIME-03 batch 段回调注册表挂载
  （04 §3.3 批后时点；注册名 `kernel.snapshot`，main.py 接线）。
- dump 对象 = 主库 `world_state` + `agents`/`relations`/`goals` 当前态全量（05 §3.6 源行），
  JSON gzip 落盘 `var/snapshot/`（目录布局 04 §1.5；保留口径 04 §12.1：本地 14 天，清理归运维），
  文件名按 `sim_day`（`snapshot_<YYYY-MM-DD>.json.gz`），同 sim_day 重跑幂等覆盖。
- 同一生成函数产出**白名单子集**（05 §3.6 `state` 白名单逐字键集合），供 M6 快照流送副本库
  `world_state_snapshot`（04 §1.3，推送归 07 文档）；生成时算 `digest=sha256(canonical(state))`
  随文件 sidecar（`<file>.digest`）留存（05 §3.6 digest 适配；canonical 唯一实现 =
  `worldsim/snapshot/canonical.py`，R2 §A.7）。
- 白名单边界（05 §3.6 脱敏规则与 P2-6）：余额/持仓、secrets、`secret`/`trigger_point`/`contrast`
  私下半、记忆原文不进白名单子集。
- M1 数据源口径（02 文档）：`economy.stocks[]` 取 `world_state` 最新价（D3）；日结成本读
  `world_state` 键 `health.cost_daily`（08 T-AUD-08 写）、映射为 `health.cost_daily_micro_cny`，
  M3 起携带、M1 缺省为 null。
- dump 后写 `world_state` 键 `snapshot.latest`（最近快照 sim_day + 文件路径 + digest 留痕，
  供 08 T-OPS-06 恢复演练定位；键族登记 = 02 文档 D12）。

工程默认（登记 02 文档偏差表）：`contrast_public` = contrast 首个"，"/","前半（D31）；
`agents[].mood` = 情绪值（needs.mood 数值；agents.mood JSON 为内部键载体，不出站）（D32）；
`agents[].activity` = 当日最近一条事件类型名（D32）；`routine.regular` 读 needs.yaml `schedule`
段镜像（01 §1.4）（D32）；`announcements` = 最近 5 条 `world.announce` 的展示文本（D32）。
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
from pathlib import Path
from typing import Any

from .canonical import canonical, sha256_hex

log = logging.getLogger(__name__)

ANNOUNCEMENTS_LIMIT = 5  # 近期公告条数（工程默认，D32）
PERSONA_DISPLAY_KEYS = ("big_five", "backstory", "appearance",
                        "signature_quirk", "contrast_public", "speech_style_public")  # 六键上限（05 §3.6）


def contrast_public(contrast: str | None) -> str | None:
    """表/里反差的公开半（01 §11.1/05 §3.6 P2-6）：首个"，"/"，"前半（工程默认切分，D31）。"""
    if not contrast:
        return None
    for sep in ("，", ",", ";", "；"):
        if sep in contrast:
            return contrast.split(sep, 1)[0].strip() or None
    return contrast.strip() or None


def speech_style_public(style: dict[str, Any] | None) -> str | None:
    """speech_style 的公开可展示摘要 = tone + sentence_len + catchphrase（01 §11.1 唯一定义方）；
    taboo 属内部生成约束，不进 public 摘要。"""
    if not style:
        return None
    parts = [str(style.get("tone") or "").strip(), str(style.get("sentence_len") or "").strip() + "句"]
    catchphrase = str(style.get("catchphrase") or "").strip()
    if catchphrase:
        parts.append(f"口癖'{catchphrase}'")
    return " · ".join(p for p in parts if p and p != "句") or None


async def read_full_state(pool: Any) -> dict[str, Any]:
    """全量态：world_state 全键 + agents/relations/goals 全量（05 §3.6 源行）。"""
    ws_rows = await pool.fetch("SELECT key, value, updated_tick FROM world_state ORDER BY key")
    world_state = {}
    for r in ws_rows:
        v = r["value"]
        world_state[r["key"]] = json.loads(v) if isinstance(v, str) else v
    agents = []
    for r in await pool.fetch("SELECT * FROM agents ORDER BY id"):
        row = dict(r)
        for k in ("persona", "needs", "mood", "holdings"):
            if isinstance(row.get(k), str):
                row[k] = json.loads(row[k])
        row["created_at"] = row["created_at"].isoformat()
        if row.get("next_due_sim") is not None:
            row["next_due_sim"] = row["next_due_sim"].isoformat()
        agents.append(row)
    relations = [dict(r) for r in await pool.fetch(
        "SELECT a_id, b_id, affinity, tension, labels, one_line, last_event_seq FROM relations ORDER BY a_id, b_id"
    )]
    goals = [dict(r) for r in await pool.fetch(
        "SELECT id, agent_id, sim_week, goal, blocked_count, status, frustration FROM goals ORDER BY id"
    )]
    return {"world_state": world_state, "agents": agents, "relations": relations, "goals": goals}


def build_whitelist_state(
    full: dict[str, Any], *, sim_day: dt.date, sim_time: dt.datetime, compression_ratio: float,
    schedule: dict[str, Any] | None = None, latest_activity: dict[str, str] | None = None,
    announcements: list[str] | None = None,
) -> dict[str, Any]:
    """白名单子集（05 §3.6 `state` 键集合逐字）；禁出键永不进（余额/持仓/secrets/私下半/记忆原文）。"""
    sleep = (schedule or {}).get("sleep", ["00:30", "06:30"])
    work = (schedule or {}).get("work", {"start": "09:00", "end": "18:00"})
    agents = []
    for a in full["agents"]:
        persona = a.get("persona") or {}
        needs = a.get("needs") or {}
        agents.append({
            "id": a["id"], "name": a["name"], "gender": a["gender"], "age": a["age"],
            "room_no": a.get("room_no"), "department": a.get("department"), "job_title": a.get("job_title"),
            "cognition_tier": a["cognition_tier"],
            "persona_display": {
                "big_five": persona.get("big_five"),
                "backstory": persona.get("backstory"),
                "appearance": persona.get("appearance"),
                "signature_quirk": persona.get("signature_quirk"),
                "contrast_public": contrast_public(persona.get("contrast")),
                "speech_style_public": speech_style_public(persona.get("speech_style")),
            },
            "routine": {
                "regular": {"work": f"工作日 {work.get('start')}~{work.get('end')}",
                            "sleep": f"{sleep[0]}~{sleep[1]}"},
                "weekly_bias_public": persona.get("weekly_routine_bias"),
            },
            "needs": needs,
            "mood": needs.get("mood"),  # 情绪值（D32：agents.mood JSON 内部键不出站）
            "position": a.get("position"),
            "activity": (latest_activity or {}).get(a["id"]),
            "goals": [
                {"goal": g["goal"], "blocked_count": g["blocked_count"], "frustration": g["frustration"]}
                for g in full["goals"] if g["agent_id"] == a["id"] and g["status"] == "active"
            ],
        })
    relations = [
        {"a": r["a_id"], "b": r["b_id"], "affinity": r["affinity"], "tension": r["tension"],
         "labels": r["labels"], "one_line": r.get("one_line")}
        for r in full["relations"]
    ]
    stocks_raw = full["world_state"].get("economy.stocks") or {}
    stocks = [{"symbol": k, "price": stocks_raw[k]} for k in sorted(stocks_raw)]  # M1 = world_state 最新价（D3）
    health_cost = full["world_state"].get("health.cost_daily")
    return {
        "sim": {"day": sim_day.isoformat(), "sim_time": sim_time.isoformat(),
                "compression_ratio": float(compression_ratio)},
        "agents": agents,
        "relations": relations,
        "economy": {"stocks": stocks},
        "health": {"cost_daily_micro_cny": health_cost},  # M1 缺省 null（08 T-AUD-08 写入后携带）
        "announcements": list(announcements or []),
    }


def write_snapshot_files(
    full: dict[str, Any], whitelist: dict[str, Any], *, sim_day: dt.date, out_dir: str | Path,
) -> tuple[Path, Path, str]:
    """落盘全量 gzip + 白名单子集 gzip + digest sidecar；同 sim_day 幂等覆盖。返回（全量路径， 白名单路径， digest）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    digest = "sha256:" + sha256_hex(canonical(whitelist))
    day = sim_day.isoformat()
    full_path = out / f"snapshot_{day}.json.gz"
    wl_path = out / f"snapshot_{day}.whitelist.json.gz"
    dg_path = out / f"snapshot_{day}.digest"
    with gzip.open(full_path, "wt", encoding="utf-8") as f:
        f.write(canonical(full))
    with gzip.open(wl_path, "wt", encoding="utf-8") as f:
        f.write(canonical(whitelist))
    dg_path.write_text(digest + "\n", encoding="utf-8")
    return full_path, wl_path, digest


async def dump_snapshot(
    pool: Any, *, sim_day: dt.date, out_dir: str | Path, tick: int, sim_now: dt.datetime,
    compression_ratio: float = 1.0, schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """每模拟日全量快照：读全量 → 白名单子集 → 落盘 + sidecar digest → world_state 留痕键（D12）。"""
    full = await read_full_state(pool)
    announcements = [
        str(r["text"])
        for r in await pool.fetch(
            """
            SELECT payload->>'text_display' AS text FROM events
            WHERE type='world.announce' AND payload ? 'text_display' ORDER BY seq DESC LIMIT $1
            """,
            ANNOUNCEMENTS_LIMIT,
        )
    ]
    activity_rows = await pool.fetch(
        """
        SELECT DISTINCT ON (u.agent_id) u.agent_id, e.type FROM events e
        CROSS JOIN LATERAL unnest(e.actors) AS u(agent_id)
        WHERE e.sim_time >= $1 AND e.type NOT LIKE 'state.%%' AND e.type NOT LIKE 'time.%%'
        ORDER BY u.agent_id, e.seq DESC
        """,
        dt.datetime.combine(sim_day, dt.time(0, 0), tzinfo=sim_now.tzinfo),
    )
    latest_activity = {r["agent_id"]: r["type"] for r in activity_rows}
    whitelist = build_whitelist_state(
        full, sim_day=sim_day, sim_time=sim_now, compression_ratio=compression_ratio,
        schedule=schedule, latest_activity=latest_activity, announcements=announcements,
    )
    full_path, wl_path, digest = write_snapshot_files(full, whitelist, sim_day=sim_day, out_dir=out_dir)
    await pool.execute(
        """
        INSERT INTO world_state (key, value, updated_tick) VALUES ('snapshot.latest', $1::jsonb, $2)
        ON CONFLICT (key) DO UPDATE SET value=$1::jsonb, updated_tick=$2
        """,
        json.dumps({"sim_day": sim_day.isoformat(), "path": str(full_path),
                    "whitelist_path": str(wl_path), "digest": digest}, ensure_ascii=False),
        tick,
    )
    log.info("快照落盘：sim_day=%s → %s（digest=%s）", sim_day, full_path, digest[:19])
    return {"sim_day": sim_day.isoformat(), "path": str(full_path), "whitelist_path": str(wl_path),
            "digest": digest, "agents": len(full["agents"])}
