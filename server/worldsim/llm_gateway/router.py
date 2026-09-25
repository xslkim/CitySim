"""models.yaml 路由表加载/校验/SIGHUP 热更（03 文档 T-LLM-04；04 §8.1/§12.4，01 T-CFG-02 键名口径）。

- 四段键名冻结：`providers/task_routes/generation_params/thresholds`（01 T-CFG-02）。
- 路由只绑 task_type → provider 别名 + 型号 + 降级链，不硬编型号于代码（04 §8.1）。
- 降级链末端特殊动作（04 §8.1）：`pause_clock`（明星档暂停时钟）/ `queue_retry`（director 排队延后）/
  `pause_publish_channel`（safety 故障 = 暂停发布通道）；`enabled: false` 的跳（付费档/vLLM 占位）跳过。
- SIGHUP 热更（04 §12.4）：收到信号重读并校验；校验失败保留旧配置 + WARN。
"""

from __future__ import annotations

import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .providers.base import TASK_TYPES

log = logging.getLogger(__name__)

MODELS_CONFIG_ENV = "WSIM_MODELS_CONFIG"

# 04 §8.1 降级链末端特殊动作（非 provider 跳）
SPECIAL_ACTIONS: tuple[str, ...] = ("pause_clock", "queue_retry", "pause_publish_channel")

# 四段键名（01 T-CFG-02 冻结）
REQUIRED_SECTIONS: tuple[str, ...] = ("providers", "task_routes", "generation_params", "thresholds")

# 04 §8.6 生成参数表覆盖的 7 个 task_type（embed/safety 之外另有 safety 行；embed 无 LLM 生成参数）
GEN_PARAMS_TASKS: tuple[str, ...] = (
    "star_decision", "dialogue", "secondary", "bgsummary", "reflection", "director", "safety",
)


class ConfigError(ValueError):
    """models.yaml 结构校验失败（热更时保留旧配置，04 §12.4）。"""


@dataclass(frozen=True)
class RouteHop:
    """降级链一跳：`special` 非空时为末端特殊动作，provider/model 无意义。"""

    provider: str = ""
    model: str = ""
    special: str = ""  # pause_clock / queue_retry / pause_publish_channel

    @property
    def is_special(self) -> bool:
        return bool(self.special)


class ModelRouter:
    """models.yaml 路由表持有与热更。线程/协程安全前提：单裁决协程读（00 §4 红线 10）。"""

    def __init__(self, config: dict[str, Any], *, path: str | None = None) -> None:
        validate_config(config)
        self._cfg = config
        self._path = path

    # ---- 加载/热更 -------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ModelRouter:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cls(cfg, path=str(path))

    @classmethod
    def from_env(cls, default_path: str | os.PathLike[str]) -> ModelRouter:
        return cls.load(os.environ.get(MODELS_CONFIG_ENV) or default_path)

    @property
    def config(self) -> dict[str, Any]:
        return self._cfg

    def reload(self) -> bool:
        """重读配置文件；校验失败保留旧配置 + WARN，返回 False（04 §12.4）。"""
        if not self._path:
            log.warning("models.yaml 热更忽略：本 router 无文件路径（构造期 dict 注入）")
            return False
        try:
            with open(self._path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            validate_config(cfg)
        except Exception as exc:
            log.warning("models.yaml 热更校验失败，保留旧配置：%s（04 §12.4）", exc)
            return False
        self._cfg = cfg
        log.info("models.yaml 热更生效（%s）", self._path)
        return True

    def install_sighup(self) -> None:
        """挂 SIGHUP → reload（04 §12.4）；主循环启动时调用一次。"""
        def _handler(signum: int, frame: Any) -> None:
            self.reload()

        signal.signal(signal.SIGHUP, _handler)

    # ---- 查询 ------------------------------------------------------------

    def chain(self, task_type: str) -> list[RouteHop]:
        """task_type 的可用降级链：primary + fallback[]（跳过 enabled:false 占位跳）。

        特殊动作跳总是保留（它是链尽行为定义，04 §8.1）。
        """
        route = self._cfg.get("task_routes", {}).get(task_type) or {}
        hops: list[RouteHop] = []
        primary = route.get("primary") or {}
        if primary:
            hops.append(RouteHop(provider=primary.get("provider", ""), model=primary.get("model", "")))
        for fb in route.get("fallback") or []:
            if isinstance(fb, str):
                hops.append(RouteHop(special=fb))
                continue
            if fb.get("enabled") is False:
                continue  # 付费档/vLLM 占位跳（充值后/上线期启用，00 §1 A3/A9）
            hops.append(RouteHop(provider=fb.get("provider", ""), model=fb.get("model", "")))
        return hops

    def gen_params(self, task_type: str) -> dict[str, Any]:
        return dict(self._cfg.get("generation_params", {}).get(task_type, {}))

    def provider_config(self, name: str) -> dict[str, Any]:
        return dict(self._cfg.get("providers", {}).get(name, {}))

    def price_of(self, provider: str, model: str) -> tuple[float | None, float | None]:
        """(input_price, output_price) 每 token 单价（微元口径见 ledger T-LLM-07）；null = 未回填。

        免费档填 0；付费档/embedding-3 占位 null（price_status=placeholder_pending_w2，§5 R1）。
        """
        section = "embedding" if provider == "zhipu" and model.startswith("embedding") else "chat"
        pcfg = self._cfg.get("providers", {}).get(provider, {})
        for m in pcfg.get(section) or []:
            if m.get("id") == model:
                return m.get("input_price"), m.get("output_price", 0 if section == "chat" else None)
        # local_embed 等无型号列表的 provider：input_price 直挂 provider 段
        if "input_price" in pcfg:
            return pcfg.get("input_price"), None
        return None, None

    def thresholds(self) -> dict[str, Any]:
        return dict(self._cfg.get("thresholds", {}))


def validate_config(cfg: Any) -> None:
    """四段齐全 + 9 项 task_type 路由闭合 + 生成参数覆盖（热更校验唯一口径）。"""
    if not isinstance(cfg, dict):
        raise ConfigError("models.yaml 顶层必须是 mapping")
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg]
    if missing:
        raise ConfigError(f"models.yaml 缺段：{missing}（四段键名冻结，01 T-CFG-02）")
    routes = cfg["task_routes"] or {}
    for t in TASK_TYPES:
        route = routes.get(t)
        if not route or not (route.get("primary") or {}).get("provider"):
            raise ConfigError(f"task_routes 缺 {t}.primary（04 §8.1 九项全集，含 world_copy）")
        for fb in route.get("fallback") or []:
            if isinstance(fb, str):
                if fb not in SPECIAL_ACTIONS:
                    raise ConfigError(f"task_routes.{t} 未知特殊动作 {fb!r}（合法集 {SPECIAL_ACTIONS}）")
            elif not fb.get("provider"):
                raise ConfigError(f"task_routes.{t}.fallback 跳缺 provider")
    gen = cfg["generation_params"] or {}
    for t in GEN_PARAMS_TASKS:
        if t not in gen:
            raise ConfigError(f"generation_params 缺 {t} 行（04 §8.6 七行表）")
