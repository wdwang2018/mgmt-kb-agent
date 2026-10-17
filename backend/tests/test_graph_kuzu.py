"""Kùzu 图存储适配器测试（GRAPH_MODE=kuzu，真跑，全隔离临时库文件）。

覆盖：schema 自建 / MERGE 幂等 / 全 7 种关系类型 / 前置链多跳 /
争议去重 / rebuild 差值同步（含悬空引用过滤）/ 持久化重开。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.adapters.graph_kuzu import KuzuGraphStore
from app.domain.models import EvidenceLevel, KnowledgeUnit, KUType, RelationType, SourceRef

kuzu = pytest.importorskip("kuzu", reason="kuzu 未安装（pip install kuzu）")


def _unit(uid: str, title: str, relations=(), prereq=(), related=()) -> KnowledgeUnit:
    return KnowledgeUnit(
        id=uid, title=title, type=KUType.MODEL, domains=["LEAD"], dilemmas=["D01"],
        evidence_level=EvidenceLevel.B, key_points=["要点一", "要点二"],
        source=SourceRef(book="测试书", author="作者", locator="第1章", copyright_status="要点化"),
        relations=[{"type": t, "target": g} for t, g in relations],
        prerequisites=list(prereq), related=list(related),
    )


@pytest.fixture()
def graph(tmp_path) -> KuzuGraphStore:
    return KuzuGraphStore(db_path=tmp_path / "test.kuzu")


# ---------------------------------------------------------------- 基础
def test_schema_autocreate_and_stats(graph):
    """构造即自动建表，空图 stats 全零。"""
    assert graph.stats() == {"ku_nodes": 0, "person_nodes": 0, "school_nodes": 0,
                             "view_nodes": 0, "case_nodes": 0, "relations": 0}


def test_sync_all_seven_relation_types(graph):
    """relations 全部 7 种类型 + prerequisites + related 都应落边。"""
    u1 = _unit("KU-LEAD-001", "角色转变",
               relations=[("提出者", "Linda A. Hill"), ("学派", "领导力转型学派"),
                          ("争议", "理性决策观"), ("对比", "outsight 优先观"),
                          ("前置", "KU-LEAD-002"), ("应用", "CASE-001")],
               related=["KU-LEAD-006"])
    u2 = _unit("KU-LEAD-002", "能力陷阱")
    u3 = _unit("KU-LEAD-006", "授权阶梯")
    for u in (u1, u2, u3):
        graph.sync_unit(u)

    st = graph.stats()
    assert st["ku_nodes"] == 3 and st["person_nodes"] == 1 and st["school_nodes"] == 1
    assert st["view_nodes"] == 2 and st["case_nodes"] == 1
    assert graph.proposers("KU-LEAD-001") == ["Linda A. Hill"]
    assert graph.schools("KU-LEAD-001") == ["领导力转型学派"]
    assert sorted(graph.contested_views("KU-LEAD-001")) == ["outsight 优先观", "理性决策观"]
    assert graph.applied_cases("KU-LEAD-001") == ["CASE-001"]
    assert "KU-LEAD-006" in graph.neighbors("KU-LEAD-001")["RELATED"]
    assert "KU-LEAD-002" in graph.neighbors("KU-LEAD-001")["PREREQUISITE"]


def test_merge_idempotent_no_duplicates(graph):
    """重复 sync 不产生重复节点/边（「入库即生效」不写脏的前提）。

    关系边数：提出者 1 条 + related 无向语义双向 2 条 = 3。
    """
    u = _unit("KU-LEAD-001", "甲", relations=[("提出者", "学者A")], related=["KU-LEAD-002"])
    for _ in range(3):
        graph.sync_unit(u)
    st = graph.stats()
    assert st["ku_nodes"] == 2 and st["person_nodes"] == 1 and st["relations"] == 3


def test_contested_views_dedup(graph):
    """争议 + 对比指向同一 View 时查询去重。"""
    u = _unit("KU-LEAD-002", "能力陷阱",
              relations=[("争议", "同一观点"), ("对比", "同一观点")])
    graph.sync_unit(u)
    assert graph.contested_views("KU-LEAD-002") == ["同一观点"]


# ---------------------------------------------------------------- 查询
def test_prerequisites_multi_hop(graph):
    """前置链多跳遍历：006 → 002 → 001。"""
    graph.sync_unit(_unit("KU-LEAD-001", "角色转变"))
    graph.sync_unit(_unit("KU-LEAD-002", "能力陷阱", prereq=["KU-LEAD-001"]))
    graph.sync_unit(_unit("KU-LEAD-006", "授权阶梯", prereq=["KU-LEAD-002"]))
    assert graph.prerequisites("KU-LEAD-006", depth=3) == ["KU-LEAD-001", "KU-LEAD-002"]
    assert graph.prerequisites("KU-LEAD-006", depth=1) == ["KU-LEAD-002"]
    assert graph.prerequisites("KU-LEAD-001") == []


def test_prerequisites_dedup_multi_path(graph):
    """同一祖先经多条路径到达时去重（真实数据曾暴露：008→001 与 008→002→001 重复）。"""
    graph.sync_unit(_unit("KU-LEAD-001", "角色转变"))
    graph.sync_unit(_unit("KU-LEAD-002", "能力陷阱", prereq=["KU-LEAD-001"]))
    # 008 同时直接依赖 001 与 002，而 002 又依赖 001 → 001 有两条路径
    graph.sync_unit(_unit("KU-LEAD-008", "误区", prereq=["KU-LEAD-001", "KU-LEAD-002"]))
    assert graph.prerequisites("KU-LEAD-008", depth=3) == ["KU-LEAD-001", "KU-LEAD-002"]


def test_neighbors_grouped_by_relation(graph):
    """neighbors 按关系类型分组返回。"""
    graph.sync_unit(_unit("KU-LEAD-001", "角色转变",
                          relations=[("提出者", "Hill")], related=["KU-LEAD-002"]))
    graph.sync_unit(_unit("KU-LEAD-002", "能力陷阱"))
    nb = graph.neighbors("KU-LEAD-001")
    assert nb["PROPOSED_BY"] == ["Hill"] and "KU-LEAD-002" in nb["RELATED"]
    assert "PREREQUISITE" not in nb  # 无此关系时不出现该键


# ---------------------------------------------------------------- 维护
def test_rebuild_removes_stale_and_filters_dangling(graph):
    """rebuild 删陈旧节点；悬空引用（related 指向已删单元）不落地。"""
    u1 = _unit("KU-LEAD-001", "角色转变", related=["KU-LEAD-006"])
    u2 = _unit("KU-LEAD-002", "能力陷阱", prereq=["KU-LEAD-001"])
    u3 = _unit("KU-LEAD-006", "授权阶梯", prereq=["KU-LEAD-002"])
    for u in (u1, u2, u3):
        graph.sync_unit(u)
    assert graph.stats()["ku_nodes"] == 3

    graph.rebuild([u1, u2])  # 006 从库中删除，但 u1.related 仍引用它
    assert graph.stats()["ku_nodes"] == 2
    assert graph.prerequisites("KU-LEAD-006") == []            # 节点已删
    assert "RELATED" not in graph.neighbors("KU-LEAD-001")     # 悬空边不落地
    assert graph.prerequisites("KU-LEAD-002") == ["KU-LEAD-001"]  # 存活关系不受影响


def test_rebuild_idempotent(graph):
    """连续两次 rebuild 同一集合，stats 不变。"""
    units = [_unit("KU-LEAD-001", "甲", relations=[("提出者", "A")]),
             _unit("KU-LEAD-002", "乙", prereq=["KU-LEAD-001"])]
    graph.rebuild(units)
    first = graph.stats()
    graph.rebuild(units)
    assert graph.stats() == first


def test_persistence_across_reopen(tmp_path):
    """关闭重开后图数据仍在（本地文件存储）。"""
    path = tmp_path / "persist.kuzu"
    g1 = KuzuGraphStore(db_path=path)
    g1.sync_unit(_unit("KU-LEAD-002", "能力陷阱", relations=[("争议", "理性决策观")]))
    g1._db.close()
    g2 = KuzuGraphStore(db_path=path)
    assert g2.contested_views("KU-LEAD-002") == ["理性决策观"]


def test_sync_without_known_ids_keeps_references(graph):
    """单独 sync（known_ids=None）不过滤：引用尚未加载的单元仍建边。"""
    graph.sync_unit(_unit("KU-LEAD-001", "甲", related=["KU-LEAD-999"]))
    assert "KU-LEAD-999" in graph.neighbors("KU-LEAD-001")["RELATED"]
