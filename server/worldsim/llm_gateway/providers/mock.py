"""mock provider（03 文档 T-LLM-02，M1 段交付；00 §5 M1 / 04 §5.3）。

不联网、可复现的开发期工程替身（mock 是工程替身非设计语义，04 §5.3）：
- 输出 = 确定性函数 `f(seed, prompt)`：digest = SHA-256(task_type|seed|prompt) 驱动 `random.Random`，
  同一 (seed, prompt) 输出逐字节相同；异 seed 输出不同（digest 混入全部文本字段）。
- `seed` = 内核本 tick 规则骰子（04 §5.2 events.rng_seed 语义），由裁决管道经网关传入。
- 每类 task_type 返回合法 JSON（结构对齐 T-LLM-10 schema 草案；schema 落地后其校验自动激活）。
- `embed` 返回 seed 派生的 1024 维单位向量（维度口径 04 §5.2 vector(1024)）。
- token 数 = 字符数折算（CJK≈1 token/字符，仅 M1 联调用，不代表真实计量）。

prompt 契约（mock 自知之明）：裁决管道在 user 消息中放置一行 `OBS_JSON={...}`，
mock 从中取 `exits`（move 候选）/`participants`（对话发言序）/`agent_id` 做确定性决策；
无该行时退化为纯 think。真实 provider 无此约定（M2 模板渲染归 T-LLM-09）。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from typing import Any

from .base import EMBED_DIM, ChatResult, EmbedResult, Message

_OBS_RE = re.compile(r"^OBS_JSON=(\{.*\})\s*$", re.MULTILINE)

_THINK_TOPICS = ["晚饭吃什么", "周末去哪走走", "这个月的账", "工作群的未读", "楼下的猫", "昨晚那个梦"]
_MOODS = ["平稳", "有点累", "还不错", "略烦躁", "小开心"]


def _digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _rng_for(task_type: str, seed: int | None, prompt_text: str) -> random.Random:
    return random.Random(int(_digest(task_type, seed or 0, prompt_text)[:16], 16))


def _prompt_text(messages: list[Message]) -> str:
    # 注意：OBS_JSON 契约行必须保持行首锚定，本函数不得给 content 加任何行前缀
    return "\n".join(m.get("content", "") for m in messages)


def _parse_obs(prompt_text: str) -> dict[str, Any]:
    m = _OBS_RE.search(prompt_text)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return {}


def _est_tokens(text: str) -> int:
    return max(1, len(text))  # 字符数折算（03 T-LLM-02 实现要点）


class MockProvider:
    """seed 确定性 mock provider；同时实现 chat/embed 两协议。"""

    name = "mock"
    chat_model = "mock-chat"
    embed_model = "mock-embed"

    def __init__(self, *, embed_dim: int = EMBED_DIM) -> None:
        self._dim = embed_dim

    async def chat(
        self,
        task_type: str,
        messages: list[Message],
        gen_params: dict[str, Any] | None = None,
        *,
        seed: int | None = None,
    ) -> ChatResult:
        started = time.perf_counter()
        prompt = _prompt_text(messages)
        rng = _rng_for(task_type, seed, prompt)
        obs = _parse_obs(prompt)
        body = self._render(task_type, rng, obs)
        text = json.dumps(body, ensure_ascii=False, sort_keys=True)
        return ChatResult(
            text=text,
            prompt_tokens=_est_tokens(prompt),
            completion_tokens=_est_tokens(text),
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_id=f"mock-{_digest(task_type, seed or 0, prompt)[:16]}",
            provider=self.name,
            model=self.chat_model,
        )

    async def embed(self, texts: list[str], *, seed: int | None = None) -> EmbedResult:
        started = time.perf_counter()
        vectors = [self._unit_vector(seed, t) for t in texts]
        return EmbedResult(
            vectors=vectors,
            prompt_tokens=sum(_est_tokens(t) for t in texts),
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_id=f"mock-{_digest('embed', seed or 0, *texts)[:16]}",
            provider=self.name,
            model=self.embed_model,
            dimensions=self._dim,
        )

    # ---- 内部 -----------------------------------------------------------

    def _render(self, task_type: str, rng: random.Random, obs: dict[str, Any]) -> dict[str, Any]:
        tag = rng.randrange(16**6)  # 异 seed → 异文本的确定性标记
        if task_type in ("star_decision", "secondary"):
            return self._render_decision(rng, obs, tag, lite=(task_type == "secondary"))
        if task_type == "dialogue":
            return self._render_dialogue(rng, obs, tag)
        if task_type == "bgsummary":
            return {
                "diary": f"今天平平淡淡过了一天（{tag:06x}）。",
                "needs_delta": {},
                "mood": rng.choice(_MOODS),
                "tomorrow_plan": f"明天照常上班（{tag:06x}）",
            }
        if task_type == "reflection":
            return {"insights": [f"洞察{tag:06x}-{i}" for i in range(rng.randint(2, 3))]}
        if task_type == "director":
            return {"note": f"导演笔记（{tag:06x}）", "actions": []}
        if task_type == "world_copy":
            return {"title": f"公告{tag:06x}", "body": f"公告正文（{tag:06x}）"}
        if task_type == "safety":
            return {"label": "pass"}
        return {"text": f"mock-{tag:06x}"}

    def _render_decision(self, rng: random.Random, obs: dict[str, Any], tag: int, *, lite: bool) -> dict[str, Any]:
        exits = [e for e in obs.get("exits", []) if isinstance(e, str) and e != obs.get("position")]
        go_move = bool(exits) and rng.random() < 0.25
        if go_move:
            action: dict[str, Any] = {"type": "move", "args": {"to": rng.choice(exits)}}
            intent = f"换个地方待会儿（{tag:06x}）"
        else:
            action = {"type": "think", "args": {"topic_hint": rng.choice(_THINK_TOPICS)}}
            intent = f"想了想：{action['args']['topic_hint']}（{tag:06x}）"
        body: dict[str, Any] = {
            "intent": intent[:30] if lite else intent,
            "action": action,
            "emotion_delta": {},
        }
        if lite:
            body["gossip"] = None
        else:
            body["say"] = None
        return body

    def _render_dialogue(self, rng: random.Random, obs: dict[str, Any], tag: int) -> dict[str, Any]:
        speakers = [p for p in obs.get("participants", []) if isinstance(p, str)] or ["A", "B"]
        turns = rng.randint(6, 8)
        lines = [
            {"speaker": speakers[i % len(speakers)], "text": f"第{i + 1}句：{tag:06x}"}
            for i in range(turns)
        ]
        band = rng.choice(["愉快", "平淡", "敷衍"])
        return {
            "lines": lines,
            "opening_fact": f"开场由头（{tag:06x}）",
            "self_eval": {"band": band, "score": rng.randint(1, 5)},
            "quotable_lines": [lines[0]["text"]],
        }

    def _unit_vector(self, seed: int | None, text: str) -> list[float]:
        rng = random.Random(int(_digest("embed", seed or 0, text)[:16], 16))
        vec = [rng.gauss(0.0, 1.0) for _ in range(self._dim)]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
