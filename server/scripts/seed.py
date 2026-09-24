#!/usr/bin/env python3
"""seed 生成器（T-DB-04/T-DB-05）：从 config/agents.yaml + config/world.yaml 确定性生成 seed SQL。

用法（00 §1 A14，cwd = server/）：
  uv run python scripts/seed.py --ids A01..A08 --out ddl/seed_8.sql
  uv run python scripts/seed.py --ids A01..A40 --out ddl/seed_40.sql

确定性：同输入同输出，无随机/时钟依赖——一切"区间内取整"由 sha256(purpose:agent_id) 抽定
（内容生产，01 文档 §6 D9/D16）。配置路径走 env（WSIM_AGENTS_CONFIG / WSIM_WORLD_CONFIG，
默认 config/agents.yaml / config/world.yaml，00 §2 附表）。

口径：
- agents.balance_cents：01 §1.5 初始余额分层（老/普通/新，入住时长分层由 hire_months 推导，
  分层界读 world.yaml initial_balance.tenancy_rule），区间内整元抽定（×100 = 分，验收 %100=0）。
- agents.needs：六需求 60~80 区间初值（01 §3.1）叠加 needs_offset（01 §2.2），clamp 0~100。
- cognition_tier：A01~A08 = 'star'（04 §4.1 W1~W2 全员）；A09~A40 按 01 §2.3 度数规则——
  全 40 图 relations_initial 总度数降序（同度按 id 升序）前 12 = secondary、其余 = background
  （06 §3 LOD 三层 明星 8 / 次要 ~12 / 背景 ~20；分层名单属内容生产，T-DB-05）。
- relations：relations_initial 逐条落有序对（labels ← type 数组化、one_line ← note，§6 D9），
  target 须在选中 id 集内。
- debts：type=债主/欠款 的 relations_initial 解析出债务边（方向 = round2 §A.18：a_id 债权人 → b_id
  债务人），镜像对去重；金额解析自 note（"欠 … <数字>"）；due_sim = 锚点 + N 模拟日（note 含
  "剩 N 天" 取 N，否则取还款期限 14 模拟日——06 §3 登记，持有方 01 §3.2/§4，M1 配置镜像落地后改读配置）。
  初始债务 ≤¥5,000 豁免 borrow 单笔上限（01 §2.1 评审 P2-10）。
- goals：goals_initial 落 goals 表（sim_week=1, status='active'；明星层 3 条/人，01 §3.3）。
- world_state：clock.anchor（{anchor_sim, anchor_wall, ratio}，04 §3.2 值结构；键名 §6 D4；
  anchor_sim 取 2026-10-12 周一 00:00+08，对齐 04 §1.3 帧示例；anchor_wall=anchor_sim、ratio=1
  为冷启动占位，内核首启重锚，§6 D16）、economy.stocks（3 标的初值读 world.yaml）、
  economy.salary（每人月薪抽定键：按 world.yaml 工资三档在每人职级档区间整元抽定，单位分，
  供 M3 payroll 读取，T-DB-04 评审增项）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]

ANCHOR_SIM = datetime(2026, 10, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))  # §6 D4：2026-10-12 周一
DEBT_DEFAULT_DUE_DAYS = 14  # 还款期限（06 §3，持有方 01 §3.2/§4；M1 配置镜像落地后改读配置，§6 D16）
NEED_KEYS = ["hunger", "energy", "mood", "social", "achievement", "wealth"]  # 01 §3.1 六需求
NEED_BASE_MIN, NEED_BASE_SPAN = 60, 21  # 初始 60~80（01 §3.1）
STAR_IDS = [f"A{i:02d}" for i in range(1, 9)]  # 明星层 8 人（06 §3 LOD 三层）
SECONDARY_COUNT = 12  # 次要 ~12（06 §3；度数降序前 12，T-DB-05）

_AMOUNT_RE = re.compile(r"欠[^\d]*(\d+)")
_DUE_RE = re.compile(r"剩\s*(\d+)\s*天")


def _sha_int(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256(":".join(parts).encode("utf-8")).digest()[:8], "big")


def _draw_cents(purpose: str, agent_id: str, min_cents: int, max_cents: int) -> int:
    """[min,max] 闭区间整元抽定（分 = 元×100；区间界均整百，world.yaml 口径）。"""
    lo, hi = min_cents // 100, max_cents // 100
    return (lo + _sha_int(purpose, agent_id) % (hi - lo + 1)) * 100


def _sql_str(s: object) -> str:
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def _sql_json(obj: object) -> str:
    return _sql_str(json.dumps(obj, ensure_ascii=False, sort_keys=True)) + "::jsonb"


def _load_configs() -> tuple[list[dict], dict]:
    agents_path = SERVER_ROOT / os.environ.get("WSIM_AGENTS_CONFIG", "config/agents.yaml")
    world_path = SERVER_ROOT / os.environ.get("WSIM_WORLD_CONFIG", "config/world.yaml")
    with agents_path.open(encoding="utf-8") as f:
        agents = yaml.safe_load(f)["agents"]
    with world_path.open(encoding="utf-8") as f:
        world = yaml.safe_load(f)
    return agents, world


def _parse_ids(spec: str) -> list[str]:
    m = re.fullmatch(r"A(\d{2})\.\.A(\d{2})", spec)
    if not m:
        raise SystemExit(f"--ids 形态须为 A01..A08 / A01..A40，实收: {spec}")
    lo, hi = int(m.group(1)), int(m.group(2))
    return [f"A{i:02d}" for i in range(lo, hi + 1)]


def _tenancy_band(hire_months: int, rule: dict) -> str:
    """入住时长分层（01 §1.2/§1.5，hire_months 推导）：>12 老、3~12 普通、<1 新。"""
    if hire_months > rule["senior_gt_months"]:
        return "senior"
    if hire_months >= rule["regular_gte_months"]:
        return "regular"
    return "new"


def _tier_map(agents: list[dict]) -> dict[str, str]:
    """cognition_tier 分层：A01~A08 star；A09~A40 全图总度数降序前 12 secondary、其余 background。"""
    tiers = {a["agent_id"]: "background" for a in agents}
    degree = {a["agent_id"]: 0 for a in agents}
    for a in agents:
        for r in a.get("relations_initial", []):
            degree[a["agent_id"]] += 1
            degree[r["target"]] = degree.get(r["target"], 0) + 1
    ranked = sorted((a for a in agents if a["agent_id"] not in STAR_IDS),
                    key=lambda a: (-degree[a["agent_id"]], a["agent_id"]))
    for a in ranked[:SECONDARY_COUNT]:
        tiers[a["agent_id"]] = "secondary"
    for aid in STAR_IDS:
        tiers[aid] = "star"
    return tiers


def _needs_of(agent: dict) -> dict[str, int]:
    offset = agent.get("needs_offset") or {}
    needs: dict[str, int] = {}
    for k in NEED_KEYS:
        base = NEED_BASE_MIN + _sha_int("need", agent["agent_id"], k) % NEED_BASE_SPAN
        needs[k] = max(0, min(100, base + int(offset.get(k, 0))))
    return needs


def _position_of(agent: dict) -> str:
    room = agent.get("room")
    if room is None:
        return f"home.{agent['agent_id']}"  # 01 §1.6 校外抽象节点（16 名公司 NPC）
    return f"apt.L{int(str(room)[:-2])}.{room}"  # world.yaml locations.apartment.rooms.id_pattern


def _collect_debts(agents: list[dict], id_set: set[str]) -> list[dict]:
    """债主/欠款 relations → 债务边（镜像对去重；双向金额不一致即报错）。"""
    edges: dict[tuple[str, str], dict] = {}
    for a in agents:
        for r in a.get("relations_initial", []):
            if r["type"] == "债主":
                creditor, debtor = a["agent_id"], r["target"]
            elif r["type"] == "欠款":
                creditor, debtor = r["target"], a["agent_id"]
            else:
                continue
            note = r.get("note") or ""
            m_amount, m_due = _AMOUNT_RE.search(note), _DUE_RE.search(note)
            if not m_amount:
                raise SystemExit(f"债务 note 缺金额: {a['name']} → {r['target']}: {note!r}")
            amount_cents = int(m_amount.group(1)) * 100
            due_days = int(m_due.group(1)) if m_due else DEBT_DEFAULT_DUE_DAYS
            key = (creditor, debtor)
            if key in edges:
                prev = edges[key]
                if prev["amount_cents"] != amount_cents:
                    raise SystemExit(f"镜像债务金额不一致: {key} {prev['amount_cents']} vs {amount_cents}")
                prev["due_days"] = min(prev["due_days"], due_days)  # 双向 note 取更紧期限
            else:
                edges[key] = {"a_id": creditor, "b_id": debtor,
                              "amount_cents": amount_cents, "due_days": due_days}
    return sorted(
        (e for e in edges.values() if e["a_id"] in id_set and e["b_id"] in id_set),
        key=lambda e: (e["a_id"], e["b_id"]),
    )


def build_sql(ids: list[str]) -> str:
    all_agents, world = _load_configs()
    by_id = {a["agent_id"]: a for a in all_agents}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"agents.yaml 缺 id: {missing}")
    id_set = set(ids)
    selected = [by_id[i] for i in ids]
    tiers = _tier_map(all_agents)

    balance_rule = world["economy"]["initial_balance"]
    tenancy_rule = balance_rule["tenancy_rule"]
    bands = balance_rule["tiers"]
    salary_bands = world["economy"]["salary"]

    agent_rows: list[str] = []
    relation_rows: list[str] = []
    goal_rows: list[str] = []
    salaries: dict[str, int] = {}
    fund_total = 0

    for a in selected:
        aid = a["agent_id"]
        band = bands[_tenancy_band(int(a["hire_months"]), tenancy_rule)]
        balance = _draw_cents("balance", aid, band["min_cents"], band["max_cents"])
        fund_total += balance
        sb = salary_bands[a["position"]]
        salaries[aid] = _draw_cents("salary", aid, sb["min_cents"], sb["max_cents"])
        agent_rows.append(
            "(" + ", ".join([
                _sql_str(aid), _sql_str(a["name"]), _sql_str(a["gender"]), str(a["age"]),
                _sql_str(a.get("room")), _sql_str(a.get("department")), _sql_str(a["position"]),
                _sql_str(tiers[aid]), _sql_json(a), _sql_json(_needs_of(a)),
                str(balance), _sql_str(_position_of(a)),
            ]) + ")"
        )
        for r in a.get("relations_initial", []):
            if r["target"] not in id_set:
                continue  # 8 人小世界：越集关系不落库（seed_40 全集无裁减）
            labels = "{" + r["type"] + "}"
            relation_rows.append(
                "(" + ", ".join([
                    _sql_str(aid), _sql_str(r["target"]), str(r["affinity"]), str(r["tension"]),
                    _sql_str(labels) + "::text[]", _sql_str(r.get("note")),
                ]) + ")"
            )
        for g in a.get("goals_initial", []):
            goal_rows.append(f"({_sql_str(aid)}, 1, {_sql_str(g['text'])})")

    debts = _collect_debts(all_agents, id_set)
    debt_rows = [
        "(" + ", ".join([
            _sql_str(d["a_id"]), _sql_str(d["b_id"]), str(d["amount_cents"]),
            _sql_str((ANCHOR_SIM + timedelta(days=d["due_days"])).isoformat()), "0",
        ]) + ")"
        for d in debts
    ]

    stocks = {s["id"]: s["initial_price_cents"] for s in world["stocks"]["symbols"]}
    world_rows = [
        f"('clock.anchor', {_sql_json({'anchor_sim': ANCHOR_SIM.isoformat(), 'anchor_wall': ANCHOR_SIM.isoformat(), 'ratio': 1})}, 0)",
        f"('economy.stocks', {_sql_json(stocks)}, 0)",
        f"('economy.salary', {_sql_json(salaries)}, 0)",
    ]

    head = [
        "-- 生成物勿手改（scripts/seed.py 从 config/agents.yaml + config/world.yaml 确定性生成；同输入同输出）",
        f"-- 再生成：cd server && uv run python scripts/seed.py --ids {ids[0]}..{ids[-1]} --out ddl/seed_{len(ids)}.sql",
        "-- 口径：01 §1.5（余额分层/工资三档）· 01 §2（人设/拓扑）· 01 §3.1（六需求 60~80）· 01 §3.3（目标）· 04 §3.2（锚点值结构）· 06 §3（还款期限）",
        "BEGIN;",
        "",
        "-- agents（persona = 人设卡全量；needs = 60~80 初值 + needs_offset；cognition_tier 见文件头注释）",
        "INSERT INTO agents (id, name, gender, age, room_no, department, job_title, cognition_tier, persona, needs, balance_cents, position) VALUES",
        ",\n".join(agent_rows) + ";",
        "",
        "-- relations（relations_initial 逐条落有序对；labels ← type 数组化、one_line ← note）",
        "INSERT INTO relations (a_id, b_id, affinity, tension, labels, one_line) VALUES",
        ",\n".join(relation_rows) + ";",
        "",
        "-- debts（a_id 债权人 → b_id 债务人，round2 §A.18；due_sim = 锚点 + N 模拟日）",
        "INSERT INTO debts (a_id, b_id, amount_cents, due_sim, created_tick) VALUES",
        ",\n".join(debt_rows) + ";" if debt_rows else "-- （无选中集内初始债务）",
        "",
        "-- goals（sim_week=1, status='active'；明星层 3 条/人，01 §3.3）",
        "INSERT INTO goals (agent_id, sim_week, goal) VALUES",
        ",\n".join(goal_rows) + ";",
        "",
        "-- world_state（键名 01 文档 §6 D4：时钟锚点 / 股价 / 每人月薪抽定）",
        "INSERT INTO world_state (key, value, updated_tick) VALUES",
        ",\n".join(world_rows) + ";",
        "",
        "COMMIT;",
        f"-- initial_fund_cents: {fund_total}（04 §10.1 ① :initial_fund 数据源，01 文档 §5 R5）",
    ]
    return "\n".join(head) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="WorldSim seed 生成器（T-DB-04/05，确定性）")
    ap.add_argument("--ids", required=True, help="A01..A08 / A01..A40")
    ap.add_argument("--out", required=True, help="输出 SQL 路径（相对 server/ 或绝对）")
    args = ap.parse_args()
    ids = _parse_ids(args.ids)
    sql = build_sql(ids)
    out = Path(args.out)
    if not out.is_absolute():
        out = SERVER_ROOT / out
    out.write_text(sql, encoding="utf-8")
    print(f"written {out}（agents={len(ids)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
