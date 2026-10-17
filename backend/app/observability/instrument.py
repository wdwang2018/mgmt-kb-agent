"""LLM 透明计时 + token 计量包装器（P2 可观测性 + P3 token 计量）。

包裹任意 LLMClient，记录每次调用的耗时与 token 消耗到全局指标；
异常不吞、不改动返回值，finally 保证计量一定发生。
"""

from __future__ import annotations

import json
import time

from .metrics import metrics
from .tokens import estimate_tokens


class InstrumentedLLM:
    """透明包裹：保留 .name 与两个方法签名，仅追加耗时与 token 上报。"""

    def __init__(self, inner):
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    @staticmethod
    def _prompt_text(system: str, user: str) -> str:
        return f"{system or ''}{user or ''}"

    @staticmethod
    def _output_text(result) -> str:
        if isinstance(result, dict):
            try:
                return json.dumps(result, ensure_ascii=False)
            except TypeError:
                return str(result)
        return "" if result is None else str(result)

    def complete_json(self, *args, **kwargs):
        system = kwargs.get("system") or (args[1] if len(args) > 1 else "")
        user = kwargs.get("user") or (args[2] if len(args) > 2 else "")
        t = time.perf_counter()
        result = None
        try:
            result = self._inner.complete_json(*args, **kwargs)
            return result
        finally:
            ms = round((time.perf_counter() - t) * 1000, 1)
            metrics.record_llm_call(ms)
            metrics.record_llm_tokens(
                estimate_tokens(self._prompt_text(system, user)),
                estimate_tokens(self._output_text(result)),
            )

    def complete(self, *args, **kwargs):
        system = kwargs.get("system") or (args[1] if len(args) > 1 else "")
        user = kwargs.get("user") or (args[2] if len(args) > 2 else "")
        t = time.perf_counter()
        result = None
        try:
            result = self._inner.complete(*args, **kwargs)
            return result
        finally:
            ms = round((time.perf_counter() - t) * 1000, 1)
            metrics.record_llm_call(ms)
            metrics.record_llm_tokens(
                estimate_tokens(self._prompt_text(system, user)),
                estimate_tokens(self._output_text(result)),
            )

    def __getattr__(self, name):
        """透传未知属性到被包裹对象（如 FallbackLLM 的 active_backend/llm_chain）。"""
        inner = object.__getattribute__(self, "_inner")
        return getattr(inner, name)


def wrap_llm(inner):
    """包裹 LLM 适配器以开启调用计时；返回原对象类型透明兼容（含 .name）。"""
    return InstrumentedLLM(inner)
