"""知识库对话问答测试（#1）：只答库内、可溯源。

覆盖：
- 服务级：chat_kb 返回答案 + 引用来源（引用格式=作者《书名》定位）、空问题短路不调 LLM、
  检索不到时如实说「库内暂无」；
- 端点级：POST /kb/chat 走真实检索（73 单元）返回带引用的来源；
- 变异验证：把 _citation 改成返回空串后，引用格式断言必须失败（非空转）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.llm_mock import MockLLM
from app.domain.models import (EvidenceLevel, KnowledgeUnit, KUType, KBChatResponse,
                               SourceRef)
from app.services.kb_chat import _citation, chat_kb


def _unit(uid: str, title: str, book: str, author: str, locator: str, kps: list[str]) -> KnowledgeUnit:
    return KnowledgeUnit(
        id=uid, title=title, type=KUType.THEORY,
        domains=["TEAM"], dilemmas=["D01"], evidence_level=EvidenceLevel.A,
        source=SourceRef(book=book, author=author, locator=locator),
        key_points=kps,
    )


class _FakeRetriever:
    def __init__(self, units: list[KnowledgeUnit]):
        self._units = units

    def search(self, query: str, domains=None, dilemmas=None, top_k: int = 5):
        return self._units[:top_k]


class _ExplodingLLM:
    name = "exploding"

    def complete(self, purpose, system, user):
        raise AssertionError("空问题不应调用 LLM")

    def complete_json(self, purpose, system, user, fallback):
        raise AssertionError("空问题不应调用 LLM")


def test_chat_kb_service_answer_and_citations():
    units = [
        _unit("KU-LEAD-001", "情境领导", "领导力", "Hersey", "第2章",
              ["按准备度调整领导风格", "准备度=能力+意愿"]),
        _unit("KU-BEHAV-002", "X-Y 理论", "企业的人性面", "McGregor", "第1章",
              ["人性假设决定管理方式", "Y 理论强调自我实现"]),
    ]
    r = chat_kb("授权要怎么搞？", _FakeRetriever(units), MockLLM())
    assert isinstance(r, KBChatResponse)
    assert r.answer and "情境领导" in r.answer, "Mock 应用真实检索上下文拼答案"
    assert len(r.sources) == 2
    assert r.sources[0].citation == "Hersey《领导力》第2章"
    assert r.sources[0].snippet == "按准备度调整领导风格"
    assert r.sources[0].evidence_level == "A"


def test_chat_empty_question_short_circuits():
    """空问题直接返回提示，绝不调用 LLM（省 token、可离线）。"""
    r = chat_kb("   ", _FakeRetriever([]), _ExplodingLLM())
    assert r.answer and r.sources == []


def test_chat_no_units_says_library_empty():
    """检索不到时如实说「库内暂无」，不编造。"""
    r = chat_kb("一个根本不存在的问题XYZ", _FakeRetriever([]), MockLLM())
    assert "库内暂无" in r.answer
    assert r.sources == []


def test_citation_format():
    u = _unit("KU-LEAD-001", "t", "书", "作者", "第3章", ["a", "b"])
    assert _citation(u) == "作者《书》第3章"


def test_chat_endpoint_real_retrieval(tmp_path):
    """端点走真实检索（knowledge 73 单元），来源须带「作者《书名》定位」引用。"""
    import app.api.main as main
    from app.services.context import build_context
    from fastapi.testclient import TestClient

    saved = main.ctx
    ctx = build_context(llm_mode="mock", output_dir=str(tmp_path))
    ctx.llm = MockLLM()
    main.ctx = ctx
    try:
        c = TestClient(main.app)
        r = c.post("/api/v1/kb/chat", json={"question": "如何授权？"})
        assert r.status_code == 200
        d = r.json()
        assert d["answer"]
        assert len(d["sources"]) >= 1, "授权应至少命中一个知识单元"
        for s in d["sources"]:
            assert "《" in s["citation"] and "》" in s["citation"], s["citation"]
    finally:
        main.ctx = saved
