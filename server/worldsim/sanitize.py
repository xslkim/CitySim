"""观众可见文本出站清洗（R1 迭代包 #3；问题 C+D）：工程黑话黑名单 + 代码/JSON 残块拦截 + 人话兜底。

- 命中黑名单或形似截断代码/JSON 输出 → 返回 None（调用方换 `FALLBACK_*` 人话兜底句），
  **不改写原文**：原始输出仍留本机调试通道（events.payload.text_raw / memories.content），
  "text_raw 不出站"红线（00 §4 红线 7）不破——本层只作用于展示字段。
- 唯一实现：obs-api 出站（serde/rest_agents/rest_relations/rest_snapshot）与内核生成侧
  （reflection 解析兜底、降级 think 留痕）共用，不另写第二份。
"""

from __future__ import annotations

import re

# 工程黑话黑名单（迭代包 #3 验收词表）：出站式样 = 子串命中即整段不可展示
JARGON_SUBSTRINGS: tuple[str, ...] = (
    "§", "LLM", "llm", "降级", "topic_hint", "tick", "sim_cost",
    "解析失败", "输出格式", "残块",
)

# 截断 JSON 的键值对形态（`"key":`），用于拦截未带围栏的裸残块
_JSON_KV = re.compile(r'"[A-Za-z_][\w]*"\s*:')
_JSON_WORD = re.compile(r"\bjson\b", re.IGNORECASE)


def looks_like_broken_output(text: str) -> bool:
    """形似 LLM 原始/截断结构化输出（```围栏、裸 {/[ 开头、JSON 键值对残块）。"""
    t = text.strip()
    if "```" in t or t.startswith(("{", "[")):
        return True
    return bool(_JSON_KV.search(t))


def sanitize_display_text(text: object) -> str | None:
    """展示文本出站清洗：可展示 → 去空白原文；不可展示（黑话/残块）→ None（调用方换兜底句）。"""
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    if looks_like_broken_output(t):
        return None
    if _JSON_WORD.search(t):
        return None
    if any(w in t for w in JARGON_SUBSTRINGS):
        return None
    return t


# 人话兜底句（按展示语境选用；不含任何黑名单词）
FALLBACK_REFLECTION = "刚才走神了，没留下什么记录。"
FALLBACK_GENERIC = "这段内容没能完整记录下来。"
FALLBACK_LINE = "（这句没听清）"


def display_or_fallback(text: object, fallback: str = FALLBACK_GENERIC) -> str:
    """sanitize + 兜底一体化：可展示原文，否则人话兜底句。"""
    return sanitize_display_text(text) or fallback
