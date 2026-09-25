"""输出结构化解析：JSON schema 校验 + 重试（03 文档 T-LLM-10；04 §6.1 step3、§8.6，01 §7）。

- 每类 task_type 一份输出 schema（`config/schemas/<task_type>.json`，与 prompts/ 平级，03 §6 D9）；
  动作 `type` 枚举 = 19 项动作空间（唯一持有方 01 §4；schema 枚举与共享常量的一致性由
  `tests/test_llm_parse.py::test_action_enum_matches_19` 钉死，禁止抄副本漂移）。
- 失败路径钉死（04 §6.1 step3 逐字）：校验/解析失败 → 重试 1 次（prompt 追加格式纠错后缀）→
  仍失败 → 返回结构化"降级决策"（think"走神了"）交裁决器，并落 `llm_calls` `status='failed'` 行。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable

import jsonschema

from .providers.base import ChatResult, Message

log = logging.getLogger(__name__)

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "config" / "schemas"

CORRECTION_SUFFIX = (
    "\n\n[格式纠错] 上次输出未通过 JSON schema 校验：{error}\n"
    "请严格只输出符合契约的 JSON 对象，不要输出任何解释文字。"
)

# 降级决策（04 §6.1 step3："仍失败记 think 动作('走神了')并落 llm_calls"）
FALLBACK_THINK: dict[str, Any] = {
    "intent": "走神了",
    "action": {"type": "think", "args": {"topic_hint": "走神了"}},
    "say": None,
    "emotion_delta": {},
}


class SchemaNotFound(KeyError):
    """task_type 无对应 schema 文件。"""


class OutputValidationError(ValueError):
    """LLM 输出未通过 JSON schema 校验。"""


@lru_cache(maxsize=16)
def _schema(task_type: str) -> dict[str, Any]:
    path = SCHEMAS_DIR / f"{task_type}.json"
    if not path.is_file():
        raise SchemaNotFound(f"{task_type} 无 schema 文件（{SCHEMAS_DIR}/{task_type}.json）")
    return json.loads(path.read_text(encoding="utf-8"))


def validate(task_type: str, obj: Any) -> None:
    """按 `config/schemas/<task_type>.json` 校验输出对象；失败抛 `OutputValidationError`。"""
    try:
        jsonschema.validate(obj, _schema(task_type))
    except jsonschema.ValidationError as exc:
        raise OutputValidationError(f"{task_type} 输出未过 schema：{exc.message}") from exc


def parse_json(task_type: str, text: str) -> dict[str, Any]:
    """解析 + 校验 LLM 输出文本；失败抛 OutputValidationError。"""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise OutputValidationError(f"{task_type} 输出非合法 JSON：{exc}") from exc
    validate(task_type, obj)
    return obj


ChatFn = Callable[[str, list[Message], dict[str, Any]], Awaitable[ChatResult]]
RecordFailureFn = Callable[[str], Awaitable[None]]


async def call_and_parse(
    call: ChatFn,
    task_type: str,
    messages: list[Message],
    gen_params: dict[str, Any] | None = None,
    *,
    record_failure: RecordFailureFn | None = None,
) -> dict[str, Any]:
    """调用 → 校验 → 失败重试 1 次（追加格式纠错后缀）→ 仍失败返回降级决策（04 §6.1 step3）。

    `call(task_type, messages, gen_params)` 由调用侧注入（网关 chat 的偏函数）；
    `record_failure(error)` 用于落 `llm_calls` status='failed' 行（ledger 口径，T-LLM-07）。
    恰好 2 次 provider 调用后仍失败才降级（验收断言计数 = 2）。
    """
    last_error: str = ""
    current = list(messages)
    for attempt in range(2):
        result = await call(task_type, current, gen_params or {})
        try:
            return parse_json(task_type, result.text)
        except OutputValidationError as exc:
            last_error = str(exc)
            log.warning("LLM 输出校验失败（task_type=%s 第 %d 次）：%s", task_type, attempt + 1, last_error)
            current = list(messages) + [{"role": "user", "content": CORRECTION_SUFFIX.format(error=last_error)}]
    if record_failure is not None:
        await record_failure(last_error)
    log.warning("LLM 输出重试 1 次仍失败 → 降级决策 think『走神了』（task_type=%s，04 §6.1 step3）", task_type)
    return dict(FALLBACK_THINK)
