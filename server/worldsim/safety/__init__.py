"""内容安全三层过滤管线门面（03 文档 T-LLM-11；04 §11.1/§11.2，06 §1.2/§2，03 §6 D2/D7）。

层序（开发期适配，03 §6 D2）：
- 层 1 本地规则（= 04 §11.1 L2 语义前置）：词表+正则毫秒级短路，命中即 block，**不触发**层 2 LLM 调用；
- 层 2 GLM 审核（= L1 在本地 Qwen3-4B 缺位期的替身，task_type='safety'，标签 {pass, soft, block}）；
- 层 3 落库前复查：写库前对最终 `text_display` 再跑一次层 1（防模板拼装/二次加工引入）。

判定语义（04 §11.1）：soft → 标签随行记录（素材池联动 defer M6）；block → 同事件重生成一次 →
仍 block → `visibility='internal'`、不携带 `text_display`（06 §1.2 internal 事件口径）、原文只留
`text_raw`（本机-only，04 §11.2）；安全审计落点 = 结构化 WARN 日志 + llm_calls safety 行
（03 §6 D7 interim，待回登 06 裁决）。

管线同时服务记忆 `content_display` 写入点（04 §11.2/§7.2 反思双通道）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..llm_gateway.providers.base import Message
from .rules import LocalRules

log = logging.getLogger(__name__)

LABELS = ("pass", "soft", "block")  # 04 §11.1 标签集

_SAFETY_SYSTEM = "你是内容安全审核器。审核给定文本（政治/色情/暴恐/未成年人四类），只输出 JSON：{\"label\": \"pass|soft|block\"}。"


@dataclass(frozen=True)
class SafetyResult:
    label: str            # pass / soft / block
    filtered_text: str | None  # pass/soft = 原文；block = None（04 §11.1 只判标签不改写）
    layer: str            # 判定来源层：L1_rules / L2_glm / L3_recheck


@dataclass(frozen=True)
class PublishDecision:
    """block→重生成一次→仍 block 的落库决策（06 §1.2/§2 字段级口径）。"""

    visibility: str            # public / internal
    text_display: str | None   # internal 时 None（internal 事件不携带展示文本）
    text_raw: str | None       # 原文仅本机内部通道（永不出站，04 §11.2）
    label: str
    regenerated: bool          # 是否经历了 1 次重生成


class SafetyPipeline:
    """三层过滤管线。`gateway` 为 LLM 网关门面（层 2 经 task_type='safety' 调用）。"""

    def __init__(self, gateway: Any, *, rules: LocalRules | None = None) -> None:
        self._gw = gateway
        self._rules = rules or LocalRules.load()
        self.l2_calls = 0  # 层 2 调用计数（测试断言层 1 短路不触发 LLM）

    # ---- 单层 -------------------------------------------------------------

    async def check(self, text: str) -> SafetyResult:
        """层 1 短路 → 层 2 GLM 审核（04 §11.1 + 03 §6 D2 层序）。"""
        hit = self._rules.hit(text)
        if hit:
            log.warning("安全层1命中（%s）→ block", hit)
            return SafetyResult(label="block", filtered_text=None, layer="L1_rules")
        self.l2_calls += 1
        messages: list[Message] = [
            {"role": "system", "content": _SAFETY_SYSTEM},
            {"role": "user", "content": text},
        ]
        result = await self._gw.chat("safety", messages)
        label = self._parse_label(result.text)
        return SafetyResult(label=label, filtered_text=text if label != "block" else None, layer="L2_glm")

    def recheck_local(self, text: str) -> SafetyResult:
        """层 3 落库前复查（只跑层 1 本地规则，04 §11.1 + 03 §6 D2）。"""
        hit = self._rules.hit(text)
        if hit:
            log.warning("安全层3落库前复查命中（%s）→ block", hit)
            return SafetyResult(label="block", filtered_text=None, layer="L3_recheck")
        return SafetyResult(label="pass", filtered_text=text, layer="L3_recheck")

    # ---- 发布守卫（block → 重生成一次 → internal） ---------------------------

    async def guard_publication(
        self,
        text: str,
        regenerate: Callable[[], Awaitable[str]] | None = None,
        *,
        final_text: str | None = None,
    ) -> PublishDecision:
        """将置 public 的文本过管线；block → 同事件重生成一次 → 仍 block 置 internal（04 §11.1）。

        - `regenerate`：重生成回调（由裁决管道供给，恰调用 1 次）；
        - `final_text`：落库前终稿（模板拼装/二次加工产物），非 None 时层 3 对它再查一次；
          终稿被层 3 拦下时不再重生成、直接 internal（防加工引入，03 验收 4）。
        """
        result = await self.check(text)
        regenerated = False
        if result.label == "block" and regenerate is not None:
            regenerated = True
            new_text = await regenerate()
            result = await self.check(new_text)
            text = new_text
        # 层 3：对最终展示文本落库前复查
        if result.label != "block":
            final = final_text if final_text is not None else text
            l3 = self.recheck_local(final)
            if l3.label == "block":
                result = l3
        if result.label == "block":
            log.warning("安全管线终判 block（layer=%s regenerated=%s）→ visibility=internal（04 §11.1；安全审计留痕 = 本 WARN + llm_calls safety 行，03 §6 D7）",
                        result.layer, regenerated)
            return PublishDecision(visibility="internal", text_display=None, text_raw=text,
                                   label="block", regenerated=regenerated)
        display = final_text if final_text is not None else result.filtered_text
        return PublishDecision(visibility="public", text_display=display, text_raw=text,
                               label=result.label, regenerated=regenerated)

    # ---- 内部 ---------------------------------------------------------------

    @staticmethod
    def _parse_label(text: str) -> str:
        import json

        try:
            obj = json.loads(text)
            label = str(obj.get("label", ""))
        except (ValueError, TypeError, AttributeError):
            label = ""
        if label not in LABELS:
            # 审核输出无法解析：保守按 block 处理（宁误杀不放行，04 §11.1 语义）
            log.warning("安全层2输出无法解析为标签（%r）→ 保守 block", text[:50])
            return "block"
        return label
