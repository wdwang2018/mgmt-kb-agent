"""LLM 故障降级链测试：顺序尝试 / 异常跳过 / 全失败回落 / 工厂装配。

含变异验证（mutation）：临时改坏实现，确认测试能失败（测试有牙）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.llm_fallback import FallbackLLM, build_fallback_chain
from app.adapters.llm_factory import get_llm


class _Fake:
    """测试桩：可指定返回真实结果 / 返回 fallback（失败）/ 抛异常 / 空文本。"""

    def __init__(self, name, *, fail_json=False, json_result=None,
                 complete_text="", boom=False):
        self.name = name
        self._fail_json = fail_json
        self._json_result = json_result
        self._complete_text = complete_text
        self._boom = boom
        self.json_calls = 0
        self.complete_calls = 0

    def complete_json(self, purpose, system, user, fallback):
        self.json_calls += 1
        if self._boom:
            raise RuntimeError("boom")
        if self._fail_json:
            return fallback
        return self._json_result

    def complete(self, purpose, system, user):
        self.complete_calls += 1
        if self._boom:
            raise RuntimeError("boom")
        return self._complete_text


def test_fallback_returns_first_success_and_skips_rest():
    b0 = _Fake("a", fail_json=True)            # 失败
    b1 = _Fake("b", json_result={"ok": True})  # 成功
    b2 = _Fake("c", json_result={"ok": False}) # 不应被调用
    chain = FallbackLLM([b0, b1, b2])
    res = chain.complete_json("p", "s", "u", fallback={})
    assert res == {"ok": True}
    assert chain.active_backend == "b"
    assert chain.usage == {"a": 0, "b": 1, "c": 0}
    assert b0.json_calls == 1 and b1.json_calls == 1 and b2.json_calls == 0


def test_fallback_skips_booming_backend():
    b0 = _Fake("a", boom=True)
    b1 = _Fake("b", json_result={"x": 1})
    chain = FallbackLLM([b0, b1])
    res = chain.complete_json("p", "s", "u", fallback={})
    assert res == {"x": 1}
    assert chain.active_backend == "b"
    assert b0.json_calls == 1  # 异常也计一次尝试


def test_fallback_all_fail_returns_passed_fallback_by_identity():
    b0 = _Fake("a", fail_json=True)
    b1 = _Fake("b", fail_json=True)
    chain = FallbackLLM([b0, b1])
    fb = {"default": 1}
    res = chain.complete_json("p", "s", "u", fallback=fb)
    assert res is fb  # 必须原样退回调用方 fallback（门禁/溯源依赖此契约）
    assert chain.active_backend is None


def test_fallback_complete_empty_falls_through():
    b0 = _Fake("a", complete_text="")
    b1 = _Fake("b", complete_text="hello")
    chain = FallbackLLM([b0, b1])
    assert chain.complete("p", "s", "u") == "hello"
    assert chain.active_backend == "b"


def test_fallback_complete_all_empty_returns_empty():
    b0 = _Fake("a", complete_text="")
    b1 = _Fake("b", complete_text="")
    chain = FallbackLLM([b0, b1])
    assert chain.complete("p", "s", "u") == ""
    assert chain.active_backend is None


def test_fallback_name_and_chain_property():
    chain = FallbackLLM([_Fake("a"), _Fake("b")])
    assert chain.name == "fallback(a→b)"
    assert chain.llm_chain == ["a", "b"]


def test_build_chain_order_and_collapse():
    # 多后端 → FallbackLLM
    chain = build_fallback_chain(["mock", "glm"])
    assert isinstance(chain, FallbackLLM)
    assert chain.llm_chain[0].startswith("mock")
    assert chain.llm_chain[1].startswith("glm")

    # 单一有效后端 → 直接返回该后端（不包链）
    single = build_fallback_chain(["mock"])
    assert type(single).__name__ == "MockLLM"

    # 全部无效 / 空 → 退回 MockLLM
    empty = build_fallback_chain([])
    assert type(empty).__name__ == "MockLLM"
    only_bogus = build_fallback_chain(["nope"])
    assert type(only_bogus).__name__ == "MockLLM"


def test_build_chain_skips_invalid_entries():
    chain = build_fallback_chain(["bogus", "mock", "deepseek"])
    assert isinstance(chain, FallbackLLM)
    assert chain.llm_chain == ["mock-deterministic-v1", "deepseek-chat"]


def test_factory_fallback_mode(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "fallback")
    monkeypatch.setenv("LLM_FALLBACK_ORDER", "mock,glm")
    llm = get_llm()
    assert isinstance(llm, FallbackLLM)
    assert llm.llm_chain[0].startswith("mock")
    assert llm.llm_chain[1].startswith("glm")


def test_factory_default_still_mock(monkeypatch):
    # 清除可能由 .env 注入的 LLM_MODE，确认默认路径仍是 mock
    monkeypatch.delenv("LLM_MODE", raising=False)
    assert type(get_llm()).__name__ == "MockLLM"
