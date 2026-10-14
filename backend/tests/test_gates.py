"""质量门禁 G1-G4 单元测试：合成状态验证各门禁的通过与拦截。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.graph.gates import run_gates

UNITS = {"KU-LEAD-001", "KU-LEAD-006", "KU-STRAT-001"}


def valid_state() -> dict:
    return {
        "objectives": [
            {"id": "OBJ-01", "linked_activity_ids": ["ACT-01", "ACT-02"],
             "linked_assessment_ids": ["ASM-01"]},
        ],
        "activities": [
            {"id": "ACT-01", "objective_ids": ["OBJ-01"], "bucket": "practice", "minutes": 40},
            {"id": "ACT-02", "objective_ids": ["OBJ-01"], "bucket": "social", "minutes": 20},
        ],
        "assessments": [
            {"id": "ASM-01", "objective_ids": ["OBJ-01"], "unit_ids": ["KU-LEAD-001"],
             "kind": "SJT", "stem": "s", "module_id": "M1"},
        ],
        "modules": [{"id": "M1", "title": "模块"}],
        "content": {"M1": [
            {"unit_id": "KU-LEAD-001", "citation": "Hill《成为管理者》第1章",
             "misconceptions": ["误区甲"], "text": "要点", "evidence_level": "B"},
        ]},
        "citations": ["Hill《成为管理者》第1章"],
    }


def _gates(state):
    return {g.gate: g for g in run_gates(state, UNITS)}


def test_all_gates_pass_on_valid_state():
    gates = _gates(valid_state())
    assert all(g.passed for g in gates.values()), [g.issues for g in gates.values()]


def test_g1_flags_unlinked_objective():
    s = valid_state()
    s["objectives"][0]["linked_activity_ids"] = []
    s["objectives"][0]["linked_assessment_ids"] = []
    g = _gates(s)["G1-建设性对齐"]
    assert not g.passed and any("教学活动" in i for i in g.issues) and any("评估题" in i for i in g.issues)


def test_g1_flags_dangling_links():
    s = valid_state()
    s["objectives"][0]["linked_activity_ids"] = ["ACT-99"]
    g = _gates(s)["G1-建设性对齐"]
    assert not g.passed and any("ACT-99" in i for i in g.issues)


def test_g2_flags_unknown_unit_and_missing_citation():
    s = valid_state()
    s["content"]["M1"][0]["unit_id"] = "KU-NOPE"
    g = _gates(s)["G2-强制溯源"]
    assert not g.passed and any("KU-NOPE" in i for i in g.issues)

    s2 = valid_state()
    s2["content"]["M1"][0]["citation"] = ""
    g2 = _gates(s2)["G2-强制溯源"]
    assert not g2.passed and any("引用定位" in i for i in g2.issues)


def test_g3_flags_formal_heavy_mix():
    s = valid_state()
    s["activities"] = [
        {"id": "ACT-01", "objective_ids": ["OBJ-01"], "bucket": "formal", "minutes": 60},
    ]
    g = _gates(s)["G3-70-20-10配比"]
    assert not g.passed and any("实践学习占比" in i for i in g.issues)


def test_g3_custom_rule():
    s = valid_state()
    rule = {"practice_min": 0.80, "social_min": 0.10, "formal_max": 0.10}
    g = run_gates(s, UNITS, rule)[2]
    assert not g.passed  # 40/60=67% < 80% 下限


def test_g4_flags_module_without_misconception():
    s = valid_state()
    s["content"]["M1"][0]["misconceptions"] = []
    g = _gates(s)["G4-认知冲突"]
    assert not g.passed and any("认知冲突" in i for i in g.issues)


def test_gates_accept_pydantic_models():
    from app.domain.models import Activity, AssessmentItem, LearningObjective, ModulePlan, TheoryPoint
    s = {
        "objectives": [LearningObjective(id="OBJ-01", text="t", bloom=3, domain="TEAM",
                                          target_level=3, linked_activity_ids=["ACT-01"],
                                          linked_assessment_ids=["ASM-01"])],
        "activities": [Activity(id="ACT-01", module_id="M1", title="a", bucket="practice",
                                minutes=40, objective_ids=["OBJ-01"], description="d")],
        "assessments": [AssessmentItem(id="ASM-01", kind="SJT", stem="s", module_id="M1",
                                       objective_ids=["OBJ-01"], unit_ids=["KU-LEAD-001"])],
        "modules": [ModulePlan(id="M1", title="m", minutes=60, objective_ids=["OBJ-01"])],
        "content": {"M1": [TheoryPoint(text="p", unit_id="KU-LEAD-001",
                                       citation="Hill《成为管理者》第1章",
                                       evidence_level="B", misconceptions=["误区"])]},
        "citations": ["Hill《成为管理者》第1章"],
    }
    gates = _gates(s)
    # G3 会因缺 social 未过，其余三门禁应通过（验证模型形态可读）
    assert gates["G1-建设性对齐"].passed
    assert gates["G2-强制溯源"].passed
    assert gates["G4-认知冲突"].passed
    assert not gates["G3-70-20-10配比"].passed
