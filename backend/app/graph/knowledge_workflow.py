"""知识流水线工作流装配（P1）：五节点 + 条件路由 + 检查点。

路由规则：
- validate_draft 质检通过 → human_review（interrupt 挂起等审核员）
- validate_draft 质检不过 → END（自动打回，detail 带失败检查项）
- human_review 批准 → commit_draft → END；打回 → END
"""

from __future__ import annotations

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from .checkpoint import domain_serde
from .knowledge_nodes import KB_NODES, KBNodeDeps, KBState

# 状态携带 pydantic 领域模型，msgpack serde 允许列表由 checkpoint.domain_serde
# 统一登记（课程/知识两张图取并集，超集为安全放行）。
_SERDE = domain_serde()


def default_kb_checkpointer():
    return MemorySaver(serde=_SERDE)


def build_kb_workflow(deps: KBNodeDeps, checkpointer=None):
    """装配并编译知识流水线状态机。"""

    def _n(name):
        def node(state: KBState):
            return KB_NODES[name](state, deps)
        node.__name__ = name
        return node

    def _route_after_validate(state: KBState) -> str:
        quality = state.get("quality")
        return "human_review" if (quality and quality.passed) else END

    def _route_after_review(state: KBState) -> str:
        decision = state.get("decision") or {}
        return "commit_draft" if decision.get("action") == "approve" else "reject_draft"

    g = StateGraph(KBState)
    for name in ["ingest_corpus", "extract_draft", "validate_draft",
                 "human_review", "reject_draft", "commit_draft"]:
        g.add_node(name, _n(name))

    g.add_edge(START, "ingest_corpus")
    g.add_edge("ingest_corpus", "extract_draft")
    g.add_edge("extract_draft", "validate_draft")
    g.add_conditional_edges("validate_draft", _route_after_validate)
    g.add_conditional_edges("human_review", _route_after_review)
    g.add_edge("reject_draft", END)
    g.add_edge("commit_draft", END)

    return g.compile(checkpointer=checkpointer or default_kb_checkpointer())
