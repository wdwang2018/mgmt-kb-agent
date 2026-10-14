"""质量门禁 G1-G4：课程包的自动质检规则（纯函数，无外部依赖）。

- G1 建设性对齐：每个学习目标必须同时挂接教学活动与评估题（教学设计灵魂）；
- G2 强制溯源：每个理论要点/评估题必须能对应到已入库知识单元，无据不生成；
- G3 70-20-10 配比：实践/人际/正式学习时长占比失衡即打回；
- G4 认知冲突：每个模块至少一处「学员常见误区 vs 理论证据」。

状态中的载体可能是 dict（JSON 检查点）或 pydantic 模型（内存态），统一走 _f 访问器。
"""

from __future__ import annotations

from typing import Any

from ..domain.constants import DEFAULT_70_20_10_RULE
from ..domain.models import GateReport


def _f(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _g1_alignment(state: dict[str, Any]) -> GateReport:
    issues: list[str] = []
    objectives = state.get("objectives", [])
    activities = state.get("activities", [])
    assessments = state.get("assessments", [])
    obj_ids = {_f(o, "id") for o in objectives}
    act_ids = {_f(a, "id") for a in activities}
    asm_ids = {_f(a, "id") for a in assessments}

    for o in objectives:
        oid = _f(o, "id")
        if not _f(o, "linked_activity_ids"):
            issues.append(f"目标 {oid} 未挂接任何教学活动")
        if not _f(o, "linked_assessment_ids"):
            issues.append(f"目标 {oid} 未挂接任何评估题")
        for aid in _f(o, "linked_activity_ids", []):
            if aid not in act_ids:
                issues.append(f"目标 {oid} 挂接了不存在的活动 {aid}")
        for aid in _f(o, "linked_assessment_ids", []):
            if aid not in asm_ids:
                issues.append(f"目标 {oid} 挂接了不存在的评估 {aid}")
    for a in activities:
        for oid in _f(a, "objective_ids", []):
            if oid not in obj_ids:
                issues.append(f"活动 {_f(a, 'id')} 引用了不存在的目标 {oid}")
    for a in assessments:
        for oid in _f(a, "objective_ids", []):
            if oid not in obj_ids:
                issues.append(f"评估 {_f(a, 'id')} 引用了不存在的目标 {oid}")
    return GateReport(gate="G1-建设性对齐", passed=not issues, issues=issues)


def _g2_traceability(state: dict[str, Any], units_index: set[str]) -> GateReport:
    issues: list[str] = []
    content: dict[str, list] = state.get("content", {})
    total_points = 0
    for module_id, points in content.items():
        for p in points:
            total_points += 1
            if not _f(p, "unit_id"):
                issues.append(f"模块 {module_id} 存在无来源理论要点")
            elif _f(p, "unit_id") not in units_index:
                issues.append(f"模块 {module_id} 引用了未入库单元 {_f(p, 'unit_id')}")
            if not _f(p, "citation"):
                issues.append(f"模块 {module_id} 要点缺少引用定位")
    if total_points == 0:
        issues.append("课程包无任何理论要点（内容匹配失败）")
    for a in state.get("assessments", []):
        for uid in _f(a, "unit_ids", []):
            if uid not in units_index:
                issues.append(f"评估 {_f(a, 'id')} 引用未入库单元 {uid}")
    if not state.get("citations"):
        issues.append("引用清单为空")
    return GateReport(gate="G2-强制溯源", passed=not issues, issues=issues)


def _g3_70_20_10(state: dict[str, Any], rule: dict[str, float] | None = None) -> GateReport:
    rule = rule or DEFAULT_70_20_10_RULE
    issues: list[str] = []
    totals = {"formal": 0, "social": 0, "practice": 0}
    for a in state.get("activities", []):
        bucket = _f(a, "bucket")
        bucket = bucket.value if hasattr(bucket, "value") else bucket
        totals[bucket] = totals.get(bucket, 0) + _f(a, "minutes", 0)
    total = sum(totals.values())
    if total == 0:
        return GateReport(gate="G3-70-20-10配比", passed=False, issues=["无教学活动，无法评估配比"])
    practice = totals["practice"] / total
    social = totals["social"] / total
    formal = totals["formal"] / total
    if practice < rule["practice_min"]:
        issues.append(f"实践学习占比 {practice:.0%} 低于下限 {rule['practice_min']:.0%}")
    if social < rule["social_min"]:
        issues.append(f"人际学习占比 {social:.0%} 低于下限 {rule['social_min']:.0%}")
    if formal > rule["formal_max"]:
        issues.append(f"正式学习占比 {formal:.0%} 高于上限 {rule['formal_max']:.0%}")
    return GateReport(gate="G3-70-20-10配比", passed=not issues, issues=issues)


def _g4_cognitive_conflict(state: dict[str, Any]) -> GateReport:
    issues: list[str] = []
    content: dict[str, list] = state.get("content", {})
    for m in state.get("modules", []):
        points = content.get(_f(m, "id"), [])
        if not points:
            issues.append(f"模块 {_f(m, 'id')} 无内容")
            continue
        if not any(_f(p, "misconceptions") for p in points):
            issues.append(f"模块 {_f(m, 'id')} 缺少认知冲突点（常见误区 vs 理论证据）")
    return GateReport(gate="G4-认知冲突", passed=not issues, issues=issues)


def run_gates(state: dict[str, Any], units_index: set[str], rule: dict[str, float] | None = None) -> list[GateReport]:
    return [
        _g1_alignment(state),
        _g2_traceability(state, units_index),
        _g3_70_20_10(state, rule),
        _g4_cognitive_conflict(state),
    ]
