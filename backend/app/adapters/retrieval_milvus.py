"""Milvus 混合检索适配器（P1）：稠密向量语义召回 + 能力域/困境标量过滤。

设计要点：
- 存储用 Milvus Lite（本地文件，无需 Docker）；换服务端仅改 MILVUS_URI，代码不动；
- 主键用知识单元 id（VARCHAR，如 KU-LEAD-001），reload 走 upsert 保证幂等：
  重复入库不产生重复行，「入库即生效」不会把索引写脏；
- reload 额外删除已不在持久层的 id，保证索引与 JSONL 真相源强一致；
- 标签过滤用 ARRAY_CONTAINS，多标签之间取 or（命中任一即相关），
  与原 BM25 的标签加权语义保持一致，避免误用 and 把召回卡死；
- 过滤后不足 top_k 时用纯语义召回补齐，兼顾精确与召回；
- pymilvus lazy import：模块可安全导入，仅实例化时才需要向量依赖，
  保证默认 memory 模式（离线可跑）不受向量库影响。
"""

from __future__ import annotations

import os

from ..domain.models import KnowledgeUnit
from ..ports import EmbeddingClient, Retriever

_DEFAULT_URI = "milvus_kb.db"
_DEFAULT_COLLECTION = "kb_units"
_BATCH = 64


def _doc_text(u: KnowledgeUnit) -> str:
    """向量化输入文本：与稀疏侧（BM25）保持完全一致，避免稠密/稀疏两侧语义漂移。"""
    parts = [u.title, u.type.value, u.source.book, u.source.author]
    parts += u.key_points
    parts += [m.claim + m.correction for m in u.misconceptions]
    parts += u.applicable_context
    return " ".join(parts)


def _or_expr(field: str, values: list[str]) -> str | None:
    vals = [v for v in (values or []) if v]
    if not vals:
        return None
    return " or ".join(f'ARRAY_CONTAINS({field}, "{v}")' for v in vals)


