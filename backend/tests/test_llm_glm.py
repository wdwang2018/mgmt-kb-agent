"""GLM 适配器测试：不打真实 API，用 MockTransport 验证请求构造/解析/降级。"""

import json

import httpx
import pytest

from app.adapters.llm_factory import get_llm
from app.adapters.llm_glm import GLMLLM


def test_factory_selects_glm(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "glm")
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    llm = get_llm()
    assert isinstance(llm, GLMLLM)
    assert llm.name == "glm-4-plus"


def test_glm_json_mode_request_and_parse(monkeypatch):
    captured = {}
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"objectives": [{"text": "能够……"}]}'}}]
        })

    def fake_client(**kw):
        return real_client(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))

    llm = GLMLLM(api_key="k123", base_url="https://example.test/v4", model="glm-4-air")
    monkeypatch.setattr(httpx, "Client", fake_client)

    out = llm.complete_json("objectives", "SYS", "USER", fallback={"objectives": []})
    assert out == {"objectives": [{"text": "能够……"}]}
    assert captured["url"] == "https://example.test/v4/chat/completions"
    assert captured["auth"] == "Bearer k123"
    assert captured["body"]["model"] == "glm-4-air"
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_glm_network_failure_falls_back(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr("app.adapters.llm_glm.time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    def fake_client(**kw):
        return real_client(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))

    llm = GLMLLM(api_key="k123")
    monkeypatch.setattr(httpx, "Client", fake_client)

    assert llm.complete_json("p", "s", "u", fallback={"ok": True}) == {"ok": True}
    assert llm.complete("p", "s", "u") == ""


def test_glm_retries_on_429_then_succeeds(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr("app.adapters.llm_glm.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    def fake_client(**kw):
        return real_client(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))

    llm = GLMLLM(api_key="k123")
    monkeypatch.setattr(httpx, "Client", fake_client)

    assert llm.complete_json("p", "s", "u", fallback={"ok": False}) == {"ok": True}
    assert calls["n"] == 3  # 两次 429 + 一次成功


def test_glm_no_retry_on_401(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr("app.adapters.llm_glm.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "invalid key"})

    def fake_client(**kw):
        return real_client(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))

    llm = GLMLLM(api_key="bad")
    monkeypatch.setattr(httpx, "Client", fake_client)

    assert llm.complete_json("p", "s", "u", fallback={"ok": 1}) == {"ok": 1}
    assert calls["n"] == 1  # 鉴权失败不重试


def test_glm_missing_key_short_circuits():
    llm = GLMLLM(api_key="")
    assert llm.complete_json("p", "s", "u", fallback={"ok": 1}) == {"ok": 1}
    assert llm.complete("p", "s", "u") == ""
