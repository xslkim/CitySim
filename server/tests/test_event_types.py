"""T-CFG-05 event_types.yaml + gen_event_types.py 单源生成链验收。

测试纪律（00 §1 A11）：一律从 yaml / 06 原文动态读值，禁止写死字面量（含类型计数——动态计数）。
06 解析口径：§1.2 表格首列反引号类型名（`a.b` / `.c` 续写名按前名去末段展开）；审计域列（第 6 列）
含 "economy" 为 economy 标记；括号注记含 "时" 为条件标记（06 现有三条：收费时/含 delta_salary 时/仅 lucky 含金额时）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
DOC_06 = REPO_ROOT / "docs" / "design" / "06-契约登记表.md"
SRC = SERVER_ROOT / "config" / "event_types.yaml"
DERIVED = [
    SERVER_ROOT / "config" / "payload_whitelist.yaml",
    SERVER_ROOT / "config" / "economy_audit_types.yaml",
    SERVER_ROOT / "ddl" / "obs_whitelist_seed.sql",
]


def _registry() -> dict:
    with SRC.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _expand_names(names: list[str]) -> list[str]:
    """展开 06 表格首列的反引号名（`.remind` 类续写名接前名去末段）。"""
    out: list[str] = []
    for n in names:
        n = n.strip()
        if not n:
            continue
        if n.startswith("."):
            prefix = out[-1].rsplit(".", 1)[0]
            out.append(prefix + n)
        else:
            out.append(n)
    return out


def _parse_06_registry() -> tuple[list[str], set[str], set[str]]:
    """解析 06 §1.2：返回 (全量类型, economy 类型, 条件 economy 类型)。"""
    text = DOC_06.read_text(encoding="utf-8")
    sec = text.split("### 1.2 注册表（全量）")[1].split("### 1.3")[0]
    all_types: list[str] = []
    eco: set[str] = set()
    eco_cond: set[str] = set()
    for line in sec.splitlines():
        line = line.strip()
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.split("|")]
        names = _expand_names(re.findall(r"`([^`]+)`", cells[1]))
        all_types.extend(names)
        audit_cell = cells[6]
        if "economy" in audit_cell:
            eco.update(names)
            m = re.search(r"（([^）]*)）", audit_cell)
            if m and "时" in m.group(1):
                eco_cond.update(names)
    return all_types, eco, eco_cond


def _parse_06_legacy_names() -> list[str]:
    """解析 06 §1.4 旧名表首列（反引号 token；`x.*` 保留为前缀模式）。"""
    text = DOC_06.read_text(encoding="utf-8")
    sec = text.split("### 1.4 旧名 → 契约名对照")[1].split("## 2")[0]
    tokens: list[str] = []
    for line in sec.splitlines():
        line = line.strip()
        if not line.startswith("| `"):
            continue
        first_cell = line.split("|")[1]
        for raw in re.findall(r"`([^`]+)`", first_cell):
            parts = raw.split("/")
            tokens.extend(_expand_names(parts))
    return tokens


def test_registry_covers_06() -> None:
    types_06, _, _ = _parse_06_registry()
    yaml_types = [t["type"] for t in _registry()["types"]]
    assert len(yaml_types) == len(set(yaml_types)), "yaml 存在重复类型"
    assert len(types_06) == len(set(types_06)), "06 解析存在重复类型"
    missing = sorted(set(types_06) - set(yaml_types))
    extra = sorted(set(yaml_types) - set(types_06))
    assert not missing and not extra, f"缺失: {missing}；多余: {extra}"
    assert len(yaml_types) == len(types_06), "条目数动态对拍不等"


def test_no_legacy_names() -> None:
    yaml_types = set(t["type"] for t in _registry()["types"])
    for token in _parse_06_legacy_names():
        token = token.strip()
        if not token:
            continue
        if "*" in token:  # 前缀模式（如 invite.*）
            prefix = token.split("*")[0]
            assert not any(t.startswith(prefix) for t in yaml_types), f"命中旧名前缀: {token}"
        elif "{" in token:  # 花括号模式（如 time.catchup_{start,end}）
            base, rest = token.split("{", 1)
            for alt in rest.rstrip("}").split(","):
                assert base + alt not in yaml_types, f"命中旧名: {base + alt}"
        else:
            assert token not in yaml_types, f"命中 06 §1.4 旧名: {token}"


def test_economy_marks_match_06_1_3() -> None:
    _, eco_06, eco_cond_06 = _parse_06_registry()
    types = _registry()["types"]
    eco_yaml = {t["type"] for t in types if t.get("audit_domain") == "economy"}
    eco_cond_yaml = {t["type"] for t in types if t.get("audit_domain") == "economy" and t.get("audit_condition")}
    assert eco_yaml == eco_06, f"economy 标记不一致：缺 {sorted(eco_06 - eco_yaml)} 多 {sorted(eco_yaml - eco_06)}"
    assert eco_cond_yaml == eco_cond_06, (
        f"条件 economy 标记不一致：缺 {sorted(eco_cond_06 - eco_cond_yaml)} 多 {sorted(eco_cond_yaml - eco_cond_06)}"
    )
    assert len(eco_cond_yaml) == len(eco_cond_06)  # 条件标记含 3 条 06 注记（disturb 行展开 4 类型）


def test_trigger_enum_closed() -> None:
    text = DOC_06.read_text(encoding="utf-8")
    m = re.search(r"trigger 枚举\*\*（6 值[^`]*`([^`]+)`", text)
    enum_06 = {s.strip() for s in m.group(1).split("|")}
    reg = _registry()
    assert set(reg["meta"]["triggers"]) == enum_06
    domains = set(reg["meta"]["domains"])
    for t in reg["types"]:
        assert set(t["trigger"]) <= enum_06, f"{t['type']} trigger 越枚举: {t['trigger']}"
        assert t["type"].split(".")[0] in domains, f"{t['type']} 域越封闭清单"


def _run_gen(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/gen_event_types.py", *args],
        cwd=SERVER_ROOT, capture_output=True, text=True,
    )


def test_derived_artifacts_in_sync() -> None:
    r = _run_gen()
    assert r.returncode == 0, r.stderr
    diff = subprocess.run(
        ["git", "diff", "--exit-code", "--"] + [str(p.relative_to(REPO_ROOT)) for p in DERIVED],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert diff.returncode == 0, f"重跑生成器后派生物有漂移:\n{diff.stdout}"

    # 篡改派生物 → --check 非零退出；复原后 --check 归零
    target = DERIVED[0]
    orig = target.read_text(encoding="utf-8")
    try:
        target.write_text(orig + "\n# tampered\n", encoding="utf-8")
        assert _run_gen("--check").returncode != 0
    finally:
        target.write_text(orig, encoding="utf-8")
    assert _run_gen("--check").returncode == 0

    # --require-types：注册表内类型通过；未登记类型非零退出
    some_type = _registry()["types"][0]["type"]
    assert _run_gen("--check", "--require-types", some_type).returncode == 0
    assert _run_gen("--check", "--require-types", "not.registered.type").returncode != 0
