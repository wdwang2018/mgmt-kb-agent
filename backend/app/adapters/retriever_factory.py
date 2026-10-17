"""适配器工厂：按环境变量选择检索实现。

VECTOR_MODE 取值：
  memory（默认）：纯 BM25 内存检索，零向量依赖，离线可跑；
  milvus：纯稠密向量语义召回（Milvus Lite 本地文件）；
  hybrid：BM25 + 稠密向量双路召回，RRF 融合（2026-10-17 实测推荐，
          依据见 eval/retrieval-bench-2026-10-17.md）。

milvus / hybrid 模式若向量侧构造失败（pymilvus 缺失、集合创建失败、embedding
维度异常），自动降级为 memory 并返回降级原因，保证服务可用性优先于技术形式；
原因由调用方写入 AppContext.vector_note 并经 healthz 暴露，避免"以为在用向量
检索、其实已静默退化"的假象。
"""

from __future__ import annotations

import os

from ..domain.models import KnowledgeUnit
from ..ports import EmbeddingClient, Retriever
from .embedding_factory import get_embedding
from .retrieval_memory import MemoryRetriever


def get_retriever(units: list[KnowledgeUnit],
                  embedder: EmbeddingClient | None = None,
                  uri: str | None = None) -> tuple[Retriever, str, str | None]:
    """返回 (retriever, 实际生效模式, 降级原因)；未降级时原因为 None。"""
    mode = os.environ.get("VECTOR_MODE", "memory").lower()
    if mode not in ("milvus", "hybrid"):
        return MemoryRetriever(units), "memory", None

    try:
        from .retrieval_milvus import MilvusRetriever
        emb = embedder or get_embedding()
        dense = MilvusRetriever(embedder=emb, units=units, uri=uri)
    except Exception as exc:  # 向量依赖不可用时不影响主流程
        reason = f"milvus 不可用已降级：{type(exc).__name__}: {exc}"[:300]
        return MemoryRetriever(units), "memory", reason

    if mode == "milvus":
        return dense, "milvus", None

    from .retrieval_hybrid import HybridRetriever
    return HybridRetriever.from_env(MemoryRetriever(units), dense), "hybrid", None
