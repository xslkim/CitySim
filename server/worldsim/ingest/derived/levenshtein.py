"""normalized_levenshtein（07 T-SYN-07；05 §3.7 失真度 V1 口径：归一化 0~1，分母 max(len)）。

纯 Python O(n×m) 实现，不引新依赖（07 D7；R4 实测超时再评估 C 扩展，须过 00 §3 钉版）。
只消费 `content_display`/`text_display`（无原文，05 §3.7 脱敏规则）。
"""

from __future__ import annotations


def levenshtein(a: str, b: str) -> int:
    """编辑距离（字符级 DP，两行滚动）。"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def normalized_levenshtein(a: str | None, b: str | None) -> float:
    """归一化失真度 = levenshtein / max(len)；两空 → 0.0（05 §3.7；05 文档 D3 分母 max(len)）。"""
    a, b = a or "", b or ""
    m = max(len(a), len(b))
    if m == 0:
        return 0.0
    return round(levenshtein(a, b) / m, 3)
