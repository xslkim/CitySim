"""compute_ripple_edge（07 T-SYN-07；05 §3.7/§4.2）：gossip 事件 → ripple_edge 传播边。

- `payload.cites[]`（裸 seq 数字字符串，`::bigint` 转换）→ memory_projection 查被引用记忆
  `source_event_seq`；**无 source_event_seq（纯反思/摘要/计划记忆）的 cites 条目跳过、
  不进 ripple_edge**（05 §3.7 P2-9 裁决）。
- 上一手非 gossip → root=该事件, hop=1；是 gossip → 取其全部已有 root、hop+1（>4 弃链，
  01 §4.1）→ 插 (root, dst, src, hop, teller, listener) 行；UNIQUE(root,dst) 幂等（多根多行）。
- distortion = normalized_levenshtein(dst 事件 payload.text_display, 被引用记忆 content_display)
  （05 §3.7/06 §2；只落本表，不回写 events；payload.distortion 键废弃）。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .levenshtein import normalized_levenshtein

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
HOP_MAX = 4  # 01 §4.1 涟漪深度上限（跨文档统一口径）


async def compute(conn: Any, event_seq: int) -> int:
    """计算一条 gossip 的入边（每链根一行）；返回插入行数（幂等重放为 0）。"""
    ev = await conn.fetchrow(
        "SELECT seq, sim_time, payload FROM events WHERE seq=$1 AND type='dialogue.gossip'",
        event_seq)
    if ev is None:
        return 0
    payload = ev["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    dst_seq = int(ev["seq"])
    sim_time = ev["sim_time"]
    sim_day = sim_time.astimezone(LOCAL_TZ).date()
    teller = str(payload.get("teller") or "")
    listener = str(payload.get("listener") or "")
    dst_text = payload.get("text_display") or ""

    inserted = 0
    for cite in payload.get("cites") or []:
        mem = await conn.fetchrow(
            "SELECT source_event_seq, content_display FROM memory_projection WHERE memory_id=$1",
            int(str(cite)))
        if mem is None or mem["source_event_seq"] is None:
            continue  # P2-9：无源 cites 条目跳过
        src_seq = int(mem["source_event_seq"])
        distortion = normalized_levenshtein(dst_text, mem["content_display"])
        src_type = await conn.fetchval("SELECT type FROM events WHERE seq=$1", src_seq)
        if src_type != "dialogue.gossip":
            roots = [(src_seq, 1)]  # 非 gossip 源 → root=源事件, hop=1
        else:
            roots = [(int(r["root_event_seq"]), int(r["hop"]) + 1) for r in await conn.fetch(
                "SELECT root_event_seq, hop FROM ripple_edge WHERE dst_event_seq=$1", src_seq)]
        for root, hop in roots:
            if hop > HOP_MAX:
                continue  # 弃链（01 §4.1）
            n = await conn.execute(
                """
                INSERT INTO ripple_edge (root_event_seq, dst_event_seq, src_event_seq, hop,
                                         teller_id, listener_id, distortion, sim_time, sim_day)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (root_event_seq, dst_event_seq) DO NOTHING
                """,
                root, dst_seq, src_seq, hop, teller, listener, distortion, sim_time, sim_day)
            inserted += int(n.split()[-1])
    return inserted
