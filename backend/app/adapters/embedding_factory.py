"""适配器工厂：按环境变量选择 Embedding 实现（默认 mock，保证离线可跑）。"""

from __future__ import annotations

import os

from ..ports import EmbeddingClient
from .embedding_glm import GLMEmbedding
from .embedding_mock import MockEmbedding


def get_embedding() -> EmbeddingClient:
    mode = os.environ.get("EMBED_MODE", "mock").lower()
    if mode == "glm":
        return GLMEmbedding()
    return MockEmbedding()
