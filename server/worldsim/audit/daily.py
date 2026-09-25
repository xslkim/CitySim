"""每日审计执行器（08 T-AUD-01~07；04 §10.1 六项 + 样例立法 + 日报载体）。

- 注册表 6 项 = `{id, name, sql_path, sample_path, severity}`；**SQL 文件即唯一实现**，
  禁止在 Python 内拼 SQL 串绕过审计文件（08 T-AUD-01）。
- 日报载体：`var/logs/audit/<sim_day>.json`（机读）+ 同基名 `.md`（人读摘要）；任一不过 →
  该日标红并告警（内存队列 → T-OPS-01 文件通道）；**连续 3 个模拟日不过 → 经 `pause_cb`
  （clock.pause 路径，D8）暂停时钟进人工**（04 §10.1）。
- 锚点口径（08 D9/D10 工程落点，偏差表 D14 登记）：`initial_fund` / 初始 needs 为审计首跑时
  从事件流反推并固化进 `world_state`（`audit.initial_fund` / `audit.initial_needs`）的锚定值，
  此后一切漂移可检出；`sim_now` 由调用方（TIME/CLI）传入，SQL 内不出现 now() 以外的真实时钟。
- economy 类型清单从 `config/event_types.yaml` 审计域标记动态读入（06 §1.3，禁 LIKE 前缀法）；
  干预率上限从 `config/models.yaml` `thresholds:` 段读（01 T-CFG-02 唯一载体）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

log = logging.getLogger(__name__)

SERVER_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVER_ROOT.parent
SQL_DIR = Path(__file__).parent / "sql"
SAMPLES_DIR = Path(__file__).parent / "samples"
REPORT_DIR = REPO_ROOT / "var" / "logs" / "audit"

INITIAL_FUND_KEY = "audit.initial_fund"
INITIAL_NEEDS_KEY = "audit.initial_needs"

NEED_KEYS = ("hunger", "energy", "mood", "social", "achievement", "wealth")  # 01 §3.1 六需求
SAMPLE_SIZE = 5  # 审计⑥ 抽样人数（04 §10.1 ⑥「抽样 5 人」逐字）


@dataclass(frozen=True)
class AuditItem:
    id: str
    name: str
    sql_path: Path
    sample_path: Path
    severity: str = "red"


@dataclass
class ItemResult:
    id: str
    name: str
    ok: bool
    violations: int
    detail: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AuditReport:
    sim_day: str
    sim_now: str
    items: list[ItemResult]
    red: bool
    sampled_agents: list[str] = field(default_factory=list)


REGISTRY: tuple[AuditItem, ...] = (
    AuditItem("01_balance_conservation", "余额守恒", SQL_DIR / "01_balance_conservation.sql",
              SAMPLES_DIR / "01_violation.sql"),
    AuditItem("02_no_teleport", "不在两地/瞬移", SQL_DIR / "02_no_teleport.sql",
              SAMPLES_DIR / "02_violation.sql"),
    AuditItem("03_memory_single_source", "双人记忆同源", SQL_DIR / "03_memory_single_source.sql",
              SAMPLES_DIR / "03_violation.sql"),
    AuditItem("04_intervention_rate", "干预率滑窗", SQL_DIR / "04_intervention_rate.sql",
              SAMPLES_DIR / "04_violation.sql"),
    AuditItem("05_lod_completeness", "LOD 记录完整", SQL_DIR / "05_lod_completeness.sql",
              SAMPLES_DIR / "05_violation.sql"),
    AuditItem("06_cache_reconcile", "缓存对账", SQL_DIR / "06_cache_reconcile.sql",
              SAMPLES_DIR / "06_violation.sql"),
)


def get_item(item_id: str) -> AuditItem:
    for it in REGISTRY:
        if it.id == item_id or it.id.split("_", 1)[1] == item_id or it.id.startswith(str(item_id)):
            return it
    raise KeyError(f"未知审计项 {item_id!r}（注册表：{[i.id for i in REGISTRY]}）")


def load_economy_types() -> list[str]:
    """economy 审计类型清单 = event_types.yaml 审计域标记全集（06 §1.3；唯一数据源，不另存副本）。"""
    with (SERVER_ROOT / "config" / "event_types.yaml").open(encoding="utf-8") as f:
        reg = yaml.safe_load(f)
    return sorted(t["type"] for t in reg["types"] if t.get("audit_domain") == "economy")


def load_intervention_cap() -> float:
    """干预率上限（models.yaml thresholds 段，01 T-CFG-02 唯一载体；数值持有方 = 源方案 §4.8）。"""
    with (SERVER_ROOT / "config" / "models.yaml").open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return float(cfg["thresholds"]["intervention_rate_cap"])


def sample_agents(agent_ids: list[str], sim_day: dt.date, n: int = SAMPLE_SIZE) -> list[str]:
    """审计⑥ 每日固定随机种子抽 5 人（可复现；名单写入日报，08 T-AUD-07 R4 工程口径）。"""
    ids = sorted(agent_ids)
    rng = random.Random(f"audit-sample|{sim_day.isoformat()}")
    return sorted(rng.sample(ids, min(n, len(ids))))


# ---- 锚点（首跑固化；此后漂移可检出） -------------------------------------------------


async def ensure_anchors(pool: Any) -> None:
    """initial_fund / initial_needs 锚定（首跑 = 当前缓存 − 事件流累计，自洽零点）。"""
    if await pool.fetchval("SELECT 1 FROM world_state WHERE key=$1", INITIAL_FUND_KEY) is None:
        fund = await pool.fetchval(
            """
            SELECT (SELECT SUM(balance_cents) FROM agents)
                 - (SELECT coalesce(SUM((payload->>'amount_cents')::bigint), 0) FROM events
                     WHERE type = ANY($1::text[]))
            """, load_economy_types())
        await pool.execute(
            "INSERT INTO world_state (key, value, updated_tick) VALUES ($1, $2::jsonb, 0)",
            INITIAL_FUND_KEY, json.dumps(int(fund or 0)))
        log.info("审计锚点 initial_fund = %s（首跑固化，08 D9/D14）", fund)
    if await pool.fetchval("SELECT 1 FROM world_state WHERE key=$1", INITIAL_NEEDS_KEY) is None:
        rows = await pool.fetch("SELECT id, needs FROM agents")
        initial: dict[str, dict[str, float]] = {}
        sql = (SQL_DIR / "06_cache_reconcile.sql").read_text(encoding="utf-8")
        for r in rows:
            needs = r["needs"]
            needs = json.loads(needs) if isinstance(needs, str) else dict(needs)
            acc = {k: float(needs.get(k, 0.0)) for k in NEED_KEYS}
            for d in await pool.fetch(sql, r["id"]):
                c = d["change"]
                c = json.loads(c) if isinstance(c, str) else dict(c)
                acc[c["need"]] = round(acc.get(c["need"], 0.0) - float(c["delta"]), 2)
            initial[r["id"]] = {k: max(0.0, min(100.0, v)) for k, v in acc.items()}
        await pool.execute(
            "INSERT INTO world_state (key, value, updated_tick) VALUES ($1, $2::jsonb, 0)",
            INITIAL_NEEDS_KEY, json.dumps(initial, ensure_ascii=False))
        log.info("审计锚点 initial_needs 固化（%d 人）", len(initial))


# ---- 执行器 ---------------------------------------------------------------------------


async def run_item(pool: Any, item: AuditItem, *, sim_now: dt.datetime,
                   sim_day: dt.date) -> ItemResult:
    """跑单项审计：SQL 文件返回行即违规（⑥ 走重算比对口径）。"""
    if not item.sql_path.is_file():
        raise FileNotFoundError(f"审计项 {item.id} 缺 SQL 文件 {item.sql_path}")
    sql = item.sql_path.read_text(encoding="utf-8")
    if item.id in ("01_balance_conservation", "06_cache_reconcile"):
        await ensure_anchors(pool)  # 锚点懒建（首跑固化；run_daily_audit 亦调，幂等）
    if item.id == "01_balance_conservation":
        fund_raw = await pool.fetchval("SELECT value FROM world_state WHERE key=$1", INITIAL_FUND_KEY)
        fund = json.loads(fund_raw) if isinstance(fund_raw, str) else fund_raw
        rows = await pool.fetch(sql, load_economy_types(), int(fund))
    elif item.id == "04_intervention_rate":
        rows = await pool.fetch(sql, sim_now, load_intervention_cap())
    elif item.id == "06_cache_reconcile":
        rows = await _run_cache_reconcile(pool, sql, sim_day)
    elif item.id == "05_lod_completeness":
        rows = await pool.fetch(sql, sim_now)
    else:
        rows = await pool.fetch(sql)
    detail = [dict(r) for r in rows[:50]]
    return ItemResult(item.id, item.name, ok=len(rows) == 0, violations=len(rows), detail=detail)


async def _run_cache_reconcile(pool: Any, sql: str, sim_day: dt.date) -> list[dict[str, Any]]:
    """审计⑥：抽样 5 人，needs 缓存 == 锚定初始值 + Σ state.needs_delta（04 §10.1 ⑥；
    逐项比对 + new_value 交叉校验）。"""
    raw = await pool.fetchval("SELECT value FROM world_state WHERE key=$1", INITIAL_NEEDS_KEY)
    initial = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    agent_ids = [r["id"] for r in await pool.fetch("SELECT id FROM agents ORDER BY id")]
    violations: list[dict[str, Any]] = []
    for aid in sample_agents(agent_ids, sim_day):
        acc = {k: float(v) for k, v in (initial.get(aid) or {}).items()}
        for k in NEED_KEYS:
            acc.setdefault(k, 0.0)
        for d in await pool.fetch(sql, aid):
            c = d["change"]
            c = json.loads(c) if isinstance(c, str) else dict(c)
            acc[c["need"]] = round(max(0.0, min(100.0, acc[c["need"]] + float(c["delta"]))), 2)
            # new_value 交叉校验（04 §6.5 记录自洽性）
            if abs(float(c["new_value"]) - acc[c["need"]]) > 0.01:
                violations.append({"agent_id": aid, "need": c["need"], "kind": "new_value_mismatch",
                                   "event_seq": int(d["seq"]), "expected": acc[c["need"]],
                                   "recorded": float(c["new_value"])})
        cur = await pool.fetchval("SELECT needs FROM agents WHERE id=$1", aid)
        cur = json.loads(cur) if isinstance(cur, str) else dict(cur or {})
        for k in NEED_KEYS:
            if abs(float(cur.get(k, 0.0)) - acc[k]) > 0.01:
                violations.append({"agent_id": aid, "need": k, "kind": "cache_mismatch",
                                   "expected": acc[k], "cached": float(cur.get(k, 0.0))})
    return violations


async def run_daily_audit(pool: Any, *, sim_now: dt.datetime, sim_day: dt.date | None = None,
                          only: str | None = None, report_dir: Path = REPORT_DIR) -> AuditReport:
    """跑全量（或 `--only` 单项）审计并落日报。任一不过 → 标红。"""
    await ensure_anchors(pool)
    day = sim_day or sim_now.date()
    items = [get_item(only)] if only else list(REGISTRY)
    results = [await run_item(pool, it, sim_now=sim_now, sim_day=day) for it in items]
    sampled = sample_agents(
        [r["id"] for r in await pool.fetch("SELECT id FROM agents ORDER BY id")], day)
    report = AuditReport(day.isoformat(), sim_now.isoformat(), results,
                         red=any(not r.ok for r in results), sampled_agents=sampled)
    _write_report(report, report_dir)
    return report


def _write_report(report: AuditReport, report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "sim_day": report.sim_day, "sim_now": report.sim_now, "red": report.red,
        "sampled_agents": report.sampled_agents,
        "items": [{"id": r.id, "name": r.name, "ok": r.ok, "violations": r.violations,
                   "detail": r.detail} for r in report.items],
    }
    (report_dir / f"{report.sim_day}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [f"# 审计日报 {report.sim_day}", "",
             f"结论：{'**红**' if report.red else '绿'}", ""]
    for r in report.items:
        lines.append(f"- {'✅' if r.ok else '❌'} {r.id} {r.name}：违规 {r.violations} 条")
    (report_dir / f"{report.sim_day}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if report.red:
        log.warning("审计日报 %s 标红：%s", report.sim_day,
                    [r.id for r in report.items if not r.ok])


def consecutive_red_days(report_dir: Path = REPORT_DIR, *, upto: dt.date | None = None) -> int:
    """最近连续标红模拟日数（日报文件口径，04 §10.1「连续 3 天不过 → 暂停时钟进人工」）。"""
    if not report_dir.is_dir():
        return 0
    days = sorted(p.stem for p in report_dir.glob("*.json") if upto is None or p.stem <= upto.isoformat())
    streak = 0
    for stem in reversed(days):
        payload = json.loads((report_dir / f"{stem}.json").read_text(encoding="utf-8"))
        if payload.get("red"):
            streak += 1
        else:
            break
    return streak


async def audit_and_maybe_pause(pool: Any, *, sim_now: dt.datetime,
                                pause_cb: Callable[[str], Awaitable[None]] | None = None,
                                report_dir: Path = REPORT_DIR) -> AuditReport:
    """日报 + 连续 3 日红 → pause_cb（clock.pause 路径，04 §10.1/D8）。"""
    report = await run_daily_audit(pool, sim_now=sim_now, report_dir=report_dir)
    streak = consecutive_red_days(report_dir)
    if streak >= 3 and pause_cb is not None:
        await pause_cb(f"审计连续 {streak} 个模拟日标红（04 §10.1：暂停时钟进人工）")
    return report


async def daily_loop(pool: Any, *, clock: Any, stop: Any,
                     pause_cb: Callable[[str], Awaitable[None]] | None = None,
                     report_dir: Path = REPORT_DIR) -> None:
    """自动入口（04 §2.2 audit.daily_loop 协程）：模拟日界翻转且非 batch 段时跑前一日审计。"""
    import asyncio

    last_day: dt.date | None = None
    while not stop.is_set():
        try:
            await asyncio.sleep(30)
            now = clock.now_sim()
            if getattr(clock, "batch_mode", False):
                continue
            day = now.date()
            if last_day is not None and day > last_day:
                await audit_and_maybe_pause(pool, sim_now=now, pause_cb=pause_cb,
                                            report_dir=report_dir)
            last_day = day
        except Exception:
            log.exception("audit daily_loop 异常（下一周期重试）")
