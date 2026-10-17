"""Mock 向量化适配器（离线默认）：确定性哈希伪向量，保证无网络/无 Key 时检索管线可跑通。

工程要点：
- 用 md5 而非内置 hash()：内置 hash 受 PYTHONHASHSEED 随机化影响，跨进程不稳定，
  会导致同一文本两次 upsert 进 Milvus 得到不同向量，索引逐步失真；
- hashing trick + 符号哈希 + L2 归一化：重叠 token 越多向量越近，具备"弱语义"，
  足以让"入库 → 检索能召回"这条链路在离线环境下被完整验证；
- 仅用于打通管线与测试；真实语义召回请切 GLMEmbedding。
"""

from __future__ import annotations

import hashlib
import math
import os

from ..ports import EmbeddingClient
from .retrieval_memory import tokenize

_DEFAULT_DIM = 256


def _embed_one(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    for tok in tokenize(text or ""):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:8], 16)
        idx = h % dim
        sign = 1.0 if (h // dim) % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class MockEmbedding(EmbeddingClient):
    name = "mock-hash-embedding"

    def __init__(self, dim: int | None = None):
        self.dim = int(dim or os.environ.get("EMBED_DIM", _DEFAULT_DIM))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_embed_one(t, self.dim) for t in texts]
