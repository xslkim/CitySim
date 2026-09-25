"""T-ART-01 stream 页取色合法性检查：stream/ 内一切 6 位 HEX ⊆ allowed_hex.txt（02 §2.3∪§4.3∪§7.1）。

用法：`cd server && uv run python scripts/check_stream_hex.py`
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
ALLOWED = REPO_ROOT / "ui" / "assets" / "palette" / "allowed_hex.txt"
STREAM_DIR = REPO_ROOT / "stream"
HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")


def load_allowed() -> set[str]:
    out = set()
    for ln in ALLOWED.read_text(encoding="utf-8").splitlines():
        toks = ln.split()
        if toks and HEX_RE.fullmatch(toks[0]):
            out.add(toks[0].upper())
    return out


def main() -> int:
    allowed = load_allowed()
    found: dict[str, list[str]] = {}
    for p in sorted(STREAM_DIR.rglob("*")):
        # tests/ 内 HEX 是纯逻辑断言值（非页面呈现色），不计入（06 T-ART-01 口径 = "stream 页"取色）
        if p.is_file() and "tests" not in p.relative_to(STREAM_DIR).parts:
            for m in HEX_RE.findall(p.read_text(encoding="utf-8")):
                found.setdefault(m.upper(), []).append(str(p.relative_to(REPO_ROOT)))
    bad = {h: ps for h, ps in found.items() if h not in allowed}
    print(f"stream/ HEX 全集 {len(found)} 色；allowed {len(allowed)} 色")
    if bad:
        for h, ps in sorted(bad.items()):
            print(f"FAIL: {h} 不在 allowed_hex.txt（{sorted(set(ps))}）")
        return 1
    print("OK: stream/ 全部 HEX ⊆ allowed_hex.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
