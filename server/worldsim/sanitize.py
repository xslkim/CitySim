"""观众可见文本出站清洗（R1 迭代包 #3；问题 C+D）：工程黑话黑名单 + 代码/JSON 残块拦截 + 人话兜底。

- 命中黑名单或形似截断代码/JSON 输出 → 返回 None（调用方换 `FALLBACK_*` 人话兜底句），
  **不改写原文**：原始输出仍留本机调试通道（events.payload.text_raw / memories.content），
  "text_raw 不出站"红线（00 §4 红线 7）不破——本层只作用于展示字段。
- 唯一实现：obs-api 出站（serde/rest_agents/rest_relations/rest_snapshot）与内核生成侧
  （reflection 解析兜底、降级 think 留痕）共用，不另写第二份。
- T-ITER2-04（round2 #4②）：两级判定——"JSON 形态但内容完好"先尝试结构化解包提取展示字段，
  提取成功（且过黑名单）则不出兜底句；解包失败才回退兜底句（修误杀，历史残块形态仍兜住）。
"""

from __future__ import annotations

import json
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

FALLBACK_SENTENCES: tuple[str, ...] = (FALLBACK_REFLECTION, FALLBACK_GENERIC, FALLBACK_LINE)


def is_fallback_text(text: object) -> bool:
    """展示文本是否为人话兜底句（消费侧过滤用：兜底事件不进 feed/字幕观众面，T-ITER2-04 ③）。"""
    return isinstance(text, str) and text.strip() in FALLBACK_SENTENCES


# 结构化解包的候选展示键（按优先级；answer 为透传包装层，展开后再取一次）
_STRUCT_KEYS = ("diary", "summary", "reflection", "thought", "text", "content")


def _structured_candidates(body: object) -> list[str]:
    """JSON 体 → 候选展示字符串列表（dict 按候选键 / answer 包装 / insights 数组；list 字符串拼接）。"""
    out: list[str] = []
    if isinstance(body, dict):
        for k in _STRUCT_KEYS:
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v)
        if not out:  # 顶层无直取键 → 展开 answer 包装层 / insights 数组
            ans = body.get("answer")
            if isinstance(ans, str) and ans.strip():
                out.append(ans)
            elif isinstance(ans, dict):
                out.extend(_structured_candidates(ans))
            if not out:
                ins = body.get("insights")
                if isinstance(ins, list):
                    parts = [x for x in ins if isinstance(x, str) and x.strip()]
                    if parts:
                        out.append("；".join(parts[:3]))
    elif isinstance(body, list):
        parts = [x for x in body if isinstance(x, str) and x.strip()]
        if parts:
            out.append("；".join(parts[:3]))
    return out


def extract_structured_display(text: object) -> str | None:
    """T-ITER2-04②：JSON 形态但内容完好 → 提取展示字段（提取结果仍需过黑名单/残块判定）。

    覆盖历史误杀形态：`{"summary":…}` / `{"answer":{…}}` / `{"answer":"…"}` / `{"insights":[…]}`；
    非 JSON 或解包不出展示字段 → None（交兜底句）。
    """
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t.startswith(("{", "[")):
        return None
    try:
        body = json.loads(t)
    except (ValueError, TypeError):
        return None
    for cand in _structured_candidates(body):
        shown = sanitize_display_text(cand)
        if shown:
            return shown
    return None


def sanitize_or_extract(text: object) -> str | None:
    """两级判定：原文可展示 → 原文；JSON 形态内容完好 → 提取展示字段；否则 None（兜底句归调用方）。"""
    return sanitize_display_text(text) or extract_structured_display(text)


def display_or_fallback(text: object, fallback: str = FALLBACK_GENERIC) -> str:
    """sanitize + 兜底一体化：可展示原文，否则人话兜底句。"""
    return sanitize_or_extract(text) or fallback
