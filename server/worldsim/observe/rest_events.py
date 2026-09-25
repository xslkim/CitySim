"""REST：事件查询/直方图/中文检索（05 T-WEB-05；03 §5.1、05 §3.8、06 §1.1/§2）。

- 六维过滤全部下推 SQL：type（逗号多值，合法值 = 06 §1.2 注册表 = `config/event_types.yaml`
  唯一手维护源镜像，00 §1 A11）/actor（`actors` 数组 GIN）/location/trigger（六值含 system，06 §1.1）/
  grade（**最新生效 grade**：JOIN `obs.event_grade_view`，不读 `events.ui` 初值，05 §3.8/06 §1.2 注释仲裁）/
  q（`payload->>'text_display'` 三元组 ILIKE，03 §5.1 pg_trgm 降级路径，05 文档 D8）。
- 分页：cursor = 上一页末条 **seq**（全局单调，00 §4 红线 2）；limit ≤500 默认 200（03 §5.1）。
- items[] 单条 schema 逐字 03 §5.1（serde.serialize_event 唯一实现）。
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, Query

from .app import ApiError, get_pool, ok_envelope, require_token
from .serde import serialize_event

router = APIRouter(prefix="/api/events", dependencies=[Depends(require_token)])

TRIGGER_ENUM = {"autonomous", "world", "director", "gift", "vote", "system"}  # 06 §1.1 封闭枚举
GRADE_ENUM = {"A", "B", "C"}
DEFAULT_LIMIT = 200
MAX_LIMIT = 500  # 03 §5.1
EVENT_TYPES_PATH = Path(__file__).resolve().parents[2] / "config" / "event_types.yaml"


@lru_cache(maxsize=1)
def registered_types() -> frozenset[str]:
    """事件类型注册表镜像（唯一手维护源 = config/event_types.yaml，06 §1.2 转写，00 §1 A11）。"""
    reg = yaml.safe_load(EVENT_TYPES_PATH.read_text(encoding="utf-8"))
    return frozenset(t["type"] for t in reg["types"])


def _parse_from_to(v: str | None, label: str) -> tuple[str, Any] | None:
    """from/to 接受 tick（纯数字）或 sim_time ISO（03 §5.1）。返回（列名, 值）。"""
    if v is None:
        return None
    if v.isdigit():
        return ("tick", int(v))
    try:
        return ("sim_time", dt.datetime.fromisoformat(v))
    except ValueError as e:
        raise ApiError("bad_param", f"{label} 须为 tick 整数或 ISO sim_time：{v!r}", 422) from e


def _split_csv(v: str | None) -> list[str]:
    return [s.strip() for s in v.split(",") if s.strip()] if v else []


@router.get("")
async def api_events(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    type: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    location: str | None = Query(default=None),
    trigger: str | None = Query(default=None),
    grade: str | None = Query(default=None),
    q: str | None = Query(default=None),
    cursor: int | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    if limit > MAX_LIMIT or limit < 1:
        raise ApiError("bad_param", f"limit 须 ∈ [1, {MAX_LIMIT}]（03 §5.1）", 422)
    conds: list[str] = []
    args: list[Any] = []

    for label, v in (("from", from_), ("to", to)):
        parsed = _parse_from_to(v, label)
        if parsed:
            col, val = parsed
            conds.append(f"e.{col} {'>=' if label == 'from' else '<='} ${len(args) + 1}")
            args.append(val)

    types = _split_csv(type)
    if types:
        unknown = [t for t in types if t not in registered_types()]
        if unknown:
            raise ApiError("bad_param", f"未注册事件类型（06 §1.2）：{unknown}", 422)
        conds.append(f"e.type = ANY(${len(args) + 1}::text[])")
        args.append(types)

    actors = _split_csv(actor)
    if actors:
        conds.append(f"e.actors && ${len(args) + 1}::text[]")
        args.append(actors)

    if location:
        conds.append(f"e.location_id = ${len(args) + 1}")
        args.append(location)

    triggers = _split_csv(trigger)
    if triggers:
        bad = [t for t in triggers if t not in TRIGGER_ENUM]
        if bad:
            raise ApiError("bad_param", f"非法 trigger（06 §1.1 六枚举）：{bad}", 422)
        conds.append(f"e.trigger = ANY(${len(args) + 1}::text[])")
        args.append(triggers)

    join = ""
    if grade:
        if grade not in GRADE_ENUM:
            raise ApiError("bad_param", f"非法 grade：{grade!r}（A/B/C）", 422)
        join = "JOIN obs.event_grade_view g ON g.seq = e.seq"
        conds.append(f"g.grade = ${len(args) + 1}")
        args.append(grade)

    if q:
        conds.append(f"e.payload->>'text_display' ILIKE ${len(args) + 1}")
        args.append("%" + q.replace("%", "").replace("_", "") + "%")

    if cursor is not None:
        conds.append(f"e.seq > ${len(args) + 1}")
        args.append(cursor)

    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = await pool.fetch(
        f"SELECT e.* FROM obs.events e {join} {where} ORDER BY e.seq LIMIT ${len(args) + 1}",
        *args, limit + 1,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return await ok_envelope(pool, {
        "items": [serialize_event(r) for r in rows],
        "next_cursor": int(rows[-1]["seq"]) if has_more and rows else None,
    }, kind="events")


@router.get("/histogram")
async def api_events_histogram(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    bucket: str = Query(default="sim_hour"),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """密度直方图（03 §3.2/§5.1）：每模拟小时事件计数 + A 级叠加（最新生效 grade 口径）。"""
    if bucket != "sim_hour":
        raise ApiError("bad_param", "bucket 仅支持 sim_hour", 422)
    conds: list[str] = []
    args: list[Any] = []
    for label, v in (("from", from_), ("to", to)):
        parsed = _parse_from_to(v, label)
        if parsed:
            col, val = parsed
            conds.append(f"e.{col} {'>=' if label == 'from' else '<='} ${len(args) + 1}")
            args.append(val)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = await pool.fetch(
        f"""
        SELECT date_trunc('hour', e.sim_time) AS bucket_start, count(*) AS count,
               count(*) FILTER (WHERE g.grade = 'A') AS a_count
        FROM obs.events e
        LEFT JOIN obs.event_grade_view g ON g.seq = e.seq
        {where}
        GROUP BY 1 ORDER BY 1
        """,
        *args,
    )
    return await ok_envelope(pool, [
        {"bucket_start": r["bucket_start"].isoformat(), "count": int(r["count"]),
         "a_count": int(r["a_count"])}
        for r in rows
    ], kind="events_histogram")
