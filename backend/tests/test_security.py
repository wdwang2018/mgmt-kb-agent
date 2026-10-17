"""P0 安全层测试：API Key / CORS 白名单 / 限流三中间件 + 环境配置解析。

验证策略：全部用裸 FastAPI app + install_security 接线（不触建 AppContext），
保证测试轻量、确定。变异验证记录见文末注释。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.security import (
    SecurityConfig,
    install_security,
    security_config_from_env,
)


@pytest.fixture
def app():
    a = FastAPI()

    @a.get("/")
    def index():
        return {"page": "workbench"}

    @a.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @a.get("/api/v1/kb/units")
    def units():
        return {"units": 26}

    return a


class FakeClock:
    """可操控的单调时钟，用于限流窗口恢复测试。"""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ================================================================ API Key

def test_api_key_open_when_not_configured(app):
    """未配置 API_KEY：开放模式，请求直接放行。"""
    install_security(app, SecurityConfig())
    r = TestClient(app).get("/api/v1/kb/units")
    assert r.status_code == 200


def test_api_key_rejects_missing_and_wrong(app):
    """配置 API_KEY 后：缺 header / 错 key 均 401，正确 key 200。"""
    install_security(app, SecurityConfig(api_key="secret-123"))
    c = TestClient(app)
    assert c.get("/api/v1/kb/units").status_code == 401
    assert c.get("/api/v1/kb/units", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/api/v1/kb/units", headers={"X-API-Key": "secret-123"}).status_code == 200


def test_api_key_exempt_paths(app):
    """/healthz 与静态首页 / 永远免 Key（Render 健康检查不带 Key）。"""
    install_security(app, SecurityConfig(api_key="secret-123"))
    c = TestClient(app)
    assert c.get("/healthz").status_code == 200
    assert c.get("/").status_code == 200


def test_api_key_options_preflight_not_blocked(app):
    """OPTIONS 预检不被 Key 中间件拦截（由 CORS 层应答）。"""
    install_security(app, SecurityConfig(api_key="secret-123"))
    r = TestClient(app).options("/api/v1/kb/units")
    assert r.status_code != 401


# ================================================================ 限流

def test_rate_limit_ip_blocks_after_limit(app):
    """IP 维度：超过 RATE_LIMIT_IP 后 429，带 Retry-After。"""
    install_security(app, SecurityConfig(rate_limit_ip=3, rate_limit_key=0))
    c = TestClient(app)
    for _ in range(3):
        assert c.get("/api/v1/kb/units").status_code == 200
    r = c.get("/api/v1/kb/units")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_rate_limit_key_bucket_independent(app):
    """Key 维度独立计数：同 key 超限 429，换 key 放行（IP 桶未满时）。

    注意：不配 api_key（开放模式）——此处只验证限流器按 X-API-Key 取值分桶，
    与 Key 是否合法无关；401 请求也会占 IP 桶额度（防暴力探测，有意为之）。
    """
    install_security(app, SecurityConfig(rate_limit_ip=10, rate_limit_key=2))
    c = TestClient(app)
    h1 = {"X-API-Key": "k1"}
    h2 = {"X-API-Key": "k2"}
    assert c.get("/api/v1/kb/units", headers=h1).status_code == 200
    assert c.get("/api/v1/kb/units", headers=h1).status_code == 200
    assert c.get("/api/v1/kb/units", headers=h1).status_code == 429   # k1 桶满
    assert c.get("/api/v1/kb/units", headers=h2).status_code == 200   # k2 桶独立


def test_rate_limit_window_recovers(app):
    """滑动窗口：时钟推进过窗口后配额恢复。"""
    clock = FakeClock()
    install_security(app, SecurityConfig(rate_limit_ip=2, rate_limit_key=0, rate_window=60),
                     rate_limit_clock=clock)
    c = TestClient(app)
    assert c.get("/api/v1/kb/units").status_code == 200
    assert c.get("/api/v1/kb/units").status_code == 200
    assert c.get("/api/v1/kb/units").status_code == 429
    clock.advance(61)
    assert c.get("/api/v1/kb/units").status_code == 200


def test_rate_limit_exempt_healthz_not_counted(app):
    """/healthz 豁免且不占 IP 桶额度。"""
    install_security(app, SecurityConfig(rate_limit_ip=1, rate_limit_key=0))
    c = TestClient(app)
    for _ in range(5):
        assert c.get("/healthz").status_code == 200
    assert c.get("/api/v1/kb/units").status_code == 200  # healthz 未消耗额度


def test_rate_limit_disabled_with_zero(app):
    """IP 与 Key 限额均 0：不装限流中间件。"""
    install_security(app, SecurityConfig(rate_limit_ip=0, rate_limit_key=0))
    c = TestClient(app)
    for _ in range(20):
        assert c.get("/api/v1/kb/units").status_code == 200


# ================================================================ CORS

def test_cors_local_origin_any_port(app):
    """本地来源（127.0.0.1/localhost 任意端口）预检放行——预览面板场景。"""
    install_security(app, SecurityConfig())
    c = TestClient(app)
    for origin in ("http://127.0.0.1:9999", "http://localhost:5173"):
        r = c.options("/api/v1/kb/units", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        })
        assert r.status_code == 200, origin
        assert r.headers["Access-Control-Allow-Origin"] == origin


def test_cors_evil_origin_rejected(app):
    """白名单外来源的预检被拒。"""
    install_security(app, SecurityConfig())
    r = TestClient(app).options("/api/v1/kb/units", headers={
        "Origin": "https://evil.example.com",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code != 200 or "Access-Control-Allow-Origin" not in r.headers


def test_cors_configured_origin_allowed(app):
    """CORS_ORIGINS 显式配置的域名（线上）预检放行。"""
    install_security(app, SecurityConfig(cors_origins=["https://mgmt-kb-agent.onrender.com"]))
    r = TestClient(app).options("/api/v1/kb/units", headers={
        "Origin": "https://mgmt-kb-agent.onrender.com",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == "https://mgmt-kb-agent.onrender.com"


# ================================================================ 配置解析

def test_config_from_env(monkeypatch):
    """环境变量解析：启用项、逗号分隔 origin、非法值回退默认。"""
    monkeypatch.setenv("API_KEY", " env-key ")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.com, https://b.com ,")
    monkeypatch.setenv("RATE_LIMIT_IP", "30")
    monkeypatch.setenv("RATE_LIMIT_KEY", "abc")          # 非法 → 默认 300
    monkeypatch.setenv("RATE_LIMIT_WINDOW", "30")
    sec = security_config_from_env()
    assert sec.api_key == "env-key"
    assert sec.cors_origins == ["https://a.com", "https://b.com"]
    assert sec.rate_limit_ip == 30
    assert sec.rate_limit_key == 300
    assert sec.rate_window == 30


def test_config_from_env_defaults(monkeypatch):
    """空环境：全部默认（开放模式 + 默认限额 + 无额外 origin）。"""
    for name in ("API_KEY", "CORS_ORIGINS", "RATE_LIMIT_IP", "RATE_LIMIT_KEY", "RATE_LIMIT_WINDOW"):
        monkeypatch.delenv(name, raising=False)
    sec = security_config_from_env()
    assert sec.api_key is None
    assert sec.cors_origins == []
    assert sec.rate_limit_ip == 120 and sec.rate_limit_key == 300 and sec.rate_window == 60


# ================================================================ 全栈集成

def test_full_stack_flow(app):
    """全配置下的完整链路：preflight → 401 → 带 Key 200 → 限流 429。"""
    install_security(app, SecurityConfig(api_key="k", rate_limit_ip=100, rate_limit_key=2))
    c = TestClient(app)
    ok = {"X-API-Key": "k"}
    # 预检放行（必须带 Access-Control-Request-Method 才会被 CORS 层按预检处理）
    pre = c.options("/api/v1/kb/units", headers={
        "Origin": "http://127.0.0.1:1234",
        "Access-Control-Request-Method": "GET",
    })
    assert pre.status_code == 200
    # 无 Key 401
    assert c.get("/api/v1/kb/units").status_code == 401
    # 带 Key 前两次 200
    assert c.get("/api/v1/kb/units", headers=ok).status_code == 200
    assert c.get("/api/v1/kb/units", headers=ok).status_code == 200
    # 第三次超 Key 桶 → 429
    assert c.get("/api/v1/kb/units", headers=ok).status_code == 429
    # healthz 全程畅通
    assert c.get("/healthz").status_code == 200


# ================================================================
# 变异验证记录（2026-10-17，本项目铁律：测试必须有牙）：
# M1 删掉 APIKeyMiddleware 的 exempt 判断 → test_api_key_exempt_paths 失败 ✓
# M2 RateLimitMiddleware._hit 不 append 只查长度 → test_rate_limit_ip_blocks_after_limit 失败 ✓
# M3 _hit 的 key 桶与 ip 桶共用同一 bucket 名 → test_rate_limit_key_bucket_independent 失败 ✓
# M4 CORS allow_origin_regex 改成 "" → test_cors_local_origin_any_port 失败 ✓
# ================================================================
