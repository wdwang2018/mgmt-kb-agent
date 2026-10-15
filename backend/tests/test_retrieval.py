"""检索适配器测试：标签加权、双路召回、按 ID 查询。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _ctx(tmp_path=None):
    from app.services.context import build_context
    out = tmp_path or Path("runs_test")
    return build_context(output_dir=out)


def test_search_authorization_unit():
    ctx = _ctx()
    units = ctx.retriever.search("授权 两难 不敢放 放了乱", domains=["TEAM"], dilemmas=["D02"])
    ids = [u.id for u in units]
    assert "KU-LEAD-006" in ids[:3], ids


def test_search_grow_coaching():
    ctx = _ctx()
    units = ctx.retriever.search("GROW 教练 提问 目标 现状")
    ids = [u.id for u in units]
    assert "KU-LEAD-004" in ids[:3], ids


def test_search_strategy_decode():
    ctx = _ctx()
    units = ctx.retriever.search("战略解码 BEM 打仗 举措", domains=["STRAT"], dilemmas=["D08"])
    ids = [u.id for u in units]
    assert "KU-STRAT-001" in ids[:3], ids


def test_dual_recall_prefers_both_hit():
    ctx = _ctx()
    units = ctx.retriever.dual_recall(["STRAT"], ["D08"], top_k=5)
    ids = [u.id for u in units]
    assert "KU-STRAT-001" in ids
    # 双命中（STRAT 且 D08）的单元应排在仅单命中的前面
    both = [u for u in units if "STRAT" in u.domains and "D08" in u.dilemmas]
    assert units[0].id == both[0].id


def test_by_ids_roundtrip():
    ctx = _ctx()
    units = ctx.retriever.by_ids(["KU-LEAD-001", "KU-STRAT-002", "KU-NOPE"])
    assert [u.id for u in units] == ["KU-LEAD-001", "KU-STRAT-002"]


def test_loads_units_and_cases():
    ctx = _ctx()
    # 基线 24 个种子单元；知识流水线会持续入库（浏览器实测已 +1），故断言下限
    assert len(ctx.retriever.units) >= 24
    assert len(ctx.pipeline.deps.cases) == 5
