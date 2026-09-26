"""反思旧行 text_display 一次性清洗回填（R3 #7②；T-ITER2-04 两级判定同一实现）。

背景：round2 #4 之前落库的 `agent.reflection` 事件，payload.text_display 是 LLM 原始输出
（```json 围栏 / {"summary":…} / {"answer":…} 嵌套），观众面直接看到 raw JSON（主库 seq
151/230/235 等）。新链路已在写入侧清洗（sanitize.py），本脚本把存量旧行按同一判定回填：
可提取展示字段 → 提取回填；提取不出 → 人话兜底句（FALLBACK_REFLECTION）。

用法（对本机主库；只 UPDATE agent.reflection 行的 payload.text_display 键，不动其他列）：
    cd server && uv run python scripts/clean_reflections.py            # 预演（只打印）
    cd server && uv run python scripts/clean_reflections.py --apply    # 回填
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

import asyncpg

from worldsim.sanitize import FALLBACK_REFLECTION, extract_structured_display, sanitize_display_text

_RAW_JSON_RE = re.compile(r"^\s*(```|\{|\[)")


def _looks_raw(text: str) -> bool:
    t = str(text).strip()
    return bool(_RAW_JSON_RE.match(t))


def _clean(text: str) -> str:
    """旧行清洗：结构化提取 → 提取结果的展示清洗 → 兜底句（T-ITER2-04 两级判定同口径）。"""
    t = str(text).strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.strip()
    shown = sanitize_display_text(t) or extract_structured_display(t)
    return shown or FALLBACK_REFLECTION


async def main(*, apply: bool) -> int:
    import os

    dsn = os.environ.get("WSIM_PG_DSN", "")
    if not dsn:
        raise SystemExit("缺 WSIM_PG_DSN")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        rows = await pool.fetch(
            """
            SELECT seq, payload FROM events
            WHERE type = 'agent.reflection' AND payload ? 'text_display'
            ORDER BY seq
            """)
        dirty = [
            (int(r["seq"]), json.loads(r["payload"])["text_display"])
            for r in rows
            if _looks_raw(json.loads(r["payload"]).get("text_display") or "")
        ]
        print(f"agent.reflection 共 {len(rows)} 行，raw JSON 旧行 {len(dirty)} 行")
        fixed = 0
        for seq, old in dirty:
            new = _clean(old)
            if new != old:
                fixed += 1
                print(f"  seq={seq}: {str(old)[:60]!r} -> {new[:60]!r}")
                if apply:
                    await pool.execute(
                        """
                        UPDATE events SET payload = jsonb_set(
                            payload, '{text_display}', to_jsonb($2::text), false)
                        WHERE seq = $1
                        """,
                        seq, new)
        print(f"{'已回填' if apply else '预演（--apply 生效）'}：{fixed} 行")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
