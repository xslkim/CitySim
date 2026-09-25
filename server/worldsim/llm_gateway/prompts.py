"""prompt 模板注册表（版本化，03 文档 T-LLM-09；04 §6.1 step1/step3、§4.3、§7.1/§7.2、01 §7）。

- 模板文件：`config/prompts/<id>.yaml`，结构 `{id, version, task_type, variables[], system, user}`；
  占位符 `$var`（string.Template 语法，模板正文可安全含 JSON 花括号）。
- 变量契约显式：每模板 `variables[]` 声明，渲染缺声明变量即抛 `MissingVariable`；
  渲染返回 `(template_id, version, messages)`，messages = [{system}, {user}] 供 provider 直接消费；
  `prompt_hash` 计算输入 = 模板版本 + 渲染文本（T-LLM-07 ledger 口径，03 §6 D4）。
- gossip 族失真档位：`fidelity` 由内核按 01 §4.1 计算后注入，注册表据此派生
  `distortion_instruction` 档位指令（阈值口径 01 §4.1，本模块只引用持有方数值的镜像常量）；
  fidelity 不进 payload、不是 LLM 输出字段（06 §2）。
- `lines[].at_offset_s` 不是模板输出字段（03 §6 D5；内核按每句 uniform(2,4)s 写死）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from string import Template
from typing import Any

import yaml

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "config" / "prompts"

# gossip 失真档位（01 §4.1 失真规则镜像：<0.7 丢数字/时间；<0.55 只留"谁+干了什么"主干）
GOSSIP_TIER_DROP_DETAILS = 0.7
GOSSIP_TIER_SKELETON_ONLY = 0.55


class MissingVariable(KeyError):
    """渲染缺声明变量（03 T-LLM-09 实现要点：变量契约显式，缺即错）。"""


class TemplateNotFound(KeyError):
    """模板 id 未注册。"""


def gossip_distortion_instruction(fidelity: float) -> str:
    """按 01 §4.1 保真度档位派生失真指令文本（gossip 族模板注入用）。"""
    if fidelity < GOSSIP_TIER_SKELETON_ONLY:
        return "保真度极低：转述时只保留「谁+干了什么」主干，丢掉全部数字、时间与细节。"
    if fidelity < GOSSIP_TIER_DROP_DETAILS:
        return "保真度偏低：转述时丢掉具体数字/时间字段，只保留大致情节。"
    return "保真度尚可：转述基本忠实，允许轻微措辞出入。"


class PromptTemplate:
    def __init__(self, raw: dict[str, Any], *, source: Path) -> None:
        self.id = str(raw["id"])
        self.version = str(raw["version"])
        self.task_type = str(raw["task_type"])
        self.variables: tuple[str, ...] = tuple(raw.get("variables") or [])
        self.system = str(raw["system"])
        self.user = str(raw["user"])
        self.source = source

    def render(self, context: dict[str, Any]) -> list[dict[str, str]]:
        values: dict[str, str] = {}
        for var in self.variables:
            if var not in context:
                raise MissingVariable(f"模板 {self.id}@v{self.version} 缺变量 {var!r}（variables[] 契约）")
            values[var] = _stringify(context[var])
        try:
            system = Template(self.system).substitute(values)
            user = Template(self.user).substitute(values)
        except KeyError as exc:  # 模板正文引用了未声明变量 → 同口径视为契约违例
            raise MissingVariable(f"模板 {self.id}@v{self.version} 正文引用未声明变量 {exc}") from exc
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class PromptRegistry:
    """模板注册表：加载 config/prompts/*.yaml；渲染入口 `render(template_id, **context)`。"""

    def __init__(self, templates: dict[str, PromptTemplate]) -> None:
        self._templates = templates

    @classmethod
    def load(cls, prompts_dir: str | Path | None = None) -> PromptRegistry:
        root = Path(prompts_dir) if prompts_dir else PROMPTS_DIR
        templates: dict[str, PromptTemplate] = {}
        for path in sorted(root.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            t = PromptTemplate(raw, source=path)
            if t.id in templates:
                raise ValueError(f"模板 id 重复：{t.id}（{path}）")
            templates[t.id] = t
        return cls(templates)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))

    def get(self, template_id: str) -> PromptTemplate:
        try:
            return self._templates[template_id]
        except KeyError:
            raise TemplateNotFound(f"模板 {template_id!r} 未注册（{PROMPTS_DIR}）") from None

    def render(self, template_id: str, **context: Any) -> tuple[str, str, list[dict[str, str]]]:
        """渲染 → (template_id, version, messages)。gossip 族自动派生 distortion_instruction。"""
        t = self.get(template_id)
        if t.task_type == "dialogue" and "fidelity" in t.variables and "distortion_instruction" not in context:
            if "fidelity" not in context:
                raise MissingVariable(f"模板 {t.id}@v{t.version} 缺变量 'fidelity'（variables[] 契约）")
            context["distortion_instruction"] = gossip_distortion_instruction(float(context["fidelity"]))
        return t.id, t.version, t.render(context)
