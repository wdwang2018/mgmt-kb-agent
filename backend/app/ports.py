"""端口定义：LLM / 检索 / 知识库三类外部能力的协议。

架构原则（端口-适配器）：
- 领域层与图编排层只依赖本文件定义的 Protocol，不 import 任何具体适配器；
- P0 提供 Memory/JSON/Mock 适配器离线可跑；P1 换 Milvus/Neo4j/DeepSeek 时只换适配器不动图。
"""

from __future__ import annotations

from typing import Any, Protocol

from .domain.models import KnowledgeUnit, TrainingNeed


class LLMClient(Protocol):
    """大模型客户端协议。complete_json 用于结构化产出，complete 用于自由文本。"""

    name: str

    def complete_json(self, purpose: str, system: str, user: str, fallback: dict[str, Any]) -> dict[str, Any]:
        """按 purpose 产出结构化 JSON；失败时由适配器返回 fallback（调用方保证 fallback 合法）。"""
        ...

    def complete(self, purpose: str, system: str, user: str) -> str:
        """自由文本生成。"""
        ...


class Retriever(Protocol):
    """混合检索协议：标签过滤 + 关键词/向量召回，P0 内存实现，P1 切 Milvus。"""

    def search(self, query: str, domains: list[str] | None = None,
               dilemmas: list[str] | None = None, top_k: int = 5) -> list[KnowledgeUnit]:
        ...

    def by_ids(self, ids: list[str]) -> list[KnowledgeUnit]:
        ...

    def reload(self, units: list[KnowledgeUnit]) -> None:
        """（重）建索引：入库后由 AppContext.reindex 调用，保证新单元即时可检索且幂等。"""
        ...

    def dual_recall(self, domains: list[str], dilemmas: list[str],
                    top_k: int = 6) -> list[KnowledgeUnit]:
        """按「能力域 × 困境」双路召回：优先双命中，其次单路命中补齐。"""
        ...


class EmbeddingClient(Protocol):
    """文本向量化协议：默认 Mock（确定性伪向量，离线可跑），Milvus 模式换 GLMEmbedding。"""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量文本向量化；网络/鉴权失败时由适配器返回确定性兜底向量，不抛异常。"""
        ...


class KnowledgeStore(Protocol):
    """知识库存储协议：默认 JSONL 文件，STORE_MODE=pg 切 PostgreSQL。"""

    def load_units(self) -> list[KnowledgeUnit]:
        ...

    def load_cases(self) -> list[dict[str, Any]]:
        """加载 L3 案例卡（场景资产层）。"""
        ...

    def save_unit(self, unit: KnowledgeUnit) -> None:
        ...

    def log_review(self, run_id: str, action: str, payload: dict[str, Any]) -> None:
        """记录人工审核动作（HITL 台账，修正即重索引的触发源）。"""
        ...


class GraphStore(Protocol):
    """知识图谱协议：前置链 / 提出者 / 争议观点 / 案例关联查询。

    数据来源是知识单元自带的 relations / prerequisites / related 字段，
    入库时由 sync_unit 同步建边，无需改知识模型。

    适配器：
    - KuzuGraphStore：嵌入式图库（本地文件、无服务端），GRAPH_MODE=kuzu，默认 none；
    - Neo4jGraphStore：服务端版，GRAPH_MODE=neo4j，lazy import，连不上由工厂降级。
    """

    name: str

    def sync_unit(self, unit: KnowledgeUnit) -> None:
        """把单元及其关系写入图（MERGE 幂等，重复调用不写脏）。"""
        ...

    def rebuild(self, units: list[KnowledgeUnit]) -> None:
        """全量重建图：删除陈旧 KU 节点后逐个 MERGE（差值同步）。"""
        ...

    def prerequisites(self, unit_id: str, depth: int = 3) -> list[str]:
        """前置概念链（多跳遍历），返回祖先单元 id，近者在前。"""
        ...

    def proposers(self, unit_id: str) -> list[str]:
        """理论提出者（relations 中 type=提出者）。"""
        ...

    def contested_views(self, unit_id: str) -> list[str]:
        """争议/对立观点（relations 中 type=争议）——证据分级与学派争议的图查询入口。"""
        ...

    def applied_cases(self, unit_id: str) -> list[str]:
        """关联案例卡（relations 中 type=应用 + related）。"""
        ...

    def neighbors(self, unit_id: str) -> dict[str, list[str]]:
        """全部邻居，按关系类型分组（如 {"PREREQUISITE": [...], "PROPOSED_BY": [...]}）。"""
        ...

    def stats(self) -> dict[str, int]:
        """图规模统计（ku_nodes / person_nodes / view_nodes / case_nodes / relations）。"""
        ...


__all__ = ["LLMClient", "Retriever", "EmbeddingClient", "KnowledgeStore",
           "GraphStore", "TrainingNeed"]
