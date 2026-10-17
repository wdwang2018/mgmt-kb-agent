"""图/元数据工厂测试：GRAPH_MODE 与 STORE_MODE 的切换与降级路径。

这些测试不依赖任何服务端（kuzu 除外）——验证的正是
「基础设施不可用时系统仍可用」的降级契约。
"""

from __future__ import annotations

import os

import pytest

from app.adapters.graph_factory import build_graph_store, build_metadata_store


@pytest.fixture(autouse=True)
def restore_environ():
    """快照并还原环境变量，杜绝 GRAPH_MODE/STORE_MODE 跨测试泄漏。"""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


# ---------------------------------------------------------------- 图工厂
def test_graph_mode_none_default(monkeypatch):
    """默认 GRAPH_MODE=none：不建图，note 为 None。"""
    monkeypatch.delenv("GRAPH_MODE", raising=False)
    store, mode, note = build_graph_store([])
    assert store is None and mode == "none" and note is None


def test_graph_mode_kuzu_builds(monkeypatch, tmp_path):
    """GRAPH_MODE=kuzu：真建图并灌入单元（kuzu 已装则验证，未装则验降级）。"""
    pytest.importorskip("kuzu")
    monkeypatch.setenv("GRAPH_MODE", "kuzu")
    monkeypatch.setenv("GRAPH_DB_PATH", str(tmp_path / "factory.kuzu"))
    from app.domain.models import (EvidenceLevel, KnowledgeUnit, KUType, SourceRef)
    u = KnowledgeUnit(id="KU-LEAD-001", title="甲", type=KUType.MODEL, domains=["LEAD"],
                      dilemmas=["D01"], evidence_level=EvidenceLevel.B,
                      key_points=["要点一", "要点二"],
                      source=SourceRef(book="书", author="者", locator="第1章",
                                       copyright_status="要点化"))
    store, mode, note = build_graph_store([u])
    assert mode == "kuzu" and store is not None and note is None
    assert store.stats()["ku_nodes"] == 1


def test_graph_mode_unknown_falls_back(monkeypatch):
    """未知 GRAPH_MODE：关闭图查询并带明确 note（不崩溃）。"""
    monkeypatch.setenv("GRAPH_MODE", "bogus")
    store, mode, note = build_graph_store([])
    assert store is None and mode == "none"
    assert note and "bogus" in note


def test_graph_mode_neo4j_without_server_falls_back(monkeypatch):
    """GRAPH_MODE=neo4j 但无 driver/服务端：降级关闭并记原因（不崩溃）。"""
    monkeypatch.setenv("GRAPH_MODE", "neo4j")
    store, mode, note = build_graph_store([])
    assert store is None and mode == "none"
    assert note and ("neo4j" in note.lower() or "GRAPH" in note)


# ---------------------------------------------------------------- 元数据工厂
def test_store_mode_json_default(monkeypatch, tmp_path):
    """默认 STORE_MODE=json：JsonKnowledgeStore，行为与 P0/P1 一致。"""
    monkeypatch.delenv("STORE_MODE", raising=False)
    k = tmp_path / "kb"
    (k / "units").mkdir(parents=True)
    store, mode, note = build_metadata_store(k, review_log_dir=tmp_path / "rev")
    assert mode == "json" and note is None
    assert store.load_units() == [] and store.load_cases() == []


def test_store_mode_pg_without_server_falls_back(monkeypatch, tmp_path):
    """STORE_MODE=pg 但无 PG 服务端：自动降级 JSONL 并记原因。"""
    monkeypatch.setenv("STORE_MODE", "pg")
    monkeypatch.setenv("PG_DSN", "postgresql://postgres:x@127.0.0.1:59999/none")
    k = tmp_path / "kb"
    (k / "units").mkdir(parents=True)
    store, mode, note = build_metadata_store(k, review_log_dir=tmp_path / "rev")
    assert mode == "json"  # 降级
    assert note and "pg" in note.lower()
    assert store.load_units() == []  # 降级后仍可用


def test_store_mode_unknown_falls_back(monkeypatch, tmp_path):
    """未知 STORE_MODE：使用 JSONL 并带明确 note。"""
    monkeypatch.setenv("STORE_MODE", "bogus")
    k = tmp_path / "kb"
    (k / "units").mkdir(parents=True)
    store, mode, note = build_metadata_store(k)
    assert mode == "json" and note and "bogus" in note
