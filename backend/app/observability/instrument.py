"""LLM 透明计时包装器（P2 可观测性）。

包裹任意 LLMClient，记录每次调用的耗时到全局指标；
异常不吞、不改动返回值，仅用 finally 保证计时一定发生。
"""

from __future__ import annotations

import time

from .metrics import metrics


class InstrumentedLLM:
    """透明包裹：保留 .name 与两个方法签名，仅追加耗时上报。"""

    def __init__(self, inner):
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    def complete_json(self, *args, **kwargs):
        t = time.perf_counter()
        try:
            return self._inner.complete_json(*args, **kwargs)
        finally:
            metrics.record_llm_call(round((time.perf_counter() - t) * 1000, 1))

    def complete(self, *args, **kwargs):
        t = time.perf_counter()
        try:
            return self._inner.complete(*args, **kwargs)
        finally:
            metrics.record_llm_call(round((time.perf_counter() - t) * 1000, 1))


def wrap_llm(inner):
    """包裹 LLM 适配器以开启调用计时；返回原对象类型透明兼容（含 .name）。"""
    return InstrumentedLLM(inner)
