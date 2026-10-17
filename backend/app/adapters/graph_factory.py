"""图存储工厂：按 GRAPH_MODE 创建 GraphStore，失败自动降级为「无图」并记原因。

- none（默认）：不建图，GraphRAG 查询全部返回空——保证零依赖可跑；
- kuzu：嵌入式图库（本地文件、无服务端），win_amd64 wheel 可用；
- neo4j：服务端版，需要 neo4j driver + Neo4j 服务端（bolt 7687）。

降级策略与向量层一致：可用性优先，降级原因经 healthz 暴露，
避免「以为开着图查询、其实已静默关闭」的假象。
"""

from __future__ import annotations

import os

from ..domain.models import KnowledgeUnit
from ..ports import GraphStore

_MODE_NONE = "none"


def build_graph_store(units: list[KnowledgeUnit] | None = None
                      ) -> tuple[GraphStore | None, str, str | None]:
    """返回 (graph_store, mode, note)。graph_store 为 None 表示图查询关闭。"""
    mode = (os.environ.get("GRAPH_MODE") or _MODE_NONE).strip().lower()
    units = units or []

    if mode == _MODE_NONE:
        return None, _MODE_NONE, None

    if mode == "kuzu":
        try:
            from .graph_kuzu import KuzuGraphStore
            store = KuzuGraphStore()
            store.rebuild(units)  # 启动即灌入现有单元（MERGE 幂等，可重复执行）
            return store, "kuzu", None
        except Exception as exc:
            return None, _MODE_NONE, f"GRAPH_MODE=kuzu 构造失败，图查询已关闭：{exc}"[:300]

    if mode == "neo4j":
        try:
            from .graph_neo4j import Neo4jGraphStore
            store = Neo4jGraphStore()
            store.rebuild(units)
            return store, "neo4j", None
        except Exception as exc:
            return None, _MODE_NONE, f"GRAPH_MODE=neo4j 构造失败，图查询已关闭：{exc}"[:300]

    return None, _MODE_NONE, f"未知 GRAPH_MODE={mode}（合法值 none/kuzu/neo4j），图查询已关闭"


def build_metadata_store(knowledge_dir, review_log_dir=None) -> tuple[object, str, str | None]:
    """返回 (store, mode, note)。

    - json（默认）：JsonKnowledgeStore，JSONL 文件，行为与 P0/P1 完全一致；
    - pg：PgMetadataStore，连接失败自动降级 json 并记原因。

    review_log_dir：HITL 台账落点。不传则按 JsonKnowledgeStore 默认建在
    knowledge/reviews（与 P0 一致）；组合根传 output_dir/reviews 避免污染知识目录。
    """
    from .kb_json import JsonKnowledgeStore

    mode = (os.environ.get("STORE_MODE") or "json").strip().lower()

    def json_store(note: str | None = None):
        return JsonKnowledgeStore(knowledge_dir, review_log_dir=review_log_dir), "json", note

    if mode == "json":
        return json_store()
    if mode == "pg":
        try:
            from .metadata_pg import PgMetadataStore
            return PgMetadataStore(), "pg", None
        except Exception as exc:
            return json_store(f"STORE_MODE=pg 连接失败，已降级 JSONL：{exc}"[:300])
    return json_store(f"未知 STORE_MODE={mode}（合法值 json/pg），已使用 JSONL")
