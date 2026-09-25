"""层 1 本地规则（03 T-LLM-11；04 §11.1 L2 语义前置短路，03 §6 D2 层序适配）。

敏感词表（`config/safety/wordlist.txt`）+ 正则，毫秒级短路，四类覆盖：政治/色情/暴恐/未成年人。
只判标签不改写：`check(text)` 命中 → "block"（规则层不做软化档，soft 由层 2 GLM 审核产出）。
"""

from __future__ import annotations

import re
from pathlib import Path

WORDLIST_PATH = Path(__file__).resolve().parents[2] / "config" / "safety" / "wordlist.txt"

# 正则层（词表兜不住的形态；保持最小集，误杀观察归 T-LLM-12 日报）
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"炸[弹藥药].{0,4}(制|做|配方)"),
    re.compile(r"(幼|未成年).{0,6}(色情|援交|裸)"),
)


class LocalRules:
    """词表 + 正则短路判定。`hit(text) -> 命中词/规则描述 | None`。"""

    def __init__(self, words: tuple[str, ...]) -> None:
        self._words = words

    @classmethod
    def load(cls, path: str | Path | None = None) -> LocalRules:
        p = Path(path) if path else WORDLIST_PATH
        words = tuple(
            line.strip()
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )
        return cls(words)

    def hit(self, text: str) -> str | None:
        for w in self._words:
            if w in text:
                return f"word:{w}"
        for pat in _PATTERNS:
            m = pat.search(text)
            if m:
                return f"regex:{m.group(0)}"
        return None

    def label(self, text: str) -> str:
        return "block" if self.hit(text) else "pass"
