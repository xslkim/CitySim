"""本地 bge-m3 embedding provider（03 文档 T-LLM-03；00 §1 A10 定案，04 §5.2 vector(1024)）。

- sentence-transformers 封装 `BAAI/bge-m3`，输出维度 = 1024，与 DDL `memories.embedding vector(1024)`
  对拍（04 §5.2；09 M2 E1 冒烟口径）。
- 懒加载（首次 embed 才载入权重）；GPU 可用则 GPU、不可用自动 CPU 兜底（00 §1 A10：
  GPU 与图像生成错峰，CPU 可兜底）。device 可显式指定（`"cpu"`/`"cuda"`）。
- 权重下载走 HF 镜像：调用侧设 `HF_ENDPOINT=https://hf-mirror.com`（00 §1 附开发期口径）。
- 本地模型计量记 0 成本但记耗时（04 §8.3 归账口径），由 ledger（T-LLM-07）统一归账。
- 推理为同步阻塞调用，经 `asyncio.to_thread` 让出事件循环（网关并发不受阻塞）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from .base import EMBED_DIM, EmbedResult

DEFAULT_MODEL = "BAAI/bge-m3"


class EmbedDimensionMismatch(RuntimeError):
    """加载的模型输出维度 ≠ EMBED_DIM（04 §5.2 vector(1024) 契约）。"""


class LocalEmbedProvider:
    """本地 bge-m3 embed provider（sentence-transformers 封装，懒加载 + CPU 兜底）。"""

    name = "local_embed"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        dimensions: int = EMBED_DIM,
        device: str | None = None,
        encode_batch_size: int = 32,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self.device = device
        self._batch_size = encode_batch_size
        self._st: Any = None  # SentenceTransformer 实例（懒加载）

    def _load(self) -> Any:
        if self._st is None:
            import torch  # 延迟 import：未装 torch 的环境仍可 import 本模块（单测 MockTransport 路径）
            from sentence_transformers import SentenceTransformer

            device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
            try:
                self._st = SentenceTransformer(self.model, device=device)
            except Exception:
                if device == "cpu":
                    raise
                # GPU 加载失败（如显存被图像生成占满）→ CPU 兜底（00 §1 A10）
                self._st = SentenceTransformer(self.model, device="cpu")
        return self._st

    def _encode(self, texts: list[str]) -> list[list[float]]:
        st = self._load()
        vectors = st.encode(
            texts, batch_size=self._batch_size, normalize_embeddings=True,
        )
        out = [list(map(float, row)) for row in vectors]
        for row in out:
            if len(row) != self.dimensions:
                raise EmbedDimensionMismatch(
                    f"{self.model} 输出维度 {len(row)} ≠ {self.dimensions}（04 §5.2 vector(1024)）"
                )
        return out

    async def embed(self, texts: list[str], *, seed: int | None = None) -> EmbedResult:
        started = time.perf_counter()
        vectors = await asyncio.to_thread(self._encode, texts)
        return EmbedResult(
            vectors=vectors,
            prompt_tokens=sum(max(1, len(t)) for t in texts),  # 字符数折算（本地模型无 tokenizer 计量口径）
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_id=f"local-embed-{uuid.uuid4().hex[:16]}",
            provider=self.name,
            model=self.model,
            dimensions=self.dimensions,
        )
