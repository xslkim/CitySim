"""T-CFG-02 models.yaml 验收：路由表/生成参数/阈值/密钥纪律/embedding 维度对拍。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

SERVER_ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = SERVER_ROOT / "config" / "models.yaml"
SCHEMA_DDL_PATH = SERVER_ROOT / "ddl" / "schema_v1.sql"

# 04 §8.1 全部 9 个 task_type（含 world_copy）
TASK_TYPES_04_8_1 = {
    "star_decision", "dialogue", "reflection", "secondary", "bgsummary",
    "director", "world_copy", "safety", "embed",
}

# 04 §8.6 生成参数表 7 行（world_copy 无独立行）
GENERATION_PARAMS_04_8_6 = {
    "star_decision": {"temperature": 0.8, "max_tokens": 400, "response_format": "json"},
    "dialogue": {"temperature": 0.9, "max_tokens": 1200, "presence_penalty": 0.3},
    "secondary": {"temperature": 0.7, "max_tokens": 250, "response_format": "json"},
    "bgsummary": {"temperature": 0.3, "max_tokens": 300, "response_format": "json"},
    "reflection": {"temperature": 0.6, "max_tokens": 500},
    "director": {"temperature": 1.0, "max_tokens": 2000},
    "safety": {"temperature": 0.0, "max_tokens": 50},
}


def _load() -> dict:
    with MODELS_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_frozen_four_sections() -> None:
    assert set(_load().keys()) == {"providers", "task_routes", "generation_params", "thresholds"}


def test_task_types_cover_04_8_1() -> None:
    routes = _load()["task_routes"]
    assert set(routes.keys()) == TASK_TYPES_04_8_1
    for task, route in routes.items():
        assert "primary" in route and "provider" in route["primary"], task


def test_dev_routes_free_tier() -> None:
    """00 §1 A9 开发期路由落档：明星系=glm-4.5-flash、次要/摘要/安全=glm-4-flash、embed=local_embed。"""
    routes = _load()["task_routes"]
    for task in ("star_decision", "dialogue", "reflection", "director", "world_copy"):
        assert routes[task]["primary"] == {"provider": "zhipu", "model": "glm-4.5-flash"}, task
    for task in ("secondary", "bgsummary", "safety"):
        assert routes[task]["primary"] == {"provider": "zhipu", "model": "glm-4-flash"}, task
    assert routes["embed"]["primary"]["provider"] == "local_embed"


def test_generation_params_match_04_8_6() -> None:
    assert _load()["generation_params"] == GENERATION_PARAMS_04_8_6


def test_thresholds_match_design() -> None:
    th = _load()["thresholds"]
    assert th["retrieval_quota"] == {"N": 10, "M": 5, "K": 10}          # 04 §7.1
    assert th["rotation"] == {"promote_in": 0.62, "demote_out": 0.45}   # 04 §4.2
    assert th["reflection"]["importance_acc"] == 20                     # 04 §7.2
    assert th["cost_breaker"] == {"alarm_ratio": 1.5, "throttle_ratio": 2.0, "recover_ratio": 1.3}  # 源方案 §5.2（06 §3；恢复行 04 §8.4"回落 <1.3×"）
    assert th["intervention_rate_cap"] == 0.15                          # 源方案 §4.8（06 §3）


def test_no_hardcoded_key() -> None:
    raw = MODELS_PATH.read_text(encoding="utf-8")
    for i, line in enumerate(raw.splitlines(), 1):
        if "WSIM_ZHIPU_API_KEY" in line:
            assert "api_key_env" in line, f"第 {i} 行在 api_key_env 之外引用 key 变量"
    assert not re.search(r"[0-9a-f]{32}\.[A-Za-z0-9]+", raw), "文件含真实 key 值"


def test_embed_config_matches_ddl_vector_dim() -> None:
    """09 M0 E7：embed 主路由 = local_embed/bge-m3/1024，与 DDL vector(1024) 静态对拍（维度值从 yaml 读）。"""
    cfg = _load()
    embed_route = cfg["task_routes"]["embed"]["primary"]
    provider = cfg["providers"][embed_route["provider"]]
    assert embed_route["provider"] == "local_embed"
    assert provider["model"] == "BAAI/bge-m3"
    dims = provider["dimensions"]
    assert dims == 1024
    # 付费备选 embedding-3 必须 disabled
    (emb3,) = [e for e in cfg["providers"]["zhipu"]["embedding"] if e["id"] == "embedding-3"]
    assert emb3["enabled"] is False
    if not SCHEMA_DDL_PATH.exists():
        pytest.skip("ddl/schema_v1.sql 未落地（T-DB-01 归 M0b），DDL 文本对拍随其落地自动激活")
    ddl = SCHEMA_DDL_PATH.read_text(encoding="utf-8")
    assert f"vector({dims})" in ddl, "DDL 与 models.yaml 的 embedding 维度不一致"
