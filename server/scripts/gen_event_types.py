#!/usr/bin/env python3
"""注册表单源生成链（00 §1 A11）：从 config/event_types.yaml 派生全部下游镜像。

用法（00 §1 A14 调用约定）：
  cd server && uv run python scripts/gen_event_types.py              # 重写派生物
  cd server && uv run python scripts/gen_event_types.py --check      # 只校验不重写：漂移即非零退出（04 T-WA-10 纯消费此模式）
  cd server && uv run python scripts/gen_event_types.py --check --require-types agent.move,dialogue.chat
                                                                       # 校验给定类型集合 ⊆ 注册表，缺一则非零退出
派生物（头部注释"生成物勿手改"，提交入库）：
  ① config/payload_whitelist.yaml      sync 键白名单（07 T-SYN-01 消费）
  ② config/economy_audit_types.yaml    economy 审计类型清单（04 §10.1 ① :economy_types 数据源，08 消费；禁 LIKE 前缀对账，00 §4 红线 6）
  ③ ddl/obs_whitelist_seed.sql         obs.payload_key_whitelist 种子 INSERT（T-DB-03 消费）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = SERVER_ROOT / "config" / "event_types.yaml"
OUT_PATHS = {
    "whitelist": SERVER_ROOT / "config" / "payload_whitelist.yaml",
    "economy": SERVER_ROOT / "config" / "economy_audit_types.yaml",
    "obs_sql": SERVER_ROOT / "ddl" / "obs_whitelist_seed.sql",
}

GEN_HEADER = "生成物勿手改（由 scripts/gen_event_types.py 从 config/event_types.yaml 派生，00 §1 A11 单源生成链）"


def load_registry() -> dict:
    with SRC_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def render_whitelist(reg: dict) -> str:
    lines = [
        f"# {GEN_HEADER}",
        "# sync 键白名单（07 T-SYN-01 消费）：public = 同步放行；conditional = 仅 visibility='public' 事件放行；未登记键一律剥除",
        "types:",
    ]
    for t in reg["types"]:
        keys = t["payload_keys"]
        pub = "[" + ", ".join(keys.get("public", [])) + "]"
        cond = "[" + ", ".join(keys.get("conditional", [])) + "]"
        lines.append(f"  {t['type']}: {{ public: {pub}, conditional: {cond} }}")
    return "\n".join(lines) + "\n"


def render_economy(reg: dict) -> str:
    eco = [t for t in reg["types"] if t.get("audit_domain") == "economy"]
    lines = [
        f"# {GEN_HEADER}",
        "# economy 审计类型清单（04 §10.1 ① 余额守恒 :economy_types 数据源，08 审计消费；由 06 §1.2 economy 标记生成，禁 LIKE 'economy.%' 前缀对账，00 §4 红线 6）",
        "economy_types:",
    ]
    lines += [f"  - {t['type']}" for t in eco]
    cond = {t["type"]: t["audit_condition"] for t in eco if t.get("audit_condition")}
    lines.append("# 条件 economy 标记（06 §1.2 注记照抄）：仅满足条件的事件行计入审计")
    lines.append("conditional:")
    lines += [f"  {k}: {v}" for k, v in cond.items()]
    return "\n".join(lines) + "\n"


def render_obs_sql(reg: dict) -> str:
    lines = [
        f"-- {GEN_HEADER}",
        "-- obs.payload_key_whitelist 种子（T-DB-03 消费；policy: public = 放行, conditional = 仅 visibility='public' 放行）",
        "INSERT INTO obs.payload_key_whitelist (event_type, key, policy) VALUES",
    ]
    rows: list[str] = []
    for t in reg["types"]:
        for k in t["payload_keys"].get("public", []):
            rows.append(f"  ('{t['type']}', '{k}', 'public')")
        for k in t["payload_keys"].get("conditional", []):
            rows.append(f"  ('{t['type']}', '{k}', 'conditional')")
    lines.append(",\n".join(rows))
    lines.append("ON CONFLICT DO NOTHING;")
    return "\n".join(lines) + "\n"


RENDERERS = {"whitelist": render_whitelist, "economy": render_economy, "obs_sql": render_obs_sql}


def main() -> int:
    ap = argparse.ArgumentParser(description="event_types.yaml 单源生成链（00 §1 A11）")
    ap.add_argument("--check", action="store_true", help="只校验不重写：派生物漂移即非零退出（04 T-WA-10 消费）")
    ap.add_argument("--require-types", default="", help="逗号分隔类型集合，须 ⊆ 注册表，缺一则非零退出")
    args = ap.parse_args()

    reg = load_registry()
    registered = {t["type"] for t in reg["types"]}
    assert len(registered) == len(reg["types"]), "event_types.yaml 存在重复 type"

    required = [s for s in args.require_types.split(",") if s]
    missing = [s for s in required if s not in registered]
    if missing:
        print(f"FAIL: 类型未登记于注册表: {missing}", file=sys.stderr)
        return 1

    rendered = {name: fn(reg) for name, fn in RENDERERS.items()}
    if args.check:
        drifted = []
        for name, content in rendered.items():
            path = OUT_PATHS[name]
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                drifted.append(str(path.relative_to(SERVER_ROOT)))
        if drifted:
            print(f"FAIL: 派生物与 event_types.yaml 漂移: {drifted}（重跑 gen_event_types.py）", file=sys.stderr)
            return 1
        print("OK: 派生物与注册表一致")
        return 0

    for name, content in rendered.items():
        OUT_PATHS[name].write_text(content, encoding="utf-8")
        print(f"written {OUT_PATHS[name].relative_to(SERVER_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
