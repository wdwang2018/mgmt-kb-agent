"""DeepSeek LLM 适配器（在线，OpenAI 兼容协议）。

配置（环境变量）：
- LLM_MODE=deepseek 时启用
- DEEPSEEK_API_KEY 必填
- DEEPSEEK_BASE_URL 默认 https://api.deepseek.com
- DEEPSEEK_MODEL 默认 deepseek-chat

工程约束：任何网络/解析失败都返回 fallback（调用方保证 fallback 合法），
保证质量门禁与溯源逻辑永远有完整输入，不会因模型抖动产生半成品课程包。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from ..ports import LLMClient


class DeepSeekLLM(LLMClient):
    name = "deepseek-chat"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: float = 60.0):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self.timeout = timeout

    def _chat(self, system: str, user: str, json_mode: bool) -> str | None:
        if not self.api_key:
            return None
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception:
            return None

    def complete_json(self, purpose: str, system: str, user: str, fallback: dict[str, Any]) -> dict[str, Any]:
        content = self._chat(system, user, json_mode=True)
        if not content:
            return fallback
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and parsed:
                return parsed
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.S)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                    if isinstance(parsed, dict) and parsed:
                        return parsed
                except json.JSONDecodeError:
                    pass
        return fallback

    def complete(self, purpose: str, system: str, user: str) -> str:
        return self._chat(system, user, json_mode=False) or ""
