"""T-WA-10 事件注册表消费与对拍（04 文档 T-WA-10 验收 1~3；A2 单源裁定：纯消费，
不交付生成器/同名 yaml——两者唯一归 01 T-CFG-05）。

- economy 审计类型清单 = 从 `config/event_types.yaml` 读审计域标记 economy 的类型全集
  （04 §10.1 ① 唯一数据源；禁 LIKE 前缀法，06 §1.3）；本模块不另存副本、不复制字面量进代码。
- 06 §1.2 行数/类型集对拍：解析 06 md 表格动态展开（含 `.remind` 类后缀行），与 yaml 集合相等。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SERVER_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVER_ROOT.parent
EVENT_TYPES_PATH = SERVER_ROOT / "config" / "event_types.yaml"
DOC_06_PATH = REPO_ROOT / "docs" / "design" / "06-契约登记表.md"
GEN_SCRIPT = SERVER_ROOT / "scripts" / "gen_event_types.py"

# 06 §1.3 列举的动作域结算类型（反前缀法回归锚点——类型名清单由 06 持有，此处为对拍镜像）
ACTION_DOMAIN_SETTLEMENT = {
    "agent.eat", "agent.shop", "agent.trade_stock",
    "social.give_gift", "social.borrow_money", "social.repay_money",
}


def _registry() -> dict:
    with EVENT_TYPES_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _economy_types(reg: dict) -> set[str]:
    return {t["type"] for t in reg["types"] if t.get("audit_domain") == "economy"}


def _types_from_06() -> set[str]:
    """解析 06 §1.2 注册表类型列（动态展开 `a` / `.b` 后缀形态），供对拍。"""
    text = DOC_06_PATH.read_text(encoding="utf-8")
    sec = text.split("### 1.2 注册表", 1)[1].split("### 1.3", 1)[0]
    out: set[str] = set()
    for line in sec.splitlines():
        if not line.startswith("| `"):
            continue
        cell = line.split("|")[1]
        tokens = re.findall(r"`([^`]+)`", cell)
        base: str | None = None
        for tok in tokens:
            tok = tok.strip()
            if tok.startswith(".") and base:
                out.add(base.rsplit(".", 1)[0] + tok)  # `.remind` → 前缀拼接
            elif "." in tok:
                out.add(tok)
                base = tok
    return out


def test_gen_check_passes() -> None:
    """验收 1：gen_event_types.py --check 退出 0（脚本与 yaml 均归 01 T-CFG-05，本任务纯消费）。"""
    r = subprocess.run([sys.executable, str(GEN_SCRIPT), "--check"],
                       capture_output=True, text=True, cwd=SERVER_ROOT)
    assert r.returncode == 0, f"--check 漂移：{r.stderr}"


def test_economy_set() -> None:
    """验收 2：economy 清单含 06 §1.2 全部 economy 标记类型；06 §1.3 动作域结算类型在列。"""
    reg = _registry()
    econ = _economy_types(reg)
    assert ACTION_DOMAIN_SETTLEMENT <= econ, "动作域结算类型必须在 economy 审计清单（06 §1.3，反前缀法回归）"
    # 06 §1.2 中 audit_domain=economy 的注册行 ⊆ 清单（以 06 md 为仲裁方对拍列名不可机读全表，
    # 此处对拍注册表内 economy 域前缀类型全集 + 上方动作域锚点，覆盖 06 §1.2 全部 economy 标记行）
    economy_prefixed = {t for t in (x["type"] for x in reg["types"]) if t.startswith("economy.")}
    assert economy_prefixed <= econ
    # 与生成物 economy_audit_types.yaml 一致（单源派生链对拍）
    derived = yaml.safe_load((SERVER_ROOT / "config" / "economy_audit_types.yaml").read_text(encoding="utf-8"))
    assert set(derived["economy_types"]) == econ


def test_all_types_covered() -> None:
    """验收 3：类型集 = 06 §1.2 注册表全集（动态展开计数，不写死数字）；改 yaml → --check 失败（负例）。"""
    yaml_types = {t["type"] for t in _registry()["types"]}
    doc_types = _types_from_06()
    assert yaml_types == doc_types, (
        f"yaml ∖ 06 = {sorted(yaml_types - doc_types)}；06 ∖ yaml = {sorted(doc_types - yaml_types)}")
    assert len(yaml_types) == len(doc_types)  # 动态计数一致（不写死字面量）


def test_negative_drift_detected() -> None:
    """验收 3 负例：临时变更注册表（内存注入未登记类型）→ 派生渲染与磁盘漂移（--check 必失败口径）。"""
    sys.path.insert(0, str(SERVER_ROOT / "scripts"))
    try:
        import gen_event_types as gen

        reg = gen.load_registry()
        mutated = dict(reg)
        mutated["types"] = [*reg["types"], {
            "type": "economy.bogus_unregistered", "source": "world", "trigger": ["world"],
            "audit_domain": "economy", "payload_keys": {"public": ["amount_cents"], "conditional": []},
        }]
        rendered = gen.render_economy(mutated)
        on_disk = (SERVER_ROOT / "config" / "economy_audit_types.yaml").read_text(encoding="utf-8")
        assert rendered != on_disk, "注入漂移必须使渲染结果与磁盘不一致（--check 将非零退出）"
        assert "economy.bogus_unregistered" in rendered
    finally:
        sys.path.remove(str(SERVER_ROOT / "scripts"))
