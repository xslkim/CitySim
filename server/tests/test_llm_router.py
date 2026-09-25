"""T-LLM-04 models.yaml 路由表与 SIGHUP 热更验收（03 验收 1~2）。

口径：9 项 task_type 全集（含 world_copy）均有 primary 且链尽行为与 04 §8.1 一致；
合法修改热生效、非法 YAML 保留旧配置 + WARN（04 §12.4）。
"""

from __future__ import annotations

import copy
import logging
import os
import signal
from pathlib import Path

import pytest
import yaml

from worldsim.llm_gateway.providers.base import TASK_TYPES
from worldsim.llm_gateway.router import (
    SPECIAL_ACTIONS,
    ConfigError,
    ModelRouter,
    validate_config,
)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "models.yaml"


def test_route_table_covers_all_task_types() -> None:
    """真实 config/models.yaml：9 项 task_type 均有 primary，链尽行为与 04 §8.1 逐条一致。"""
    router = ModelRouter.load(CONFIG_PATH)
    for t in TASK_TYPES:
        hops = router.chain(t)
        assert hops and not hops[0].is_special, f"{t} 缺 primary 跳"
    # 链尽行为（04 §8.1）：明星档暂停时钟；director/world_copy 排队延后；safety 暂停发布通道
    for t in ("star_decision", "dialogue", "reflection"):
        assert router.chain(t)[-1].special == "pause_clock", t
    for t in ("director", "world_copy"):
        assert router.chain(t)[-1].special == "queue_retry", t
    assert router.chain("safety")[-1].special == "pause_publish_channel"
    # 开发期路由落地（00 §1 A9/A10）：明星档 glm-4.5-flash、次要/背景/安全 glm-4-flash、embed 本地 bge-m3
    assert router.chain("star_decision")[0].model == "glm-4.5-flash"
    assert router.chain("secondary")[0].model == "glm-4-flash"
    assert router.chain("embed")[0].provider == "local_embed"
    # enabled:false 占位跳（付费档/vLLM）被跳过
    assert all("glm-4.5-air" != h.model for h in router.chain("star_decision"))


def test_sighup_reload_valid_and_invalid(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """合法修改热生效；非法 YAML 保留旧配置并 WARN（04 §12.4）；SIGHUP 信号驱动 reload。"""
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    router = ModelRouter.load(path)
    router.install_sighup()
    try:
        assert signal.getsignal(signal.SIGHUP) is not signal.SIG_DFL
        # 合法修改：改 safety 的 temperature → 热生效
        modified = copy.deepcopy(cfg)
        modified["generation_params"]["safety"]["temperature"] = 0.1
        path.write_text(yaml.safe_dump(modified, allow_unicode=True), encoding="utf-8")
        os.kill(os.getpid(), signal.SIGHUP)
        assert router.gen_params("safety")["temperature"] == 0.1
        # 非法 YAML：保留旧配置 + WARN
        path.write_text("task_routes: [not a mapping", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="worldsim.llm_gateway.router"):
            assert router.reload() is False
        assert router.gen_params("safety")["temperature"] == 0.1
        assert any("保留旧配置" in r.message for r in caplog.records)
        # 结构非法（缺段）同口径
        path.write_text(yaml.safe_dump({"providers": {}}, allow_unicode=True), encoding="utf-8")
        assert router.reload() is False
        assert router.gen_params("safety")["temperature"] == 0.1
    finally:
        signal.signal(signal.SIGHUP, signal.SIG_DFL)


def test_validate_config_rejects_missing_world_copy() -> None:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    del cfg["task_routes"]["world_copy"]
    with pytest.raises(ConfigError):
        validate_config(cfg)
    cfg2 = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg2["task_routes"]["safety"]["fallback"] = ["bogus_action"]
    with pytest.raises(ConfigError):
        validate_config(cfg2)
    assert set(SPECIAL_ACTIONS) == {"pause_clock", "queue_retry", "pause_publish_channel"}
