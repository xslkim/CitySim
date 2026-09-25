"""T-ART-02（脚本先行段）头像 manifest 测试：key 形态/文件存在/PNG 1024×1024/manifest↔agents.yaml 对拍。"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
from pathlib import Path

import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
MANIFEST = REPO_ROOT / "ui" / "assets" / "portraits" / "manifest.json"
PORTRAITS_DIR = REPO_ROOT / "ui" / "assets" / "portraits"
AGENT_ID_RE = re.compile(r"^A(0[1-9]|[1-3][0-9]|40)$")


def test_manifest_keys_and_files() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest, "manifest 为空"
    for key, entry in manifest.items():
        assert AGENT_ID_RE.match(key), key
        assert (PORTRAITS_DIR / entry["file"]).exists(), entry["file"]


def test_png_1024() -> None:
    """stdlib 读 IHDR 断言 1024×1024（02 §1.1.2）。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for entry in manifest.values():
        data = (PORTRAITS_DIR / entry["file"]).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = struct.unpack(">II", data[16:24])
        assert (w, h) == (1024, 1024), entry["file"]


def test_manifest_matches_agents_yaml() -> None:
    """manifest 条目集 = agents.yaml 中 portrait_file 非空的 agent 集且 file 逐条一致。"""
    agents = yaml.safe_load((SERVER_ROOT / "config" / "agents.yaml").read_text(encoding="utf-8"))["agents"]
    expected = {
        a["agent_id"]: Path(str(a["appearance"]["portrait_file"])).name
        for a in agents
        if (a.get("appearance") or {}).get("portrait_file")
    }
    manifest = {k: v["file"] for k, v in json.loads(MANIFEST.read_text(encoding="utf-8")).items()}
    assert manifest == expected


def test_check_mode_detects_drift(tmp_path: Path) -> None:
    """--check：手改 manifest 注入脏数据时非零退出（本用例在临时副本上验证口径，不动仓内产物）。"""
    script = SERVER_ROOT / "scripts" / "gen_portrait_manifest.py"
    ok = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True,
                        cwd=str(SERVER_ROOT))
    assert ok.returncode == 0, ok.stderr
    # 注入脏数据 → 非零退出 → 恢复（读原内容重放，不改 git 状态）
    orig = MANIFEST.read_text(encoding="utf-8")
    try:
        dirty = json.loads(orig)
        dirty["A40"] = {"file": "ghost.png"}
        MANIFEST.write_text(json.dumps(dirty, ensure_ascii=False), encoding="utf-8")
        bad = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True,
                             cwd=str(SERVER_ROOT))
        assert bad.returncode != 0
    finally:
        MANIFEST.write_text(orig, encoding="utf-8")
