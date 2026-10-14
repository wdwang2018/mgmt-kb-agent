"""LangGraph 状态定义：课程设计生成器的全局状态（TypedDict）。

产物演进链：
need → gap → objectives → modules → content → activities → assessments → gates → package

HITL：pending_interrupt 记录待人工处理节点；human_decision 携带恢复时注入的人工决定。
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from typing import NotRequired, TypedDict  # Python 3.11+
except Exception:  # pragma: no cover
    from typing_extensions import NotRequired, TypedDict  # type: ignore

from ..domain.models import (
    Activity,
    AssessmentItem,
    CoursePackage,
    GapAnalysis,
    GateReport,
    LearningObjective,
    ModulePlan,
    TheoryPoint,
    TrainingNeed,
)


class CourseDesignState(TypedDict, total=False):
    run_id: str
    need: TrainingNeed
    gap: GapAnalysis
    archetype_key: str
    objectives: list[LearningObjective]
    modules: list[ModulePlan]
    content: dict[str, list[TheoryPoint]]
    activities: list[Activity]
    assessments: list[AssessmentItem]
    gate_reports: list[GateReport]
    gates_passed: bool
    retries: int
    feedback: list[str]
    human_revisions: int
    status: str
    package: Optional[CoursePackage]
    pending_interrupt: Optional[dict[str, Any]]
    human_decision: Optional[dict[str, Any]]
    citations: list[str]


def initial_state(run_id: str, need: TrainingNeed) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "need": need,
        "gap": None,
        "archetype_key": None,
        "objectives": [],
        "modules": [],
        "content": {},
        "activities": [],
        "assessments": [],
        "gate_reports": [],
        "gates_passed": False,
        "retries": 0,
        "feedback": [],
        "human_revisions": 0,
        "status": "created",
        "package": None,
        "pending_interrupt": None,
        "human_decision": None,
        "citations": [],
    }
