#!/usr/bin/env python
"""门禁①(b) 采访评估记忆查询（08 T-AUD-09；01 §10.2 十六题数据源证据包）。

- `--agent <id>` 输出采访答题证据包，覆盖 16 题题面所需数据源：|affinity| Top-3 关系边（q1）、
  记忆流检索（按 kind/source_event_seq，gossip 链沿 caused_by 回溯，q2/q7/q8/q16）、当前目标（q3）、
  负事件记忆（q4）、经济结算事件（q5）、tension 边（q6）、债务边（q10）、工作任务（q11）、
  绩效/经理评价（q12）、日程规则与偏差（q13）、secret 边界（q14，只证存在不泄内容）、
  空间认知（q15）。
- 输出 JSON（机读，stdout/--out）+ Markdown（人读，`--md`）；打分表骨架按 01 §10.2 字段
  逐字落 `var/logs/interview/<date>.json`（`{q_id, agent_id, answer_summary, evidence_event_ids,
  score, note}`；score 由人工填，0 分题必须附 evidence_event_ids 反证——脚本预填证据 id）。
- 只读不写库（打分表落盘为文件，非 DB 写）。用法（cwd=server/）：
  `uv run python scripts/interview_query.py --agent A01 [--md]`
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

import asyncpg

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from worldsim.time_engine.clock import LOCAL_TZ  # noqa: E402

OUT_DIR = SERVER_ROOT.parent / "var" / "logs" / "interview"


def _j(v):
    return json.loads(v) if isinstance(v, str) else v


async def gather_evidence(pool, agent_id: str) -> dict:
    """16 题证据包（键 = q1..q16，值 = {data, evidence_event_ids}）。"""
    ev: dict[str, dict] = {}

    def put(q: str, data, seqs: list[int]) -> None:
        ev[q] = {"data": data, "evidence_event_ids": [int(s) for s in seqs if s is not None]}

    # q1：|affinity| Top-3 关系边
    rows = await pool.fetch(
        """
        SELECT a_id, b_id, affinity, tension, labels, one_line, last_event_seq FROM relations
        WHERE a_id=$1 OR b_id=$1 ORDER BY abs(affinity) DESC LIMIT 3
        """, agent_id)
    put("q1", [dict(r) for r in rows], [r["last_event_seq"] for r in rows])

    # q2：关系记忆（与 Top-1 边对方相关的投影记忆）
    if rows:
        other = rows[0]["b_id"] if rows[0]["a_id"] == agent_id else rows[0]["a_id"]
        mems = await pool.fetch(
            """
            SELECT m.id, m.kind, m.sim_time, m.content_display, m.source_event_seq
            FROM memories m WHERE m.agent_id=$1 AND m.content_display IS NOT NULL
              AND (m.content_display LIKE '%' || (SELECT name FROM agents WHERE id=$2) || '%'
                   OR m.source_event_seq IN (SELECT seq FROM events WHERE $2 = ANY(actors)))
            ORDER BY m.sim_time DESC LIMIT 5
            """, agent_id, other)
        put("q2", {"about": other, "memories": [dict(m) for m in mems]},
            [m["source_event_seq"] for m in mems])
    else:
        put("q2", None, [])

    # q3：当前目标
    goals = await pool.fetch(
        "SELECT id, goal, blocked_count, frustration, status FROM goals"
        " WHERE agent_id=$1 AND status='active' ORDER BY id", agent_id)
    put("q3", [dict(g) for g in goals], [])

    # q4：负事件记忆（argue/stood_up/refuse 涉及本人，按日聚类取最密集日）
    neg = await pool.fetch(
        """
        SELECT seq, type, sim_time, payload FROM events
        WHERE $1 = ANY(actors) AND type IN ('dialogue.argue','social.appointment.stood_up',
                                            'social.refuse')
        ORDER BY sim_time DESC LIMIT 10
        """, agent_id)
    put("q4", [{"seq": int(r["seq"]), "type": r["type"],
                "sim_time": r["sim_time"].isoformat()} for r in neg],
        [r["seq"] for r in neg])

    # q5：经济结算事件（本人参与、含 amount_cents）
    eco = await pool.fetch(
        """
        SELECT seq, type, sim_time, payload FROM events
        WHERE $1 = ANY(actors) AND payload ? 'amount_cents' ORDER BY sim_time DESC LIMIT 10
        """, agent_id)
    put("q5", [{"seq": int(r["seq"]), "type": r["type"],
                "amount_cents": _j(r["payload"]).get("amount_cents")} for r in eco],
        [r["seq"] for r in eco])

    # q6：tension 边 Top-3 + 最近 argue
    tension = await pool.fetch(
        """
        SELECT a_id, b_id, tension, last_event_seq FROM relations
        WHERE (a_id=$1 OR b_id=$1) AND tension > 0 ORDER BY tension DESC LIMIT 3
        """, agent_id)
    argues = await pool.fetch(
        "SELECT seq, sim_time, payload FROM events WHERE type='dialogue.argue'"
        " AND $1 = ANY(actors) ORDER BY sim_time DESC LIMIT 3", agent_id)
    put("q6", {"tension_edges": [dict(r) for r in tension],
               "recent_argues": [{"seq": int(r["seq"]),
                                  "reason_hint": _j(r["payload"]).get("reason_hint")}
                                 for r in argues]},
        [r["seq"] for r in argues] + [r["last_event_seq"] for r in tension])

    # q7/q8：gossip 链（本人 teller/listener/about + cites 沿 source_event_seq 回溯）
    gossip = await pool.fetch(
        """
        SELECT seq, sim_time, payload FROM events
        WHERE type='dialogue.gossip' AND (payload->>'teller'=$1 OR payload->>'listener'=$1
                                          OR payload->>'about'=$1)
        ORDER BY sim_time DESC LIMIT 10
        """, agent_id)
    chains = []
    seqs: list[int] = []
    for g in gossip:
        p = _j(g["payload"])
        cites = [int(str(c)) for c in (p.get("cites") or [])]
        src = [r["source_event_seq"] for r in await pool.fetch(
            "SELECT source_event_seq FROM memories WHERE id = ANY($1::bigint[])", cites)] \
            if cites else []
        chains.append({"seq": int(g["seq"]), "teller": p.get("teller"),
                       "listener": p.get("listener"), "about": p.get("about"),
                       "cites": cites, "source_event_seqs": src,
                       "text_display": p.get("text_display")})
        seqs.append(g["seq"])
        seqs.extend(s for s in src if s)
    put("q7", chains, seqs)
    put("q8", chains, seqs)

    # q9：日常事实（最近就餐）
    eats = await pool.fetch(
        "SELECT seq, sim_time, payload, location_id FROM events WHERE type='agent.eat'"
        " AND $1 = ANY(actors) ORDER BY sim_time DESC LIMIT 3", agent_id)
    put("q9", [{"seq": int(r["seq"]), "sim_time": r["sim_time"].isoformat(),
                "venue": _j(r["payload"]).get("venue"),
                "with": _j(r["payload"]).get("with")} for r in eats],
        [r["seq"] for r in eats])

    # q10：债务边（a_id=债权人/b_id=债务人，round2 §A.18 口径）
    debts = await pool.fetch(
        "SELECT * FROM debts WHERE a_id=$1 OR b_id=$1", agent_id)
    put("q10", [{k: (v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else
                     _j(v) if isinstance(v, str) and v[:1] in "[{" else v)
                 for k, v in dict(d).items()} for d in debts], [])

    # q11/q12：工作任务与绩效（work 事件 + grade_revise 复核记录）
    works = await pool.fetch(
        "SELECT seq, sim_time, payload FROM events WHERE type='agent.work'"
        " AND $1 = ANY(actors) ORDER BY sim_time DESC LIMIT 10", agent_id)
    put("q11", [{"seq": int(r["seq"]), "task_id": _j(r["payload"]).get("task_id")}
                for r in works], [r["seq"] for r in works])
    reviews = await pool.fetch(
        """
        SELECT r.seq, r.sim_time, r.payload FROM events r
        WHERE r.type='director.grade_revise' AND r.payload->>'target_seq' IN
          (SELECT seq::text FROM events WHERE $1 = ANY(actors))
        ORDER BY r.seq DESC LIMIT 5
        """, agent_id)
    put("q12", [{"seq": int(r["seq"]), "target_seq": _j(r["payload"]).get("target_seq"),
                 "new_grade": _j(r["payload"]).get("new_grade"),
                 "reason": _j(r["payload"]).get("reason")} for r in reviews],
        [r["seq"] for r in reviews])

    # q13：日程规则与偏差（作息 + 周末事件实测）
    agent = await pool.fetchrow("SELECT id, name, room_no, position, persona FROM agents WHERE id=$1",
                                agent_id)
    persona = _j(agent["persona"]) if agent else {}
    weekend = await pool.fetch(
        """
        SELECT seq, type, sim_time, location_id FROM events
        WHERE $1 = ANY(actors) AND EXTRACT(ISODOW FROM sim_time) >= 6
        ORDER BY sim_time DESC LIMIT 10
        """, agent_id)
    put("q13", {"weekly_routine_bias": persona.get("weekly_routine_bias"),
                "weekend_events": [{"seq": int(r["seq"]), "type": r["type"],
                                    "sim_time": r["sim_time"].isoformat(),
                                    "location_id": r["location_id"]} for r in weekend]},
        [r["seq"] for r in weekend])

    # q14：secret 边界——只证存在性，绝不泄内容（01 §10.2 题意：不能泄露为"大家都知道"）
    has_secret = bool(persona.get("secret"))
    leak = await pool.fetch(
        """
        SELECT count(*) AS n FROM events
        WHERE visibility='public' AND $1::text = $1::text
          AND payload::text LIKE '%' || $2::text || '%'
        """, agent_id, str(persona.get("secret") or "\x00不可能命中\x00"))
    put("q14", {"has_secret": has_secret,
                "public_leak_events": int(leak[0]["n"]) if has_secret else 0}, [])

    # q15：空间认知（现居 + 移动史）
    moves = await pool.fetch(
        "SELECT seq, sim_time, payload FROM events WHERE type='agent.move'"
        " AND $1 = ANY(actors) ORDER BY sim_time DESC LIMIT 5", agent_id)
    put("q15", {"room_no": agent["room_no"] if agent else None,
                "position": agent["position"] if agent else None,
                "recent_moves": [{"seq": int(r["seq"]), "to": _j(r["payload"]).get("to")}
                                 for r in moves]},
        [r["seq"] for r in moves])

    # q16：反思通道（kind='reflection' 记忆 + agent.reflection 事件）
    refl = await pool.fetch(
        """
        SELECT id, sim_time, content_display, source_event_seq FROM memories
        WHERE agent_id=$1 AND kind='reflection' AND content_display IS NOT NULL
        ORDER BY sim_time DESC LIMIT 5
        """, agent_id)
    put("q16", [{"id": int(r["id"]), "sim_time": r["sim_time"].isoformat(),
                 "content_display": r["content_display"]} for r in refl],
        [r["source_event_seq"] for r in refl])

    return ev


def to_markdown(agent_id: str, ev: dict) -> str:
    lines = [f"# 采访证据包 · {agent_id}", ""]
    titles = {"q1": "|affinity| Top-3 关系边", "q2": "关系记忆", "q3": "当前目标",
              "q4": "负事件记忆", "q5": "经济结算", "q6": "tension 边与吵架",
              "q7": "gossip 传播链", "q8": "传闻采信", "q9": "日常事实（就餐）",
              "q10": "债务边", "q11": "工作任务", "q12": "绩效/复核记录",
              "q13": "日程规则与偏差", "q14": "secret 边界（只证存在）", "q15": "空间认知",
              "q16": "反思记录"}
    for q in sorted(ev, key=lambda x: int(x[1:])):
        e = ev[q]
        lines.append(f"## {q} {titles.get(q, '')}")
        lines.append(f"- 证据事件：{e['evidence_event_ids']}")
        lines.append("```json")
        lines.append(json.dumps(e["data"], ensure_ascii=False, default=str, indent=1)[:2000])
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    dsn = args.dsn or os.environ.get("WSIM_PG_DSN")
    if not dsn:
        print("缺 DSN（--dsn 或 WSIM_PG_DSN）", file=sys.stderr)
        return 2
    pool = await asyncpg.connect(dsn)
    try:
        ev = await gather_evidence(pool, args.agent)
    finally:
        await pool.close()
    pack = {"agent_id": args.agent, "generated_at": dt.datetime.now(LOCAL_TZ).isoformat(),
            "questions": ev}
    # 打分表骨架（01 §10.2 字段逐字；score/note 由人工填）
    sheet = [{"q_id": int(q[1:]), "agent_id": args.agent, "answer_summary": "",
              "evidence_event_ids": ev[q]["evidence_event_ids"], "score": None, "note": ""}
             for q in sorted(ev, key=lambda x: int(x[1:]))]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sheet_path = OUT_DIR / f"{dt.datetime.now(LOCAL_TZ).date().isoformat()}-{args.agent}.json"
    sheet_path.write_text(json.dumps(sheet, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.md:
        sys.stdout.write(to_markdown(args.agent, ev))
    else:
        sys.stdout.write(json.dumps(pack, ensure_ascii=False, default=str, indent=1) + "\n")
    print(f"打分表骨架 → {sheet_path}", file=sys.stderr)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--dsn", default=None)
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
