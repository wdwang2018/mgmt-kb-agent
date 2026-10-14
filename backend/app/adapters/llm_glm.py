"""GLM（智谱）LLM 适配器（在线，OpenAI 兼容协议）。

配置（环境变量）：
- LLM_MODE=glm 时启用
- GLM_API_KEY 必填（也接受 ZHIPU_API_KEY 作为别名）
- GLM_BASE_URL 默认 https://open.bigmodel.cn/api/paas/v4
- GLM_MODEL 默认 glm-4-plus（可选 glm-4-air / glm-4 / glm-5 等）

工程约束：任何网络/解析失败都返回 fallback（调用方保证 fallback 合法），
保证质量门禁与溯源逻辑永远有完整输入，不会因模型抖动产生半成品课程包。
限流（429）与服务端错误（5xx）按指数退避重试，最多 3 次。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx

from ..ports import LLMClient

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (2.0, 5.0)


class GLMLLM(LLMClient):
    name = "glm-4-plus"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: float = 60.0):
        self.api_key = api_key or os.environ.get("GLM_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
        self.base_url = (base_url or os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")).rstrip("/")
        self.model = model or os.environ.get("GLM_MODEL", "glm-4-plus")
        self.name = self.model  # healthz/日志中反映真实模型名
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
        for attempt in range(_MAX_ATTEMPTS):
            retryable = False
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                    # 429（限流）/5xx（服务端错误）可重试；4xx（鉴权/参数错误）重试无意义
                    if resp.status_code == 429 or resp.status_code >= 500:
                        retryable = True
                        raise httpx.HTTPStatusError(
                            f"retryable status {resp.status_code}",
                            request=resp.request, response=resp)
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
            except Exception:
                if retryable and attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                return None
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
