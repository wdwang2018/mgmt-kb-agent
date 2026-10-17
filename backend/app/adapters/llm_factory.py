"""适配器工厂：按环境变量选择 LLM 实现（默认 mock，保证离线可跑）。"""

from __future__ import annotations

import os

from ..ports import LLMClient
from .llm_deepseek import DeepSeekLLM
from .llm_fallback import build_fallback_chain
from .llm_glm import GLMLLM
from .llm_mock import MockLLM


def get_llm() -> LLMClient:
    mode = os.environ.get("LLM_MODE", "mock").lower()
    if mode == "deepseek":
        return DeepSeekLLM()
    if mode == "glm":
        return GLMLLM()
    if mode == "fallback":
        order = os.environ.get("LLM_FALLBACK_ORDER", "glm,deepseek,mock").split(",")
        return build_fallback_chain(order)
    return MockLLM()
