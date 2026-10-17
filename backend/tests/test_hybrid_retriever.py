"""混合检索（HybridRetriever）隔离测试。

不依赖 Milvus / 智谱 Key：用桩替代 dense 侧，专测融合与降级逻辑。
真实 Milvus + GLM 的端到端能力见 test_milvus_retriever.py 与 scripts/bench_retrieval.py。
"""

from __future__ import annotations

import pytest

from app.adapters.retrieval_hybrid import HybridRetriever
from app.adapters.retrieval_memory import MemoryRetriever
from app.domain.models import EvidenceLevel, KnowledgeUnit, SourceRef, KUType


def _unit(uid: str, title: str, domains=("TEAM",), dilemmas=("D01",)) -> KnowledgeUnit:
    return KnowledgeUnit(
        id=uid, title=title, type=KUType.MODEL,
        domains=list(domains), dilemmas=list(dilemmas),
        evidence_level=EvidenceLevel.B,
        key_points=["要点一", "要点二"],
        source=SourceRef(book="测试书", author="测试作者", locator="第1章",
                         copyright_status="要点化"),
    )


UNITS = [
    _unit("KU-LEAD-001", "角色转变"),
    _unit("KU-LEAD-002", "授权阶梯"),
    _unit("KU-LEAD-003", "GROW 教练模型"),
    _unit("KU-LEAD-004", "SBI 反馈模型"),
]


class StubDense:
    """稠密侧桩：按预设排名返回，可注入故障。"""

    def __init__(self, ranking, fail_search=False, fail_reload=False):
        self._ranking = list(ranking)
        self.fail_search = fail_search
        self.fail_reload = fail_reload
        self.last_error = None
        self._units = {u.id: u for u in UNITS}

    def search(self, query, domains=None, dilemmas=None, top_k=5):
        if self.fail_search:
            raise RuntimeError("milvus down")
        return [self._units[i] for i in self._ranking[:top_k] if i in self._units]

    def dual_recall(self, domains, dilemmas, top_k=6):
        return self.search("", domains, dilemmas, top_k)

    def reload(self, units):
        if self.fail_reload:
            raise RuntimeError("milvus reload failed")
        self._units = {u.id: u for u in units}

    def by_ids(self, ids):
        return [self._units[i] for i in ids if i in self._units]


def _hybrid(dense_ranking=("KU-LEAD-003", "KU-LEAD-004"), **kw):
    return HybridRetriever(MemoryRetriever(UNITS), StubDense(dense_ranking, **kw))


# ---------------------------------------------------------------- 融合逻辑
def test_rrf_fuses_both_lists():
    """两路都出现的文档应排最前（RRF 分数累加）。"""
    h = _hybrid(("KU-LEAD-002", "KU-LEAD-003"))
    # 构造查询让 BM25 也偏向 KU-LEAD-002
    got = [u.id for u in h.search("授权阶梯 五级情境授权", top_k=4)]
    assert got[0] == "KU-LEAD-002", f"两路共同命中者应居首，实际 {got}"


# 权重判别场景：刻意复现真实数据中的失效模式——
#   查询词在某无关单元里高频复现，BM25 把它顶到第一（伪关键词命中）；
#   而真答案与查询零词面共现，BM25 根本召回不到，只有稠密侧能给出。
_SPURIOUS = _unit("KU-LEAD-009", "SBI 反馈模型 情境 行为 影响",
                  ["SBI 反馈 情境 行为 影响", "反馈 情境 行为 影响 反馈"])
_REAL = _unit("KU-LEAD-004", "无关标题甲", ["要点一", "要点二"])
_NOISE = _unit("KU-LEAD-001", "无关标题乙", ["要点一", "要点二"])
_WEIGHT_UNITS = [_SPURIOUS, _REAL, _NOISE]
_WEIGHT_QUERY = "SBI 反馈 情境 行为 影响"


def _weight_hybrid(dense_weight: float) -> HybridRetriever:
    dense = StubDense(("KU-LEAD-004",))  # 稠密侧只认真答案
    return HybridRetriever(MemoryRetriever(_WEIGHT_UNITS), dense,
                           dense_weight=dense_weight)


def test_bm25_alone_is_fooled_by_keyword_stuffing():
    """前置校验：该场景下 BM25 确实只召回伪命中单元（否则权重测试无意义）。"""
    got = [u.id for u in MemoryRetriever(_WEIGHT_UNITS).search(_WEIGHT_QUERY, top_k=14)]
    assert got == ["KU-LEAD-009"], f"BM25 应被关键词堆砌欺骗，实际 {got}"


