"""混合检索适配器：BM25 + 稠密向量双路召回，RRF 融合。

设计依据（2026-10-17 实测，勿凭印象推翻）：
- 黄金集查询含精确专有名词（GROW/SBI/VRIO/OGSM），BM25 MRR 1.000，稠密向量零增益；
- 语义改写查询（用户真实口吻）BM25 MRR 0.650，稠密向量 0.875，向量显著占优；
- 代价：向量单次查询约 260ms（在线 embedding 往返），BM25 约 0.5ms。

两条路线失败模式互补 → 结论是混合而非单质切换。融合用 RRF（Reciprocal Rank
Fusion）：score(d) = Σ w_i / (k + rank_i)。RRF 只用排名不用原始分数，天然免疫
BM25 与 COSINE 两种量纲不可比的问题。

**权重必须偏向稠密侧，这是实测结论，不是拍脑袋**（2026-10-17，见
eval/retrieval-bench-2026-10-17.md）：
- 等权（wb:wd = 1:1）时 B 组 MRR 仅 0.833，**低于纯向量的 0.875**——原因是 BM25
  会把语义相近但非目标的单元顶到前列，等权融合反而被这种"伪关键词命中"拖累；
- 稠密侧权重 ≥1.5 后 B 组 MRR 升至 **0.917**，同时优于纯 BM25（0.650）与纯向量
  （0.875），且 A 组（专有名词查询）维持 1.000 不受影响；
- 1.5~3+ 为效果平台期，默认取 2.0 居中；A 组不受权重影响，故该权重无回退风险。

降级策略：向量侧构造失败或运行时报错时，自动退回纯 BM25 并记录原因，不拖垮主链路。
"""

from __future__ import annotations

import os


from ..domain.models import KnowledgeUnit
from ..ports import Retriever

_DEFAULT_RRF_K = 60
_DEFAULT_DENSE_WEIGHT = 2.0


def _rrf_scores(rankings: list[tuple[list[str], float]], k: int) -> dict[str, float]:
    """RRF 融合：rank 从 1 开始计；同一文档在多路出现则分数累加（天然提权重）。"""
    scores: dict[str, float] = {}
    for ranking, weight in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
    return scores


class HybridRetriever(Retriever):
    """双路召回 + RRF 融合（稠密侧加权）。

    - `bm25`：MemoryRetriever，永远可用（纯内存，无外部依赖）
    - `dense`：MilvusRetriever（稠密向量），可为 None（此时退化为纯 BM25）
    - `dense_weight`：稠密侧权重，默认 2.0（实测依据见模块 docstring）
    """

    def __init__(self, bm25, dense=None, rrf_k: int = _DEFAULT_RRF_K,
                 dense_weight: float = _DEFAULT_DENSE_WEIGHT):
        self._bm25 = bm25
        self._dense = dense
        self._rrf_k = int(rrf_k)
        self._dense_weight = float(dense_weight)
        self.degrade_note: str | None = None
        if dense is None:
            self.degrade_note = "向量侧不可用，已退化为纯 BM25 检索"

    @classmethod
    def from_env(cls, bm25, dense=None) -> "HybridRetriever":
        """按 RRF_K / RRF_DENSE_WEIGHT 环境变量构造（便于调参而不改代码）。"""
        k = os.environ.get("RRF_K")
        w = os.environ.get("RRF_DENSE_WEIGHT")
        return cls(bm25, dense,
                   rrf_k=int(k) if k else _DEFAULT_RRF_K,
                   dense_weight=float(w) if w else _DEFAULT_DENSE_WEIGHT)

    # ---------------------------------------------------------- 基础属性
    @property
    def units(self) -> dict[str, KnowledgeUnit]:
        """供健康检查与调用方读取当前索引的知识单元（以 BM25 侧为准）。"""
        return self._bm25.units

    @property
    def last_error(self) -> str | None:
        return getattr(self._dense, "last_error", None)

    @property
    def dense_available(self) -> bool:
        return self._dense is not None

    # ---------------------------------------------------------- 索引维护
    def reload(self, units: list[KnowledgeUnit]) -> None:
        """入库后重建：两路都重建；向量侧失败不影响 BM25 侧生效。"""
        self._bm25.reload(units)
        if self._dense is not None:
            try:
                self._dense.reload(units)
            except Exception as exc:  # 向量侧故障不阻断入库即生效
                self.degrade_note = f"向量侧 reload 失败，已退回 BM25：{type(exc).__name__}: {exc}"[:300]
                self._dense = None

    # ---------------------------------------------------------- 检索
    def search(self, query: str, domains: list[str] | None = None,
               dilemmas: list[str] | None = None, top_k: int = 5) -> list[KnowledgeUnit]:
        if not query or not self.units:
            return []
        # 召回深度放宽：给 RRF 留足候选，否则融合无从发生
        depth = max(top_k * 2, top_k + 4)
        # RRF 只认文档 id，需把两路返回的 KnowledgeUnit 对象转成 id 序列
        rankings: list[list[str]] = [[u.id for u in
                                     self._bm25.search(query, domains, dilemmas, top_k=depth)]]
        if self._dense is not None:
            try:
                rankings.append([u.id for u in
                                 self._dense.search(query, domains, dilemmas, top_k=depth)])
            except Exception as exc:
                self.degrade_note = f"向量侧检索失败，本次已退回 BM25：{type(exc).__name__}: {exc}"[:300]
        fused = self._fuse(rankings, top_k)
        return [self._bm25.units[i] for i in fused if i in self._bm25.units]

    def dual_recall(self, domains: list[str], dilemmas: list[str],
                    top_k: int = 6) -> list[KnowledgeUnit]:
        if not self.units:
            return []
        depth = max(top_k * 2, top_k + 4)
        rankings: list[list[str]] = [[u.id for u in
                                     self._bm25.dual_recall(domains, dilemmas, top_k=depth)]]
        if self._dense is not None:
            try:
                rankings.append([u.id for u in
                                 self._dense.dual_recall(domains, dilemmas, top_k=depth)])
            except Exception as exc:
                self.degrade_note = f"向量侧双路召回失败，本次已退回 BM25：{type(exc).__name__}: {exc}"[:300]
        fused = self._fuse(rankings, top_k)
        return [self._bm25.units[i] for i in fused if i in self._bm25.units]

    def _fuse(self, rankings: list[list[str]], top_k: int) -> list[str]:
        """RRF 融合；rankings 顺序为 [bm25, dense]，按 dense_weight 加权。"""
        live = [r for r in rankings if r]
        if not live:
            return []
        weighted: list[tuple[list[str], float]] = [(live[0], 1.0)]
        for extra in live[1:]:
            weighted.append((extra, self._dense_weight))
        scores = _rrf_scores(weighted, self._rrf_k)
        # 排序键含 id，保证同分时结果确定（可复现）
        ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [doc_id for doc_id, _ in ordered[:top_k]]

    def by_ids(self, ids: list[str]) -> list[KnowledgeUnit]:
        return self._bm25.by_ids(ids)
