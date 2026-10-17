"""智谱（GLM）向量化适配器（在线）。

配置（环境变量）：
- EMBED_MODE=glm 时启用
- GLM_API_KEY（别名 ZHIPU_API_KEY）缺失时自动退回 Mock 伪向量，保证检索管线仍可跑
- GLM_BASE_URL 默认 https://open.bigmodel.cn/api/paas/v4
- EMBED_MODEL 默认 embedding-3
- EMBED_DIM 必须与所选模型的实际输出维度一致（embedding-3 默认 2048）

工程约束（沿用 llm_glm 约定）：任何网络 / 限流 / 鉴权 / 维度异常都不向外抛出，
一律退回确定性伪向量，保证「入库即索引」链路不会因向量服务抖动而中断；
真实原因记录在 last_error 供 healthz 与日志排查，避免把配置错误误判为检索故障。
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from ..ports import EmbeddingClient
from .embedding_mock import MockEmbedding

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (2.0, 5.0)


class GLMEmbedding(EmbeddingClient):
    name = "glm-embedding"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, dim: int | None = None, timeout: float = 30.0):
        self.api_key = api_key or os.environ.get("GLM_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
        self.base_url = (base_url or os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")).rstrip("/")
        self.model = model or os.environ.get("EMBED_MODEL", "embedding-3")
        self.dim = int(dim or os.environ.get("EMBED_DIM", 2048))
        self.name = f"glm-{self.model}"
        self.timeout = timeout
        self._fallback = MockEmbedding(dim=self.dim)
        self.last_error: str | None = None

    def _call(self, texts: list[str]) -> list[list[float]] | None:
        """调用在线 embedding；任何失败返回 None，由 embed() 兜底。"""
        if not self.api_key:
            self.last_error = "missing GLM_API_KEY"
            return None
        payload: dict[str, Any] = {"model": self.model, "input": texts}
        for attempt in range(_MAX_ATTEMPTS):
            retryable = False
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        f"{self.base_url}/embeddings",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                    # 429 限流 / 5xx 服务端错误可重试；其他 4xx（鉴权/参数）重试无意义
                    if resp.status_code == 429 or resp.status_code >= 500:
                        retryable = True
                        raise httpx.HTTPStatusError(
                            f"retryable status {resp.status_code}",
                            request=resp.request, response=resp)
                    resp.raise_for_status()
                    data = resp.json()
                rows = data.get("data") or []
                vecs = [r["embedding"] for r in rows]
                if len(vecs) != len(texts):
                    self.last_error = f"count mismatch {len(vecs)}!={len(texts)}"
                    return None
                # 维度必须稳定：Milvus 集合按 dim 建，不一致会导致 upsert 失败
                if any(len(v) != self.dim for v in vecs):
                    self.last_error = f"dim mismatch: declared {self.dim} vs got {len(vecs[0]) if vecs else 0}"
                    return None
                self.last_error = None
                return vecs
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                if retryable and attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                return None
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._call(texts)
        if vecs is not None:
            return vecs
        return self._fallback.embed(texts)