def test_dense_weight_overrides_bm25_spurious_rank():
    """稠密侧权重 ≥1 时，向量 Top1 必须压过 BM25 的伪关键词命中。

    这是 2026-10-17 实测的核心结论：等权融合会被 BM25 的伪命中拖累，
    实测 B 组 MRR 从 0.833（等权）提升到 0.917（wd=2）。
    """
    fooled = [u.id for u in _weight_hybrid(0.5).search(_WEIGHT_QUERY, top_k=3)]
    fixed = [u.id for u in _weight_hybrid(2.0).search(_WEIGHT_QUERY, top_k=3)]
    assert fooled[0] == "KU-LEAD-009", f"低权重下伪命中应居首，实际 {fooled}"
    assert fixed[0] == "KU-LEAD-004", f"默认权重下真答案应居首，实际 {fixed}"
    assert fooled != fixed, "权重必须真正改变排序，否则本测试无判别力"


def test_dense_weight_is_the_deciding_factor():
    """权重是唯一变量：同一场景仅改 dense_weight，排序必须翻转。"""
    low = [u.id for u in _weight_hybrid(0.5).search(_WEIGHT_QUERY, top_k=3)]
    high = [u.id for u in _weight_hybrid(2.0).search(_WEIGHT_QUERY, top_k=3)]
    assert low != high, f"权重改变了却得到相同排序，测试失效：low={low} high={high}"


def test_fusion_is_deterministic():
    """同分按 id 排序，结果可复现。"""
    h = _hybrid(("KU-LEAD-003",))
    a = [u.id for u in h.search("GROW 教练", top_k=5)]
    b = [u.id for u in h.search("GROW 教练", top_k=5)]
    assert a == b


def test_top_k_respected():
    h = _hybrid(("KU-LEAD-002", "KU-LEAD-003", "KU-LEAD-004"))
    got = h.search("教练 反馈 授权", top_k=2)
    assert len(got) <= 2


# ---------------------------------------------------------------- 降级逻辑
def test_no_dense_degrades_to_bm25():
    h = HybridRetriever(MemoryRetriever(UNITS), None)
    assert h.dense_available is False
    assert h.degrade_note is not None
    got = [u.id for u in h.search("GROW 教练模型", top_k=2)]
    assert "KU-LEAD-003" in got  # 纯 BM25 仍可用


def test_dense_search_failure_falls_back_per_query():
    h = _hybrid(fail_search=True)
    got = [u.id for u in h.search("GROW 教练模型", top_k=3)]
    assert "KU-LEAD-003" in got
    assert h.degrade_note is not None and "向量侧检索失败" in h.degrade_note


def test_dense_reload_failure_drops_vector_path():
    h = _hybrid(fail_reload=True)
    h.reload(UNITS)
    assert h.dense_available is False
    assert "reload 失败" in (h.degrade_note or "")
    # BM25 侧仍然可用
    assert [u.id for u in h.search("GROW 教练模型", top_k=2)]


def test_reload_keeps_both_paths_when_healthy():
    h = _hybrid()
    h.reload(UNITS)
    assert h.dense_available is True
    assert h.degrade_note is None


# ---------------------------------------------------------------- dual_recall
def test_dual_recall_fuses_tags():
    h = _hybrid(("KU-LEAD-003", "KU-LEAD-004"))
    got = [u.id for u in h.dual_recall(["TEAM"], ["D01"], top_k=3)]
    assert len(got) == 3
    assert set(got) <= {"KU-LEAD-001", "KU-LEAD-002", "KU-LEAD-003", "KU-LEAD-004"}


# ---------------------------------------------------------------- from_env
def test_from_env_reads_params(monkeypatch):
    monkeypatch.setenv("RRF_K", "10")
    monkeypatch.setenv("RRF_DENSE_WEIGHT", "3.5")
    h = HybridRetriever.from_env(MemoryRetriever(UNITS), StubDense(()))
    assert h._rrf_k == 10
    assert h._dense_weight == 3.5


def test_from_env_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("RRF_K", raising=False)
    monkeypatch.delenv("RRF_DENSE_WEIGHT", raising=False)
    h = HybridRetriever.from_env(MemoryRetriever(UNITS), StubDense(()))
    assert h._rrf_k == 60
    assert h._dense_weight == 2.0


def test_units_property_for_healthz():
    h = _hybrid()
    assert len(h.units) == len(UNITS)


@pytest.mark.parametrize("weight", [1.5, 2.0, 3.0])
def test_weight_plateau(weight):
    """1.5~3+ 为实测平台期：该区间内稠密侧 Top1 都应稳定压过 BM25 伪命中。"""
    got = [u.id for u in _weight_hybrid(weight).search(_WEIGHT_QUERY, top_k=3)]
    assert got[0] == "KU-LEAD-004", f"权重 {weight} 处于平台期却未压过伪命中：{got}"
