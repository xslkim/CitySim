"""WS `state_diff` 派生（05 T-WEB-07；03 §6.3 500ms 合帧、05 §6"state_diff obs-api 派生不落表"）。

从事件流派生轻量差分：`agent.move` → `location_id`；`state.needs_delta` 的 `changes[]` →
needs/mood（`new_value` 直写）；500ms 合帧 = 轮询批次内同 agent 多次变更只留最新（03 §6.3）。
"""

from __future__ import annotations

from typing import Any


def derive_state_diff(events: list[dict[str, Any]], tick: int) -> dict[str, Any] | None:
    """事件批 → state_diff data（无相关变更返回 None）。events 按 seq 升序输入。"""
    agents: dict[str, dict[str, Any]] = {}
    for ev in events:
        payload = ev.get("payload") or {}
        if ev["type"] == "agent.move":
            actor = (ev.get("_actors") or ev.get("actors") or [None])[0]
            if actor and payload.get("to"):
                agents.setdefault(actor, {})["location_id"] = payload["to"]
        elif ev["type"] == "state.needs_delta":
            for ch in payload.get("changes") or []:
                aid = ch.get("agent_id")
                if not aid or ch.get("new_value") is None:
                    continue
                cell = agents.setdefault(aid, {})
                if ch.get("need") == "mood":
                    cell["mood"] = ch["new_value"]
                else:
                    cell.setdefault("needs", {})[ch["need"]] = ch["new_value"]
    if not agents:
        return None
    return {"tick": tick, "agents": [{"id": aid, **fields} for aid, fields in sorted(agents.items())]}
