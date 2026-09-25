"""canonical JSON 与 digest 唯一实现（02 T-ADJ-09；05 §2.2 公式、04 §9.2、02 文档 v1.2.1 ③）。

**state 侧与 events 流两侧的 canonical + digest 唯一实现归本模块**（R2 §A.7 / R3 修订③）：
07 T-SYN-01/02/09 快照流构造与 digest 推送/校验一律 import 本模块，禁第二实现。

- `canonical(obj)`：递归按键名字典序排序 → 无冗余空白的 UTF-8 JSON（RFC 8785 风格；
  两端同一实现）；`canonical_event_payload` 先经**键白名单**剥除 internal 键
  （唯一依据 = 06 §1.2 标注列，镜像载体 `config/payload_whitelist.yaml` 生成物，00 §1 A11；
  `text_display` 等条件键仅 `visibility='public'` 放行，05 §2.1）——**绝不对 `payload::text`
  直接 hash**（jsonb 键序不保证 + 副本物理剥除内部键，04 §9.2）。
- `digest_events(pool, sim_day)`：`sha256(string_agg(md5(canonical(payload)), '' ORDER BY seq))`
  + `COUNT(*)` + `SUM(seq)`，按 `date(sim_time)` 分组（05 §2.2 公式；模拟日界 = 本地时区，
  与 llm_calls_simday 的 D12-b 口径一致）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))

_WHITELIST_CACHE: dict[str, dict[str, Any]] | None = None


def canonical(obj: Any) -> str:
    """canonical JSON：递归键名典序 + 无冗余空白 UTF-8（05 §2.2）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def load_payload_whitelist(path: str | Path | None = None) -> dict[str, Any]:
    """payload 键白名单（config/payload_whitelist.yaml 生成物；模块级缓存）。"""
    global _WHITELIST_CACHE
    if _WHITELIST_CACHE is None:
        p = Path(path) if path else Path(__file__).resolve().parents[2] / "config" / "payload_whitelist.yaml"
        with p.open(encoding="utf-8") as f:
            _WHITELIST_CACHE = yaml.safe_load(f)["types"]
    return _WHITELIST_CACHE


def canonical_event_payload(
    payload: dict[str, Any], *, type_: str, visibility: str, whitelist: dict[str, Any] | None = None,
) -> str:
    """键白名单剥除后的 canonical payload（同步规范形，05 §2.1/§2.2）。

    未登记类型/键一律剥除（默认剥除原则，06 §1.2 口径块）；条件键仅 visibility='public' 放行。
    """
    wl = whitelist if whitelist is not None else load_payload_whitelist()
    entry = wl.get(type_, {})
    allowed = set(entry.get("public") or [])
    if visibility == "public":
        allowed |= set(entry.get("conditional") or [])
    stripped = {k: v for k, v in (payload or {}).items() if k in allowed}
    return canonical(stripped)


async def digest_events(
    pool: Any, sim_day: dt.date, *, whitelist: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """events 流 digest（05 §2.2）：按 `date(sim_time)`（本地时区）分组取 sim_day 一组。

    返回 {sim_day, count, sum_seq, digest('sha256:…')}；digest = sha256(按 seq 序拼接的
    md5(canonical(payload)) 串)。内容无关快速校验可只用 count/sum_seq。
    """
    rows = await pool.fetch(
        "SELECT seq, sim_time, type, visibility, payload FROM events ORDER BY seq",
    )
    count = 0
    sum_seq = 0
    md5s: list[str] = []
    for r in rows:
        sim_time = r["sim_time"]
        if sim_time.astimezone(LOCAL_TZ).date() != sim_day:
            continue
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        md5s.append(md5_hex(canonical_event_payload(
            payload, type_=r["type"], visibility=r["visibility"], whitelist=whitelist,
        )))
        count += 1
        sum_seq += int(r["seq"])
    return {
        "sim_day": sim_day.isoformat(),
        "count": count,
        "sum_seq": sum_seq,
        "digest": "sha256:" + sha256_hex("".join(md5s)),
    }
