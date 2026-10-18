"""运营包生成（#3）：把成型的课程包，转成「可招生、可交付、可跟踪」的运营方案。

设计取向（对外公开课口径）：
- 不替代课程设计，只补「推向市场 + 训前预热 + 训后行为迁移」这一段；
- 受众定位 / 卖点 / 推广方式 让讲师能直接用于招生；
- 课前预热 + 课后 30-60-90 行为迁移跟进，对应 Kirkpatrick L3（行为层）；
- 生成走 LLM（complete_json, purpose="operations_generate"），离线 Mock 也能确定性产出。
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.models import CoursePackage, OperationsPlan

_SYSTEM = (
    "你是培训运营顾问，服务于商业讲师。基于给定课程包，产出一份对外公开课的运营方案。\n"
    "要求：\n"
    "1. audience_positioning：一句话定位受众与核心卖点（结合行业/对象/痛点）；\n"
    "2. selling_points：3-5 条差异化卖点，突出「可溯源/练得多/有跟进」；\n"
    "3. promotion_channels：3-5 种推广方式 {channel, method, fit}；\n"
    "4. pre_class_prep：课前预热/准备动作（问卷、阅读、破冰）；\n"
    "5. post_class_followup：课后 30/60/90 天三阶段 {phase, goal, actions}，聚焦行为迁移（Kirkpatrick L3），动作要具体可落地。\n"
    "输出严格对应 OperationsPlan 字段。"
)


def _slim(pkg: CoursePackage) -> dict[str, Any]:
    d = pkg.model_dump()
    return {
        "need": d.get("need"),
        "modules": d.get("modules"),
        "content": d.get("content"),
        "citations": d.get("citations"),
    }


def _norm(data: dict[str, Any]) -> dict[str, Any]:
    """规整 LLM 输出为 OperationsPlan 可接受字典，缺字段用安全默认补。"""
    chs = data.get("promotion_channels") or []
    channels = [{"channel": c.get("channel", ""), "method": c.get("method", ""),
                 "fit": c.get("fit", "")} for c in chs if isinstance(c, dict)]
    fups = data.get("post_class_followup") or []
    followups = [{"phase": f.get("phase", ""), "goal": f.get("goal", ""),
                  "actions": f.get("actions") or []} for f in fups if isinstance(f, dict)]
    return {
        "audience_positioning": data.get("audience_positioning", ""),
        "selling_points": data.get("selling_points") or [],
        "promotion_channels": channels,
        "pre_class_prep": data.get("pre_class_prep") or [],
        "post_class_followup": followups,
    }


def generate_operations(pkg: CoursePackage, llm) -> OperationsPlan:
    """从课程包生成运营包。"""
    user = json.dumps(_slim(pkg), ensure_ascii=False)
    data = llm.complete_json("operations_generate", _SYSTEM, user, fallback={})
    return OperationsPlan(**_norm(data))
