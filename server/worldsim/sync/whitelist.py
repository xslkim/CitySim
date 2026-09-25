"""sync 键白名单（07 T-SYN-01；05 §2.1 同步规范形剥除规则）。

唯一依据 = 06 §1.2"payload 键标注"列（机器可读派生物 = `config/payload_whitelist.yaml`，
由 01 T-CFG-05 `scripts/gen_event_types.py` 从 `event_types.yaml` 生成——本模块只消费，
不持生成脚本、不维护第二份手维护镜像，R1 §A.2）。

- public 键放行；internal 键与未登记键一律剥除（默认剥除原则，06 §1.2 兜底条款）；
  conditional 键（`text_display` 等）仅 `visibility='public'` 事件放行（05 §2.1 N-P0-2）。
- 保底剥除：`text_raw`/prompt 片段/检索上下文/内部评分中间量永不登记 → 恒剥除（红线 7）。
- canonical/digest 唯一实现 = 02 T-ADJ-09 `worldsim/snapshot/canonical.py`（R2 §A.7），
  本模块 import 消费，禁第二实现。
"""

from __future__ import annotations

from typing import Any

from ..snapshot.canonical import load_payload_whitelist


def strip_payload(
    payload: dict[str, Any] | None,
    *,
    type_: str,
    visibility: str,
    whitelist: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """键白名单剥除 → 同步规范形 dict（canonical 化由 canonical.py 负责）。

    未登记类型 → 空 dict（全键剥除）；未登记键 → 剥除（06 §1.2 默认剥除原则）。
    """
    wl = whitelist if whitelist is not None else load_payload_whitelist()
    entry = wl.get(type_, {})
    allowed = set(entry.get("public") or [])
    if visibility == "public":
        allowed |= set(entry.get("conditional") or [])
    return {k: v for k, v in (payload or {}).items() if k in allowed}
