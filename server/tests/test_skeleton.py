"""T-ENV-04 骨架验收：包布局 + Python 3.12 + 命名约定。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PACKAGE_DIRS = [
    "time_engine",
    "scheduler",
    "adjudicator",
    "world_agent",
    "world_agent/director",
    "memory",
    "relations",
    "invite",
    "llm_gateway",
    "audit",
    "sync",
    "safety",
    "observe",
    "ingest",
    "snapshot",  # 00 §2 布局（T-ADJ-09 快照，评审 R1 增补）
]


def test_package_layout() -> None:
    root = Path(__file__).resolve().parents[1]
    pkg = root / "worldsim"
    assert (pkg / "main.py").is_file()
    for rel in PACKAGE_DIRS:
        d = pkg / rel
        assert d.is_dir(), f"missing package dir: worldsim/{rel}"
        assert (d / "__init__.py").is_file(), f"missing __init__.py: worldsim/{rel}"


def test_python_312_taskgroup() -> None:
    assert sys.version_info[:2] == (3, 12)
    assert hasattr(asyncio, "TaskGroup")


def test_main_stub_importable() -> None:
    import worldsim.adjudicator  # noqa: F401
    import worldsim.llm_gateway  # noqa: F401
    import worldsim.main

    assert callable(worldsim.main.main)
