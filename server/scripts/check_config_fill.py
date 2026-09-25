#!/usr/bin/env python3
"""models.yaml 填充检查（03 T-LLM-04 交付物，评审 R1 §C 增项）。

扫描 models.yaml：① YAML 可解析；②四段键名齐全（01 T-CFG-02 冻结）；③列出残留占位项——
`__FILL__` 字面量 / `price_status: placeholder_pending_w2` / rpm_limit·in-out 单价为 null。
有占位项退出码 1（开发期正常，T-LLM-12 回填后应退 0）；结构错误退出码 2。

用法（00 §1 A14）：`cd server && uv run python scripts/check_config_fill.py [models.yaml 路径]`
"""

from __future__ import annotations

import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(SERVER_ROOT))  # 直跑脚本时可 import worldsim

from worldsim.llm_gateway.router import ConfigError, validate_config  # noqa: E402

import yaml  # noqa: E402


def find_unfilled(node: object, path: str = "") -> list[str]:
    """递归收集占位项路径（`__FILL__` / placeholder_pending_w2 / 关键数值 null）。"""
    hits: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if v == "__FILL__":
                hits.append(p)
            elif k == "price_status" and v == "placeholder_pending_w2":
                hits.append(f"{p}（价格占位待 W2 回填）")
            elif k in ("rpm_limit", "input_price", "output_price") and v is None:
                hits.append(f"{p}=null（待回填）")
            else:
                hits.extend(find_unfilled(v, p))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits.extend(find_unfilled(v, f"{path}[{i}]"))
    elif node == "__FILL__":
        hits.append(path)
    return hits


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else SERVER_ROOT / "config" / "models.yaml"
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"FAIL: {path} YAML 解析失败：{exc}")
        return 2
    try:
        validate_config(cfg)
    except ConfigError as exc:
        print(f"FAIL: {path} 结构校验失败：{exc}")
        return 2
    unfilled = find_unfilled(cfg)
    if unfilled:
        print(f"UNFILLED: {path} 残留 {len(unfilled)} 处占位（T-LLM-12 回填后应清零）：")
        for h in unfilled:
            print(f"  - {h}")
        return 1
    print(f"OK: {path} 四段齐全、无占位残留")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
