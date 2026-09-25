#!/usr/bin/env python3
"""头像 manifest 与 portraits.ts 生成（06 T-ART-02 脚本先行段；00 §1 A17：唯一手维护源 = config/agents.yaml）。

派生物（禁止手改，--check 对拍漂移即非零退出）：
  ① ui/assets/portraits/manifest.json   {"A01": {"file": "linwan.png"}, …}（key = 'A01'~'A40'，
     由 agents.yaml `appearance.portrait_file`（01 §6 D6 工程字段）非空条目派生，取文件名部分）
  ② web/src/lib/portraits.ts            web 端 Avatar 消费（05 T-WEB-11）：PORTRAITS 映射 +
     SIGNATURE_POOL（签名色池 = agents.yaml 全部 signature_color.hex 去重，02 §4.3 池口径）+
     portraitUrl/fallbackColor（无资产兜底 = 签名色圆底+姓名首字；色缺失按 id 稳定散列取池内一色）

用法（00 §1 A14）：
  cd server && uv run python scripts/gen_portrait_manifest.py           # 重写派生物
  cd server && uv run python scripts/gen_portrait_manifest.py --check   # 漂移即非零退出
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
AGENTS_YAML = SERVER_ROOT / "config" / "agents.yaml"
OUT_MANIFEST = REPO_ROOT / "ui" / "assets" / "portraits" / "manifest.json"
OUT_PORTRAITS_TS = REPO_ROOT / "web" / "src" / "lib" / "portraits.ts"

GEN_HEADER = "生成物勿手改（由 scripts/gen_portrait_manifest.py 从 config/agents.yaml 派生，00 §1 A17）"


def load_agents() -> list[dict]:
    return yaml.safe_load(AGENTS_YAML.read_text(encoding="utf-8"))["agents"]


def derive_manifest(agents: list[dict]) -> dict[str, dict[str, str]]:
    """portrait_file 非空者成条目（取文件名部分）；null 者无条目走运行时兜底（06 T-ART-02）。"""
    manifest: dict[str, dict[str, str]] = {}
    for a in agents:
        pf = (a.get("appearance") or {}).get("portrait_file")
        if pf:
            manifest[a["agent_id"]] = {"file": Path(str(pf)).name}
    return manifest


def derive_signature_pool(agents: list[dict]) -> list[str]:
    """签名色池 = agents.yaml 全部 appearance.signature_color.hex 去重（签名色唯一权威，00 §1 A17）。"""
    pool: list[str] = []
    for a in agents:
        hex_ = ((a.get("appearance") or {}).get("signature_color") or {}).get("hex")
        if hex_ and hex_ not in pool:
            pool.append(hex_)
    return pool


def render_manifest(manifest: dict[str, dict[str, str]]) -> str:
    # 纯 JSON（无注释位；"生成物勿手改"口径由本脚本 --check 与文档承载）
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_portraits_ts(manifest: dict[str, dict[str, str]], pool: list[str]) -> str:
    entries = ",\n  ".join(f"'{k}': '{v['file']}'" for k, v in sorted(manifest.items()))
    pool_entries = ", ".join(f"'{c}'" for c in pool)
    return f"""// {GEN_HEADER}
/** agent_id → 特写文件名（06 T-ART-02；资产路径形态 /assets/portraits/<file>，T-ART-03 挂载） */
export const PORTRAITS: Record<string, string> = {{
  {entries},
}};

/** 签名色池（02 §4.3；= agents.yaml 全部 signature_color.hex 去重） */
export const SIGNATURE_POOL: readonly string[] = [{pool_entries}];

/** 有资产 → img URL；无资产 → null（调用方走签名色 initials 兜底，05 T-WEB-11） */
export function portraitUrl(agentId: string): string | null {{
  const file = PORTRAITS[agentId];
  return file ? `/assets/portraits/${{file}}` : null;
}}

/** 稳定散列兜底色（签名色缺失时按 id 取池内一色，05 T-WEB-11；djb2 确定性） */
export function fallbackColor(agentId: string): string {{
  let h = 5381;
  for (const ch of agentId) h = ((h * 33) ^ ch.charCodeAt(0)) >>> 0;
  return SIGNATURE_POOL[h % SIGNATURE_POOL.length] ?? '#8A93A0'; // neutral 兜底（02 §7.1）
}}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="头像 manifest/portraits.ts 生成（06 T-ART-02 脚本段）")
    ap.add_argument("--check", action="store_true", help="只校验不重写：漂移即非零退出")
    args = ap.parse_args()

    agents = load_agents()
    manifest = derive_manifest(agents)
    pool = derive_signature_pool(agents)
    rendered = {
        OUT_MANIFEST: render_manifest(manifest),
        OUT_PORTRAITS_TS: render_portraits_ts(manifest, pool),
    }
    if args.check:
        drifted = [str(p) for p, content in rendered.items()
                   if not p.exists() or p.read_text(encoding="utf-8") != content]
        if drifted:
            print(f"FAIL: 派生物与 agents.yaml 漂移: {drifted}（重跑 gen_portrait_manifest.py）",
                  file=sys.stderr)
            return 1
        print("OK: manifest/portraits.ts 与 agents.yaml 一致")
        return 0
    for p, content in rendered.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        print(f"written {p.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
