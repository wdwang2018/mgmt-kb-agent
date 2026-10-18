"""知识库对话问答（#1）：把讲师的自然语言提问，转成「只答库内、可溯源」的回答。

设计约束（与项目铁律一致）：
- 答案必须基于检索到的知识单元，绝不允许模型编造库外内容；
- 每条引用都回链到 书名/作者/章节定位（source.locator）；
- 检索不到就如实说「库内暂无相关单元」，而不是硬编。
- 纯检索 + 一次 LLM 调用，离线 Mock 模式也能跑（Mock 用检索上下文拼答案）。
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.models import KBChatResponse, KBChatSource, KnowledgeUnit
from ..ports import LLMClient, Retriever

_SYSTEM = (
    "你是「管理学知识库」的问答助手，服务于培训讲师。规则：\n"
    "1. 只能基于下面提供的【知识库上下文】作答，上下文外的说法一律不要编造；\n"
    "2. 每条结论都要标注来源，格式为「作者《书名》章节定位」；\n"
    "3. 若上下文无法回答，明确说「库内暂无相关单元，建议补充该主题语料」，不要猜测；\n"
    "4. 用讲师能直接用在课件里的口吻，简洁、有结构，避免营销腔。"
)


def _citation(u: KnowledgeUnit) -> str:
    """单元引用串：作者《书名》章节定位。"""
    s = u.source
    return f"{s.author}《{s.book}》{s.locator}"


def _build_context(units: list[KnowledgeUnit]) -> list[dict[str, Any]]:
    return [{
        "id": u.id,
        "title": u.title,
        "type": u.type.value,
        "evidence_level": u.evidence_level.value,
        "citation": _citation(u),
        "key_points": u.key_points,
    } for u in units]


def chat_kb(
    question: str,
    retriever: Retriever,
    llm: LLMClient,
    top_k: int = 5,
    domains: list[str] | None = None,
    dilemmas: list[str] | None = None,
) -> KBChatResponse:
    """回答一个关于管理学知识库的问题。

    - 检索：复用既有混合检索（ctx.retriever.search），命中相关知识单元；
    - 生成：把检索上下文塞进 user，调用 llm.complete(purpose="kb_chat")；
    - 不出检索结果时，答案由模型如实说明「库内暂无」。
    """
    q = (question or "").strip()
    if not q:
        return KBChatResponse(answer="请先输入你的问题，例如「新任经理最常见的授权误区有哪些？」", sources=[])

    units = retriever.search(q, domains=domains, dilemmas=dilemmas, top_k=top_k)
    context = _build_context(units)

    user = json.dumps({"question": q, "context": context}, ensure_ascii=False)
    answer = llm.complete("kb_chat", _SYSTEM, user)

    sources = [
        KBChatSource(
            unit_id=u.id,
            title=u.title,
            citation=_citation(u),
            evidence_level=u.evidence_level.value,
            snippet=(u.key_points[0] if u.key_points else ""),
        )
        for u in units
    ]
    return KBChatResponse(answer=answer, sources=sources)
