#!/usr/bin/env python
"""门禁①(a) 脱敏剧本导出（08 T-AUD-09；01 §10.1 步骤 1~3；00 §4 红线 7）。

- 取样：`--from/--to <YYYY-MM-DD>` 连续模拟日事件流，须含 ≥3 个 A 级事件（有效 grade =
  ui.grade ⊕ 最新 director.grade_revise）；不足则顺延取样窗（每次 +1 日，上限 14 日）。
- 脱敏：角色名 → 化名（甲/乙/丙…，顺序稳定映射）；剥除一切数值字段（affinity/需求值）、
  系统字段（tick/trigger/arc_id）与调试信息；只保留时间戳（"周五晚 7 点"形态）、地点、
  动作叙述、对话原文（仅 `visibility='public'` 的 `text_display`/`lines[].text_display`）。
- 排版：按场景（地点 × 连续时段）切分剧本文体（场景标题 + 动作描述 + 对白），每段 ≤300 字。
- 只读不写库。用法（cwd=server/）：
  `uv run python scripts/export_script.py --from 2026-10-12 --to 2026-10-14 [--out out.txt]`
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

import yaml  # noqa: E402


def load_location_names() -> dict[str, str]:
    """location id 到展示名映射（config/world.yaml；脱敏剧本"地点"字段口径，工程默认映射）。"""
    with (SERVER_ROOT / "config" / "world.yaml").open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    out: dict[str, str] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            nid, name = node.get("id"), node.get("name")
            if isinstance(nid, str) and isinstance(name, str) and "." in nid:
                out[nid] = name
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(cfg)
    return out

PSEUDONYMS = list("甲乙丙丁戊己庚辛壬癸") + [f"角色{i}" for i in range(11, 41)]
SEGMENT_CHAR_LIMIT = 300  # 01 §10.1 步骤 3
MIN_A_LEVEL = 3           # 01 §10.1 步骤 1
WINDOW_EXTEND_MAX = 14    # 顺延上限（工程默认）

# 动作叙述模板（人话，工程默认；无工程词汇/数值）
ACTION_TEMPLATES = {
    "agent.move": "来到了{location}",
    "agent.work": "处理工作任务",
    "agent.rest": "休息了一会儿",
    "agent.eat": "去吃了点东西",
    "agent.shop": "去买了些东西",
    "world.announce": "【公告】{text}",
    "world.holiday": "【假期】{text}",
    "dialogue.argue": "发生了争执",
    "dialogue.gossip": "说起了八卦",
    "dialogue.confess": "鼓起勇气表白",
    "dialogue.apologize": "主动道歉",
    "agent.reflection": "（独白）",
}

WEEKDAYS = "一二三四五六日"


def fmt_time(t: dt.datetime) -> str:
    """"周五晚 7 点"形态（01 §10.1 步骤 2）。"""
    wd = WEEKDAYS[t.weekday()]
    h = t.hour
    part = ("凌晨" if h < 6 else "早上" if h < 9 else "上午" if h < 12
            else "中午" if h < 14 else "下午" if h < 18 else "晚上" if h < 23 else "深夜")
    h12 = h % 12 or 12
    return f"周{wd}{part} {h12} 点"


class Anonymizer:
    """agent id/真名 → 化名（顺序稳定）。"""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._names: dict[str, str] = {}

    def register(self, agent_id: str, name: str) -> None:
        if agent_id not in self._map:
            self._map[agent_id] = PSEUDONYMS[len(self._map) % len(PSEUDONYMS)]
        self._names[name] = self._map[agent_id]

    def name_of(self, agent_id: str) -> str:
        return self._map.get(agent_id, "路人")

    def scrub(self, text: str) -> str:
        """真名替换（长名优先防前缀吞并）。"""
        for real in sorted(self._names, key=len, reverse=True):
            text = text.replace(real, self._names[real])
        return text


async def _effective_a_seqs(pool, from_day: dt.date, to_day: dt.date) -> set[int]:
    """有效 grade='A' 的事件 seq 集合（ui.grade ⊕ 最新 grade_revise，04 §6.6）。"""
    rows = await pool.fetch(
        """
        SELECT e.seq FROM events e
        WHERE (e.sim_time AT TIME ZONE 'Asia/Shanghai')::date BETWEEN $1 AND $2
          AND coalesce((SELECT r.payload->>'new_grade' FROM events r
                        WHERE r.type='director.grade_revise'
                          AND r.payload->>'target_seq' = e.seq::text
                        ORDER BY r.seq DESC LIMIT 1), e.ui->>'grade') = 'A'
        """, from_day, to_day)
    return {int(r["seq"]) for r in rows}


async def _load_window(pool, from_day: dt.date, to_day: dt.date) -> tuple[list[dict], set[int]]:
    rows = await pool.fetch(
        """
        SELECT seq, sim_time, type, location_id, actors, payload, visibility FROM events
        WHERE (sim_time AT TIME ZONE 'Asia/Shanghai')::date BETWEEN $1 AND $2
        ORDER BY seq
        """, from_day, to_day)
    return [dict(r) for r in rows], await _effective_a_seqs(pool, from_day, to_day)


def _event_lines(ev: dict, anon: Anonymizer, loc_names: dict[str, str]) -> list[str]:
    """单事件 → 剧本行（动作叙述 + 对白）；internal 事件零文本（红线 7）。"""
    payload = ev["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    lines: list[str] = []
    actors = [anon.name_of(a) for a in (ev.get("actors") or [])]
    who = "、".join(actors) if actors else ""
    loc = loc_names.get(str(ev.get("location_id") or ""), str(ev.get("location_id") or ""))
    if ev["visibility"] != "public":
        # internal 事件只出动作骨架（无展示文本；01 §10.1 只保留动作叙述）
        tpl = ACTION_TEMPLATES.get(ev["type"])
        if tpl and "{text}" not in tpl:
            lines.append(f"{who}{tpl.format(location=loc)}")
        return lines
    tpl = ACTION_TEMPLATES.get(ev["type"], None)
    text = payload.get("text_display")
    if tpl:
        lines.append(f"{who}{tpl.format(location=loc, text=text or '')}")
    elif text:
        lines.append(f"{who}：{text}" if who else str(text))
    for ln in payload.get("lines") or []:
        t = ln.get("text_display") or ln.get("text")
        if t:
            speaker = anon.name_of(str(ln.get("speaker") or ""))
            lines.append(f"{speaker}：「{t}」")
    return [anon.scrub(l) for l in lines]


def render_script(events: list[dict], anon: Anonymizer,
                  loc_names: dict[str, str] | None = None) -> str:
    """剧本文体排版：场景（地点+日+半日段）切分；每段 ≤300 字。"""
    loc_names = loc_names or {}
    scenes: list[tuple[str, list[str]]] = []
    cur_key = None
    for ev in events:
        t = ev["sim_time"].astimezone(LOCAL_TZ)
        loc = loc_names.get(str(ev.get("location_id") or ""), str(ev.get("location_id") or "楼里"))
        key = (t.date(), "午前" if t.hour < 14 else "午后", loc)
        if key != cur_key:
            scenes.append((f"【{fmt_time(t)} · {key[2]}】", []))
            cur_key = key
        scenes[-1][1].extend(_event_lines(ev, anon, loc_names))
    out: list[str] = []
    for title, lines in scenes:
        if not lines:
            continue  # 空场景（internal 无模板事件）不落段
        out.append(title)
        seg = ""
        for line in lines:
            if len(seg) + len(line) + 1 > SEGMENT_CHAR_LIMIT and seg:
                out.append(seg)
                out.append("")   # 段落间空行（剧本文体分段；每段 ≤300 字）
                seg = ""
            seg = f"{seg}\n{line}" if seg else line
        if seg:
            out.append(seg)
        out.append("")
    return "\n".join(out).strip() + "\n"


async def main_async(args: argparse.Namespace) -> int:
    dsn = args.dsn or os.environ.get("WSIM_PG_DSN")
    if not dsn:
        print("缺 DSN（--dsn 或 WSIM_PG_DSN）", file=sys.stderr)
        return 2
    pool = await asyncpg.connect(dsn)
    try:
        anon = Anonymizer()
        for r in await pool.fetch("SELECT id, name FROM agents ORDER BY id"):
            anon.register(r["id"], r["name"])

        from_day = dt.date.fromisoformat(args.from_)
        to_day = dt.date.fromisoformat(args.to)
        # 步骤 1：A 级 ≥3，不足顺延（每次 +1 日）
        while True:
            events, a_seqs = await _load_window(pool, from_day, to_day)
            if len(a_seqs) >= MIN_A_LEVEL or (to_day - from_day).days >= WINDOW_EXTEND_MAX:
                break
            to_day += dt.timedelta(days=1)
        if len(a_seqs) < MIN_A_LEVEL:
            print(f"WARN: 顺延至 {to_day} 仍仅 {len(a_seqs)} 个 A 级事件（{MIN_A_LEVEL} 需要）",
                  file=sys.stderr)
        script = render_script(events, anon, load_location_names())
        out = Path(args.out) if args.out else None
        if out:
            out.write_text(script, encoding="utf-8")
        else:
            sys.stdout.write(script)
        print(f"取样窗 {from_day}~{to_day}（A 级 {len(a_seqs)} 个）；段落 "
              f"{sum(1 for line in script.splitlines() if line.startswith('【'))} 场景",
              file=sys.stderr)
        return 0
    finally:
        await pool.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
