"""在线模型输出 shape 规范化回归测试。

背景：GLM-5 实测把 SJT 选项返回为 {"id": "A", "text": ...} 对象数组、
answer 返回字母 id，曾直接击穿 pydantic 导致节点崩溃。本测试用「坏 shape 桩」
复现该场景，验证规范化层把脏数据收敛为合法模型，流水线照常完成。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.llm_mock import MockLLM
from app.adapters.kb_json import JsonKnowledgeStore
from app.adapters.retrieval_memory import MemoryRetriever
from app.domain.models import TrainingNeed
from app.graph.nodes import NodeDeps
from app.services.pipeline import CourseDesignPipeline

NEED = TrainingNeed(
    audience="新任经理（晋升 6 个月内）", audience_level=2, headcount=24,
    duration_minutes=360,
    business_pain="新经理仍埋头做事，下属得不到成长，跨部门协作全部卡在经理这里",
    desired_behavior="完成从个人贡献者到团队领导者的转身：会授权、会给反馈、坚持 1on1",
    industry="互联网", theme_hint="新任管理者转型",
)


class GLMStyleLLM(MockLLM):
    """模拟 GLM-5 的坏习惯：选项用对象数组、答案用字母、目标用纯字符串列表。"""

    def complete_json(self, purpose, system, user, fallback):
        if purpose == "assessment_stem":
            return {
                "stem": {"text": "关于授权，哪种做法最符合理论证据？"},  # stem 是对象
                "options": [
                    {"id": "A", "text": "完全顺其自然，等下属自己领悟"},
                    {"id": "B", "text": "明确期望与边界后授权，定期复盘"},
                    {"id": "C", "text": "用更高压的管控弥补当前不足"},
                    {"id": "D", "text": "只在绩效考核时集中说明问题"},
                ],
                "answer": "B",  # 字母 id 而非整数下标
                "rationale": {"text": "理论证据支持明确授权与复盘"},
            }
        if purpose == "objectives":
            return {"objectives": ["能够授权并辅导下属成长", "能够坚持一对一沟通"]}  # 纯字符串列表
        if purpose == "parse_need":
            return {"headcount": "24人", "audience_level": "三层", "industry": "互联网"}
        return super().complete_json(purpose, system, user, fallback)


def _pipeline(tmp_path, llm):
    repo = Path(__file__).resolve().parents[2]
    store = JsonKnowledgeStore(repo / "knowledge", review_log_dir=tmp_path / "reviews")
    retriever = MemoryRetriever(store.load_units())
    deps = NodeDeps(llm=llm, retriever=retriever, store=store,
                    cases=store.load_cases(), output_dir=tmp_path / "runs",
                    hitl_mode="interrupt")
    return CourseDesignPipeline(deps)


def test_glm_style_shapes_do_not_break_pipeline(tmp_path):
    result = _pipeline(tmp_path, GLMStyleLLM()).run_auto(NEED)
    assert result.status == "completed", result.detail
    pkg = result.package
    # SJT 全部为合法字符串选项，且答案下标被正确解析（B → 1）
    sjts = [a for a in pkg.assessments if a.kind == "SJT"]
    assert sjts
    for a in sjts:
        assert all(isinstance(o, str) and o for o in a.options)
        assert isinstance(a.answer_index, int) and 0 <= a.answer_index < len(a.options)
    assert any(a.answer_index == 1 for a in sjts)
    assert all(g.passed for g in pkg.gates), [g.issues for g in pkg.gates if not g.passed]


def test_parse_need_rejects_uncoercible_fields(tmp_path):
    result = _pipeline(tmp_path, GLMStyleLLM()).run_auto(NEED)
    assert result.status == "completed"
    need = result.package.need
    assert need.headcount == 24        # "24人" 不可解析 → 保持原值
    assert need.audience_level == 2    # "三层" 不可解析 → 保持原值
    assert need.industry == "互联网"    # 合法字符串 → 采纳


def test_objectives_string_list_falls_back(tmp_path):
    result = _pipeline(tmp_path, GLMStyleLLM()).run_auto(NEED)
    assert result.status == "completed"
    assert result.package.objectives, "目标为空"
    for o in result.package.objectives:
        assert isinstance(o.text, str) and o.text
        assert 1 <= o.bloom <= 5