class MilvusRetriever(Retriever):
    name = "milvus"

    def __init__(self, embedder: EmbeddingClient, units: list[KnowledgeUnit],
                 collection: str | None = None, uri: str | None = None):
        from pymilvus import MilvusClient, DataType  # lazy import：未安装时仅本处报错
        self._MilvusClient = MilvusClient
        self._DataType = DataType
        self._embedder = embedder
        self._collection = collection or os.environ.get("MILVUS_COLLECTION", _DEFAULT_COLLECTION)
        # Milvus Lite 要求 URI 用正斜杠，Windows 反斜杠路径会导致建库失败
        self._uri = (uri or os.environ.get("MILVUS_URI") or _DEFAULT_URI).replace("\\", "/")
        self._c = MilvusClient(uri=self._uri)
        self.last_error: str | None = None
        self._units: dict[str, KnowledgeUnit] = {}
        self._ensure_collection()
        self.reload(units)

    # ---------------------------------------------------------- 集合管理
    def _ensure_collection(self) -> None:
        dim = self._embedder.dim
        if self._c.has_collection(self._collection):
            # embedding 模型更换会导致向量维度变化，旧集合不兼容 → 重建
            cur = self._vector_dim()
            if cur is None or cur == dim:
                return
            self._c.drop_collection(self._collection)
        dt = self._DataType
        schema = self._MilvusClient.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field(field_name="id", datatype=dt.VARCHAR, max_length=64, is_primary=True)
        schema.add_field(field_name="vector", datatype=dt.FLOAT_VECTOR, dim=dim)
        schema.add_field(field_name="domains", datatype=dt.ARRAY,
                         element_type=dt.VARCHAR, max_capacity=16, max_length=32)
        schema.add_field(field_name="dilemmas", datatype=dt.ARRAY,
                         element_type=dt.VARCHAR, max_capacity=16, max_length=32)
        idx = self._MilvusClient.prepare_index_params()
        idx.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        self._c.create_collection(collection_name=self._collection,
                                  schema=schema, index_params=idx)

    def _vector_dim(self) -> int | None:
        try:
            info = self._c.describe_collection(self._collection)
            for f in (info.get("fields") or []):
                if f.get("name") == "vector":
                    return (f.get("params") or {}).get("dim")
        except Exception:
            return None
        return None

    def _all_ids(self) -> set[str]:
        try:
            rows = self._c.query(collection_name=self._collection,
                                 filter='id != ""', output_fields=["id"])
            return {r["id"] for r in rows}
        except Exception:
            return set()

    # ---------------------------------------------------------- 索引维护
    def reload(self, units: list[KnowledgeUnit]) -> None:
        """入库后重建索引：upsert 全部（幂等）+ 删除持久层已移除的 id。

        upsert 保证「入库即生效」不会因重复 reload 写脏；
        删除保证索引与 JSONL 真相源强一致，不会残留已删单元。
        """
        self._units = {u.id: u for u in units}
        if not units:
            stale = self._all_ids()
            if stale:
                self._c.delete(collection_name=self._collection, ids=sorted(stale))
            return
        vecs = self._embedder.embed([_doc_text(u) for u in units])
        rows = [{"id": u.id, "vector": v,
                 "domains": list(u.domains), "dilemmas": list(u.dilemmas)}
                for u, v in zip(units, vecs)]
        for i in range(0, len(rows), _BATCH):
            self._c.upsert(collection_name=self._collection, data=rows[i:i + _BATCH])
        stale = self._all_ids() - {u.id for u in units}
        if stale:
            self._c.delete(collection_name=self._collection, ids=sorted(stale))
        self.last_error = None

    # ---------------------------------------------------------- 检索
    def search(self, query: str, domains: list[str] | None = None,
               dilemmas: list[str] | None = None, top_k: int = 5) -> list[KnowledgeUnit]:
        if not query or not self._units:
            return []
        try:
            vec = self._embedder.embed([query])[0]
        except Exception as exc:
            self.last_error = f"embed failed: {type(exc).__name__}: {exc}"[:300]
            return []
        expr = self._tag_expr(domains, dilemmas)
        ordered: list[str] = []
        seen: set[str] = set()

        def _run(filter_expr: str | None) -> None:
            try:
                res = self._c.search(collection_name=self._collection, data=[vec],
                                     limit=top_k, filter=filter_expr)
            except Exception as exc:
                self.last_error = f"search failed: {type(exc).__name__}: {exc}"[:300]
                return
            for row in (res[0] if res else []):
                uid = row["id"]
                if uid not in seen:
                    seen.add(uid)
                    ordered.append(uid)

        if expr:
            _run(expr)
        if len(ordered) < top_k:
            _run(None)  # 过滤结果不足时用纯语义召回补齐
        return [self._units[i] for i in ordered[:top_k] if i in self._units]

    def dual_recall(self, domains: list[str], dilemmas: list[str],
                    top_k: int = 6) -> list[KnowledgeUnit]:
        """「能力域 × 困境」双路召回：优先双命中，其次困境，最后能力域。

        纯标签过滤，不需要向量，与 MemoryRetriever 的排序语义保持一致。
        """
        if not self._units:
            return []
        dm = _or_expr("domains", domains)
        dl = _or_expr("dilemmas", dilemmas)
        ordered: list[str] = []
        seen: set[str] = set()

        def _add(expr: str | None) -> None:
            if not expr or len(ordered) >= top_k:
                return
            try:
                rows = self._c.query(collection_name=self._collection, filter=expr,
                                     output_fields=["id"], limit=top_k)
            except Exception as exc:
                self.last_error = f"query failed: {type(exc).__name__}: {exc}"[:300]
                return
            for r in rows:
                uid = r["id"]
                if uid not in seen:
                    seen.add(uid)
                    ordered.append(uid)

        if dm and dl:
            _add(f"({dm}) and ({dl})")
        _add(dl)
        _add(dm)
        return [self._units[i] for i in ordered[:top_k] if i in self._units]

    @property
    def units(self) -> dict[str, KnowledgeUnit]:
        """与 MemoryRetriever.units 同名的公开访问（healthz 与统计口径一致）。"""
        return self._units

    def by_ids(self, ids: list[str]) -> list[KnowledgeUnit]:
        return [self._units[i] for i in ids if i in self._units]

    @staticmethod
    def _tag_expr(domains: list[str] | None, dilemmas: list[str] | None) -> str | None:
        dm_e = _or_expr("domains", domains or [])
        dl_e = _or_expr("dilemmas", dilemmas or [])
        parts = [e for e in (dm_e, dl_e) if e]
        return " or ".join(parts) if parts else None
