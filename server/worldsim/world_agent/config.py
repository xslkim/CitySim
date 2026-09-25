"""world.yaml 全量加载与校验（04 T-WA-01；配置=设计部署镜像口径，04 §12.4）。

- 唯一载体 = `server/config/world.yaml`（01 T-CFG-03 持既有 locations/company/stocks/economy
  常量段；本模块作息/节假日/触发规则段为 T-WA-01 追加；director.grade 阈值段归 T-DIR-04 追加）。
- 校验覆盖**全文件**（含既有段）；任何校验失败 → 抛 `WorldConfigError` 拒绝启动（T-WA-01 验收 1）。
- 数值纪律（00 §7 DoD 6）：代码零硬编码阈值，校验只做结构性约束（min≤max、区间格式、
  编制自洽、枚举封闭），设计数字一律从 yaml 读。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError, field_validator, model_validator

SERVER_ROOT = Path(__file__).resolve().parents[1]

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")
_MM_DD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


class WorldConfigError(ValueError):
    """world.yaml 加载/校验失败（拒绝启动口径）。"""


def _hhmm_pair(v: Any) -> tuple[str, str]:
    if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) and _HHMM.match(x) for x in v)):
        raise WorldConfigError(f"时段窗口须为 [\"HH:MM\", \"HH:MM\"]，得到 {v!r}")
    return v[0], v[1]


# ---- pydantic 模型（结构校验；数值语义唯一持有方 = 01 对应节） ------------------


class DeptEntry(BaseModel):
    key: str
    name: str
    total: int
    managers: int
    npc_staff: int
    tenants: int
    business: str

    @model_validator(mode="after")
    def _headcount_self_consistent(self) -> "DeptEntry":
        if self.managers + self.npc_staff + self.tenants != self.total:
            raise ValueError(f"部门 {self.key} 编制不自洽：managers+npc_staff+tenants != total（01 §1.3）")
        return self


class StockSymbol(BaseModel):
    id: str
    name: str
    initial_price_cents: int

    @field_validator("initial_price_cents")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("初始价须为正（分）")
        return v


class MinMaxCents(BaseModel):
    min_cents: int
    max_cents: int

    @model_validator(mode="after")
    def _ordered(self) -> "MinMaxCents":
        if self.min_cents > self.max_cents:
            raise ValueError(f"区间倒置：min_cents({self.min_cents}) > max_cents({self.max_cents})")
        return self


class HolidayEntry(BaseModel):
    name: str
    start: str
    days: int
    confess_weight: float | None = None

    @field_validator("start")
    @classmethod
    def _mm_dd(cls, v: str) -> str:
        if not _MM_DD.match(v):
            raise ValueError(f"节假日日期须为 MM-DD 形态，得到 {v!r}")
        return v

    @field_validator("days")
    @classmethod
    def _days_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("节假日天数须 ≥1")
        return v


class WorldConfig(BaseModel):
    """world.yaml 全量 schema。既有段逐段承接（01 T-CFG-03），追加段承接（04 T-WA-01）。"""

    locations: dict[str, Any]
    company: dict[str, Any]
    stocks: dict[str, Any]
    economy: dict[str, Any]
    schedule: dict[str, Any]
    holidays: dict[str, Any]
    triggers: dict[str, Any]

    @model_validator(mode="after")
    def _validate_all(self) -> "WorldConfig":
        _validate_company(self.company)
        _validate_stocks(self.stocks)
        _validate_economy(self.economy)
        _validate_schedule(self.schedule)
        _validate_holidays(self.holidays)
        _validate_triggers(self.triggers)
        return self


def _validate_company(company: dict[str, Any]) -> None:
    depts = [DeptEntry.model_validate(d) for d in company.get("departments", [])]
    if not depts:
        raise WorldConfigError("company.departments 为空（01 §1.3 六部门编制）")
    ranks = company.get("ranks") or {}
    if set(ranks) != {"M", "P2", "P1"}:
        raise WorldConfigError(f"company.ranks 键集须为 M/P2/P1（01 §1.3 职级表），得到 {sorted(ranks)}")
    if any(int(v) <= 0 for v in ranks.values()):
        raise WorldConfigError("company.ranks 各级人数须为正")


def _validate_stocks(stocks: dict[str, Any]) -> None:
    symbols = [StockSymbol.model_validate(s) for s in stocks.get("symbols", [])]
    if not symbols:
        raise WorldConfigError("stocks.symbols 为空（01 §1.5 股票系统）")
    ids = [s.id for s in symbols]
    if len(ids) != len(set(ids)):
        raise WorldConfigError("stocks.symbols 存在重复标的 id")
    rw = stocks.get("random_walk") or {}
    if float(rw.get("sigma", 0)) <= 0:
        raise WorldConfigError("stocks.random_walk.sigma 须为正")


def _validate_economy(economy: dict[str, Any]) -> None:
    for rank, rng in (economy.get("salary") or {}).items():
        if isinstance(rng, dict) and "min_cents" in rng:
            try:
                MinMaxCents.model_validate(rng)
            except ValidationError as e:
                raise WorldConfigError(f"economy.salary.{rank} 区间倒置") from e
    for tier, rng in ((economy.get("rent") or {}).get("by_floor") or {}).items():
        try:
            MinMaxCents.model_validate({"min_cents": rng["amount_cents"], "max_cents": rng["amount_cents"]})
        except (ValidationError, KeyError) as e:
            raise WorldConfigError(f"economy.rent.by_floor.{tier} 缺 amount_cents") from e
    util = economy.get("utility") or {}
    try:
        MinMaxCents.model_validate(util)
    except ValidationError as e:
        raise WorldConfigError("economy.utility 区间倒置（min_cents > max_cents）") from e


def _validate_schedule(schedule: dict[str, Any]) -> None:
    workday = schedule.get("workday") or {}
    for key in ("morning", "work_am", "lunch", "work_pm", "evening", "sleep"):
        if key not in workday:
            raise WorldConfigError(f"schedule.workday 缺 {key} 段（01 §1.4）")
        _hhmm_pair(workday[key])


def _validate_holidays(holidays: dict[str, Any]) -> None:
    table = [HolidayEntry.model_validate(h) for h in holidays.get("table", [])]
    if not table:
        raise WorldConfigError("holidays.table 非空约束（T-WA-01 验收）")
    names = {h.name for h in table}
    if "春节" not in names:
        raise WorldConfigError("holidays.table 必含春节（T-WA-01 验收；01 §6.1）")
    if len(names) != len(table):
        raise WorldConfigError("holidays.table 存在重复节假日名")
    rules = holidays.get("rules") or {}
    for flag in ("market_closed", "work_events_suspended"):
        if not isinstance(rules.get(flag), bool):
            raise WorldConfigError(f"holidays.rules.{flag} 须为布尔")


def _validate_triggers(triggers: dict[str, Any]) -> None:
    layoff = triggers.get("layoff_rumor") or {}
    if float(layoff.get("drop_pct_threshold", 0)) <= 0:
        raise WorldConfigError("triggers.layoff_rumor.drop_pct_threshold 须为正")
    disturb = triggers.get("disturb") or {}
    for key in ("illness_prob", "weather_prob", "complaint_prob", "lucky_prob"):
        p = float(disturb.get(key, -1))
        if not 0.0 <= p <= 1.0:
            raise WorldConfigError(f"triggers.disturb.{key} 须 ∈ [0,1]，得到 {p}")
    lucky = disturb.get("lucky_amount_cents") or {}
    if int(lucky.get("min", 0)) > int(lucky.get("max", 0)) or int(lucky.get("min", 0)) <= 0:
        raise WorldConfigError("triggers.disturb.lucky_amount_cents 区间非法")
    ot = triggers.get("overtime") or {}
    freq = ot.get("per_dept_per_week") or []
    if not (len(freq) == 2 and 0 < int(freq[0]) <= int(freq[1])):
        raise WorldConfigError("triggers.overtime.per_dept_per_week 须为 [min,max] 且 0<min≤max")
    _hhmm_pair(ot.get("window"))
    tb = triggers.get("team_building") or {}
    _hhmm_pair(tb.get("window"))
    promo = triggers.get("promotion_window") or {}
    if int(promo.get("slots_per_dept", 0)) < 1:
        raise WorldConfigError("triggers.promotion_window.slots_per_dept 须 ≥1（01 §11.2 发生器 1）")


# ---- 加载入口 -----------------------------------------------------------------


def load_world_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """加载并校验 world.yaml，失败抛 `WorldConfigError`（拒绝启动）。返回原始 dict（键序保持）。"""
    candidate = Path(path or os.environ.get("WSIM_WORLD_CONFIG", "") or SERVER_ROOT / "config" / "world.yaml")
    try:
        with candidate.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise WorldConfigError(f"world.yaml 读取失败：{candidate}（{e}）") from e
    try:
        WorldConfig.model_validate(raw)
    except ValidationError as e:
        raise WorldConfigError(f"world.yaml 校验失败（拒绝启动）：\n{e}") from e
    return raw
