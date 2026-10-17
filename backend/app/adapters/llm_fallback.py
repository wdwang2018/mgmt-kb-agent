"""LLM 故障降级链（P3 韧性层）。

线上当前用 GLM（智谱）。当 GLM 触发限流(429)/网络抖动/服务端 5xx 时，若没有降级，
整条课程设计流水线会硬失败。本类把多个 LLM 后端串成一条链，按优先级依次尝试，
任一后端成功即采用其结果；全部失败才退回调用方 fallback（保证门禁与溯源永远有完整输入）。

失败判定（与现有适配器约定一致）：
- complete_json：适配器在解析/网络失败时返回「调用方传入的 fallback 对象本身」（identity），
  故用 `result is fallback` 侦测失败；
- complete：适配器失败时返回空字符串，故以非空判定成功。

子后端若抛异常（适配器本应自吞，但防御性兜底），链自动跳过该后端，不阻断整体。

配置（环境变量，见 llm_factory.py）：
- LLM_MODE=fallback 启用
- LLM_FALLBACK_ORDER 逗号分隔的优先级，默认 "glm,deepseek,mock"
- 各后端密钥仍按各自适配器读取（GLM_API_KEY / DEEPSEEK_API_KEY）；
  缺密钥的后端瞬时退回 fallback（适配器 `if not api_key: return None`），被链自动跳过，无需预过滤。
"""

from __future__ import annotations

from typing import Any

from ..ports import LLMClient
from .llm_deepseek import DeepSeekLLM
from .llm_glm import GLMLLM
from .llm_mock import MockLLM


# 可编排的后端构造器（降级链只在此三者内组合）
_BACKEND_CTORS = {
    "glm": GLMLLM,
    "deepseek": DeepSeekLLM,
    "mock": MockLLM,
}


class FallbackLLM(LLMClient):
    """多后端故障降级链：按序尝试，首个成功者胜出。"""

    def __init__(self, backends: list[LLMClient]):
        if not backends:
            raise ValueError("FallbackLLM 至少需要一个后端")
        self._backends = backends
        self.name = "fallback(" + "→".join(b.name for b in backends) + ")"
        self.active_backend: str | None = None  # 最近一次成功采用的后端名（metrics/healthz 用）
        self.usage: dict[str, int] = {b.name: 0 for b in backends}  # 各后端被采用次数

    @property
    def llm_chain(self) -> list[str]:
        """降级链顺序（后端名列表），供 healthz 透明展示。"""
        return [b.name for b in self._backends]

    def _record(self, backend: LLMClient) -> None:
        self.active_backend = backend.name
        self.usage[backend.name] = self.usage.get(backend.name, 0) + 1

    def complete_json(self, purpose: str, system: str, user: str,
                      fallback: dict[str, Any]) -> dict[str, Any]:
        for b in self._backends:
            try:
                result = b.complete_json(purpose, system, user, fallback)
            except Exception:
                # 防御性兜底：子后端异常不应中断链
                continue
            if result is not fallback:
                self._record(b)
                return result
        return fallback

    def complete(self, purpose: str, system: str, user: str) -> str:
        for b in self._backends:
            try:
                result = b.complete(purpose, system, user)
            except Exception:
                continue
            if result:
                self._record(b)
                return result
        return ""


def build_fallback_chain(order: list[str] | None = None) -> LLMClient:
    """按名称列表组装降级链；order 为 None 用默认 glm,deepseek,mock。

    空列表 / 全部名称无效 → 退回 MockLLM；单一有效后端 → 直接返回该后端（不包一层链）。
    """
    if order is None:
        order = ["glm", "deepseek", "mock"]
    names = [o.strip().lower() for o in order if o.strip()]
    backends: list[LLMClient] = []
    for name in names:
        ctor = _BACKEND_CTORS.get(name)
        if ctor is None:
            continue
        try:
            backends.append(ctor())
        except Exception:
            # 构造失败（极端情况）不阻断，跳过该后端
            continue
    if not backends:
        return MockLLM()
    if len(backends) == 1:
        return backends[0]
    return FallbackLLM(backends)
