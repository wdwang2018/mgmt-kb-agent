"""Milvus 检索适配器测试（全隔离：临时知识目录 + 临时 Milvus Lite 库）。

pymilvus 未安装时整组跳过，保证默认 memory 模式（离线可跑）不受向量依赖影响。

验证重点（对应用户可见的维护机制）：
- 语义召回：相同文本应召回自身；
- 标签过滤：单元自身的能力域/困境应能召回自身；
- upsert 幂等：重复 reload 不产生重复行（这是「入库即生效」不写脏的前提）；
- 持久层一致性：持久层移除单元后 reload，索引同步删除，不残留。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pymilvus", reason="pymilvus 未安装，跳过 Milvus 适配器测试")

from app.adapters.embedding_mock import MockEmbedding      # noqa: E402
from app.adapters.kb_json import JsonKnowledgeStore        # noqa: E402
from app.adapters.retrieval_milvus import MilvusRetriever  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_UNITS = sorted((REPO_ROOT / "knowledge" / "units").glob("*.jsonl"))


@pytest.fixture
def milvus_kb(tmp_path):
    """隔离环境：真实知识单元的前若干条 + 临时 Milvus Lite 库文件。"""
    if not _SRC_UNITS:
        pytest.skip("缺少知识单元源数据")
    lines: list[str] = []
    for f in _SRC_UNITS:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                lines.append(line)
    if not lines:
        pytest.skip("知识单元源数据为空")

    k_dir = tmp_path / "kb"
    (k_dir / "units").mkdir(parents=True, exist_ok=True)
    (k_dir / "units" / "seed.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    store = JsonKnowledgeStore(k_dir, review_log_dir=tmp_path / "reviews")
    units = store.load_units()
    if not units:
        pytest.skip("知识单元加载为空")

    uri = str(tmp_path / "milvus_test.db")
    r = MilvusRetriever(embedder=MockEmbedding(dim=64), units=units,
                        collection="kb_test", uri=uri)
    yield r, units
    r._c.drop_collection("kb_test")
    r._c.close()


def test_units_indexed(milvus_kb):
    r, units = milvus_kb
    assert len(r.units) == len(units)
    assert len(r._all_ids()) == len(units)


def test_same_text_recalls_itself(milvus_kb):
    """稠密向量语义召回：用单元标题检索，自身应排第一。"""
    r, units = milvus_kb
    u = units[0]
    got = r.search(u.title, top_k=1)
    assert got and got[0].id == u.id


def test_tag_filter_recalls_itself(milvus_kb):
    """标签过滤：单元自身的能力域/困境应能召回自身。"""
    r, units = milvus_kb
    u = units[0]
    got = r.dual_recall(list(u.domains), list(u.dilemmas), top_k=6)
    ids = {x.id for x in got}
    assert got and u.id in ids


def test_reload_is_idempotent(milvus_kb):
    """重复 reload 不产生重复行——入库即生效不会把索引写脏。"""
    r, units = milvus_kb
    before = len(r._all_ids())
    r.reload(units)
    r.reload(units)
    assert len(r._all_ids()) == before == len(units)


def test_reload_removes_deleted_units(milvus_kb):
    """持久层移除单元后 reload，索引同步删除，保证与 JSONL 真相源一致。"""
    r, units = milvus_kb
    if len(units) < 2:
        pytest.skip("单元不足，无法验证删除同步")
    keep = units[:-1]
    r.reload(keep)
    alive = r._all_ids()
    assert len(alive) == len(keep)
    assert units[-1].id not in alive


def test_by_ids(milvus_kb):
    r, units = milvus_kb
    got = r.by_ids([u.id for u in units])
    assert len(got) == len(units)
