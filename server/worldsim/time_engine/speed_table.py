"""时段变速表：加载、校验、段切换与 SIGHUP 热更（02 T-TIME-02；04 §3.1/§12.4）。

- `config/speed_table.yaml` 唯一创建归 01 T-CFG-01（M0），本模块消费既有文件、持加载/段切换/热更行为；
  数值校验（schema/24h 无空洞/底线约束）归 01，本模块加载时复验同等规则（空洞拒绝、底线校验）。
- 段切换：真实时刻跨段边界触发；continuous 段间仅改压缩比（时钟积分换比，见 clock.set_ratio）；
  batch 段无 ratio，进入即暂停 tick 排程（04 §3.1/§3.3）。
- SIGHUP 热更：`request_reload()` 挂起，`maybe_reload_at_boundary()` 在下一个段边界生效；
  校验失败保留旧配置并 WARN（04 §12.4）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DAY_MINUTES = 24 * 60


class SpeedTableError(ValueError):
    """变速表加载校验失败（空洞/底线/schema）。"""


@dataclass(frozen=True)
class Segment:
    start_min: int          # 含
    end_min: int            # 不含；24:00 = 1440
    mode: str               # 'continuous' | 'batch'
    ratio: float | None = None
    sim_hours_per_run: float | None = None


def parse_hhmm(s: str) -> int:
    hh, mm = str(s).split(":")
    minutes = int(hh) * 60 + int(mm)
    if not 0 <= minutes <= DAY_MINUTES:
        raise SpeedTableError(f"非法时刻 {s!r}")
    return minutes


class SpeedTable:
    """全天 24h 无空洞变速表（04 §3.1 schema 两段式：segments/constraints）。"""

    def __init__(self, segments: list[Segment], constraints: dict[str, Any]) -> None:
        self.segments = tuple(sorted(segments, key=lambda s: s.start_min))
        self.constraints = dict(constraints)
        self._validate()

    def _validate(self) -> None:
        segs = self.segments
        if not segs:
            raise SpeedTableError("segments 为空")
        for seg in segs:
            if not 0 <= seg.start_min < seg.end_min <= DAY_MINUTES:
                raise SpeedTableError(f"段区间非法: {seg}")
            if seg.mode == "continuous":
                if seg.ratio is None or seg.ratio <= 0:
                    raise SpeedTableError(f"continuous 段缺正 ratio: {seg}")
            elif seg.mode == "batch":
                if seg.sim_hours_per_run is None or seg.sim_hours_per_run <= 0:
                    raise SpeedTableError(f"batch 段缺正 sim_hours_per_run: {seg}")
            else:
                raise SpeedTableError(f"未知 mode: {seg.mode!r}")
        if segs[0].start_min != 0:
            raise SpeedTableError(f"首段必须从 00:00 开始（实际 {segs[0].start_min}）")
        if segs[-1].end_min != DAY_MINUTES:
            raise SpeedTableError(f"末段必须以 24:00 结束（实际 {segs[-1].end_min}）")
        for prev, nxt in zip(segs, segs[1:]):
            if prev.end_min != nxt.start_min:
                raise SpeedTableError(
                    f"段间空洞/重叠: {prev.start_min}~{prev.end_min} vs {nxt.start_min}~{nxt.end_min}"
                )
        min_days = float(self.constraints.get("min_sim_days_per_real_week", 0))
        if min_days <= 0:
            raise SpeedTableError("constraints.min_sim_days_per_real_week 缺失或非正")
        if float(self.constraints.get("max_catchup_ratio", 0)) <= 0:
            raise SpeedTableError("constraints.max_catchup_ratio 缺失或非正")
        weekly = self.sim_hours_per_real_day() * 7 / 24
        if weekly < min_days:
            raise SpeedTableError(
                f"底线校验失败：{weekly:.2f} 模拟日/真实周 < min_sim_days_per_real_week={min_days}"
            )

    def segment_at_min(self, minute_of_day: int) -> Segment:
        minute_of_day %= DAY_MINUTES
        for seg in self.segments:
            if seg.start_min <= minute_of_day < seg.end_min:
                return seg
        raise SpeedTableError(f"时刻 {minute_of_day} 无段覆盖（配置有空洞）")

    def sim_hours_per_real_day(self) -> float:
        total = 0.0
        for seg in self.segments:
            if seg.mode == "batch":
                total += float(seg.sim_hours_per_run)  # type: ignore[arg-type]
            else:
                total += (seg.end_min - seg.start_min) / 60 * float(seg.ratio)  # type: ignore[arg-type]
        return total

    @property
    def max_catchup_ratio(self) -> float:
        return float(self.constraints["max_catchup_ratio"])


def load(path: str | Path) -> SpeedTable:
    """加载并校验变速表；校验失败抛 SpeedTableError。"""
    with Path(path).open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict) or set(cfg.keys()) != {"segments", "constraints"}:
        raise SpeedTableError("顶层键必须为 segments/constraints 两段式（04 §3.1）")
    segments = [
        Segment(
            start_min=parse_hhmm(s["start"]),
            end_min=parse_hhmm(s["end"]),
            mode=s["mode"],
            ratio=float(s["ratio"]) if "ratio" in s else None,
            sim_hours_per_run=float(s["sim_hours_per_run"]) if "sim_hours_per_run" in s else None,
        )
        for s in cfg["segments"]
    ]
    return SpeedTable(segments, cfg["constraints"])


class SpeedTableReloader:
    """SIGHUP 热更（04 §12.4）：信号侧只挂起，段边界生效；校验失败保留旧配置 + WARN。"""

    def __init__(self, path: str | Path, table: SpeedTable | None = None) -> None:
        self._path = Path(path)
        self._current = table if table is not None else load(self._path)
        self._pending = False

    @property
    def current(self) -> SpeedTable:
        return self._current

    @property
    def pending(self) -> bool:
        return self._pending

    def request_reload(self) -> None:
        """SIGHUP handler 调用：标记热更挂起（不立即生效，等下一个段边界）。"""
        self._pending = True
        log.info("speed_table 热更挂起：将在下一个段边界生效（04 §12.4）")

    def maybe_reload_at_boundary(self) -> bool:
        """段边界由时钟调用；返回是否发生了生效重载。校验失败保留旧配置并 WARN。"""
        if not self._pending:
            return False
        self._pending = False
        try:
            self._current = load(self._path)
        except Exception:
            log.warning("speed_table 热更校验失败，保留旧配置（04 §12.4）", exc_info=True)
            return False
        log.info("speed_table 热更生效（段边界）")
        return True
