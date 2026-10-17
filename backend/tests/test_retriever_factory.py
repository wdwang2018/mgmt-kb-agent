"""检索适配器工厂测试：默认 memory（离线可跑）+ milvus 失败自动降级。

本文件刻意不依赖 pymilvus：验证的正是"向量库不可用时系统仍然可用"这条底线，
因此在纯离线环境下也必须能跑。
"""

from __future__ import annotations

from app.adapters import retriever_factory as rf


def test_default_mode_is_memory(monkeypatch):
    """未配置时走 memory，零向量依赖，离线可跑。"""
    monkeypatch.delenv("VECTOR_MODE", raising=False)
    r, mode, note = rf.get_retriever([])
    assert mode == "memory"
    assert note is None
    assert r is not None


def test_degrades_to_memory_when_milvus_broken(monkeypatch):
    """milvus 构造失败时降级为 memory 并带回原因，服务仍然可用。"""
    monkeypatch.setenv("VECTOR_MODE", "milvus")

    def boom(*_a, **_k):
        raise RuntimeError("milvus boom")

    monkeypatch.setattr("app.adapters.retrieval_milvus.MilvusRetriever", boom)
    r, mode, note = rf.get_retriever([])
    assert mode == "memory", "milvus 不可用时应降级为 memory"
    assert note and "boom" in note, "降级原因必须可观测，避免静默退化"
    assert r is not None


def test_degrade_keeps_retriever_usable(monkeypatch):
    """降级后的 retriever 仍满足 Retriever 契约（检索/标签召回不报错）。"""
    monkeypatch.setenv("VECTOR_MODE", "milvus")

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.adapters.retrieval_milvus.MilvusRetriever", boom)
    r, _mode, _note = rf.get_retriever([])
    assert r.search("授权", domains=["LEAD"], top_k=3) == []
    assert r.dual_recall(["LEAD"], [], top_k=3) == []
    assert r.by_ids(["KU-LEAD-001"]) == []
