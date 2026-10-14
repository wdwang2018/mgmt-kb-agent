"""LangGraph 工作流装配：九节点 + 条件边 + 检查点。

路由规则：
- quality_gate 通过 → package_output
- quality_gate 未过且重试次数未用尽 → 回到 plan_modules（带 feedback 修正）
- quality_gate 未过且重试耗尽 → 节点内转人工审核（interrupt），恢复后按人工决定路由
"""

from __future__ import annotations

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from ..domain.models import (
    Activity,
    AssessmentItem,
    Bucket,
    CoursePackage,
    GapAnalysis,
    GateReport,
    LearningObjective,
    ModulePlan,
    TheoryPoint,
    TrainingNeed,
)
from .nodes import NODES, NodeDeps
from .state import CourseDesignState, initial_state

# 状态中携带 pydantic 领域模型，需在 msgpack serde 允许列表中显式登记，
# 否则检查点往返时告警（未来版本将直接阻断）。
_SERDE = JsonPlusSerializer(allowed_msgpack_modules=[
    TrainingNeed, GapAnalysis, LearningObjective, ModulePlan,
    TheoryPoint, Activity, AssessmentItem, GateReport, CoursePackage, Bucket,
])


def default_checkpointer():
    return MemorySaver(serde=_SERDE)


def build_workflow(deps: NodeDeps, checkpointer=None):
    """装配并编译课程设计生成器状态机。"""

    def _n(name):
        def node(state: CourseDesignState):
            return NODES[name](state, deps)
        node.__name__ = name
        return node

    def _route_after_gate(state: CourseDesignState) -> str:
        if state.get("gates_passed"):
            return "package_output"
        if state.get("status") == "retrying":
            return "plan_modules"
        # awaiting_human 时 interrupt 已在节点内抛出，不会走到这里
        return END

    g = StateGraph(CourseDesignState)
    for name in ["parse_need", "gap_analysis", "generate_objectives", "plan_modules",
                 "match_content", "design_activities", "design_assessments",
                 "quality_gate", "package_output"]:
        g.add_node(name, _n(name))

    g.add_edge(START, "parse_need")
    g.add_edge("parse_need", "gap_analysis")
    g.add_edge("gap_analysis", "generate_objectives")
    g.add_edge("generate_objectives", "plan_modules")
    g.add_edge("plan_modules", "match_content")
    g.add_edge("match_content", "design_activities")
    g.add_edge("design_activities", "design_assessments")
    g.add_edge("design_assessments", "quality_gate")
    g.add_conditional_edges("quality_gate", _route_after_gate)
    g.add_edge("package_output", END)

    return g.compile(checkpointer=checkpointer or default_checkpointer())
