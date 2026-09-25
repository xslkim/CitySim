"""T-LLM-03 本地 bge-m3 embedding 冒烟（03 验收 3 = 09 M2 E1 同口径）。

真实加载 BAAI/bge-m3（sentence-transformers，懒加载 + CPU 兜底），断言输出维度 = 1024
（04 §5.2 `memories.embedding vector(1024)`）。权重经 HF 镜像缓存（HF_ENDPOINT=https://hf-mirror.com）；
无网/未装 sentence-transformers 时 skip 并给显式原因。
"""

from __future__ import annotations

import math

import pytest

from worldsim.llm_gateway.providers.base import EMBED_DIM
from worldsim.llm_gateway.providers.local_embed import LocalEmbedProvider

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def provider() -> LocalEmbedProvider:
    pytest.importorskip("sentence_transformers", reason="sentence-transformers 未安装（uv sync 后可用）")
    return LocalEmbedProvider(device="cpu")  # 冒烟走 CPU 兜底档（00 §1 A10；GPU 与图像生成错峰）


async def test_bge_m3_loads_and_dim_1024(provider: LocalEmbedProvider) -> None:
    """bge-m3 本地加载冒烟：输出维度 = 1024（09 M2 E1）。"""
    r = await provider.embed(["今天在公司加班到很晚", "周末约朋友吃饭"])
    assert r.dimensions == EMBED_DIM == 1024
    assert len(r.vectors) == 2 and all(len(v) == 1024 for v in r.vectors)
    assert r.provider == "local_embed" and r.model == "BAAI/bge-m3"
    assert r.latency_ms >= 0 and r.request_id


async def test_bge_m3_semantic_direction(provider: LocalEmbedProvider) -> None:
    """语义 sanity：近义文本余弦相似度显著高于无关文本（防"维度对但模型错"）。"""
    r = await provider.embed(["我饿了想吃饭", "肚子饿了去吃点东西", "量子引力波干涉仪"])
    a, b, c = r.vectors
    sim_ab = sum(x * y for x, y in zip(a, b))
    sim_ac = sum(x * y for x, y in zip(a, c))
    assert sim_ab > sim_ac
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-3)  # 单位向量
