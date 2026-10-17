"""P0 安全层：API Key 认证 + CORS 白名单 + 速率限制。

设计原则：
- 环境变量驱动：未配置时整体退化为「开放模式」——本地开发/测试零门槛，
  线上在 Render Dashboard 填 API_KEY 即启用（不进仓库）。
- /healthz 永远豁免（Render 健康检查不带 Key，否则部署直接失败）。
- 静态工作台首页免 Key（页面要能打开），API 调用要 Key；
  前端收到 401 时弹出 Key 输入引导，存 localStorage。
- 限流为进程内滑动窗口（单实例够用；多实例共享需 Redis，属后续项）。
- CORS 用 allow_origin_regex 放行本地任意端口（预览面板场景），
  线上域名必须显式列入 CORS_ORIGINS。

环境变量：
- API_KEY            设置即启用 Key 校验（请求头 X-API-Key）
- CORS_ORIGINS       逗号分隔的额外允许 origin（线上域名）
- RATE_LIMIT_IP      每 IP 每窗口请求数，0 = 不限，默认 120
- RATE_LIMIT_KEY     每 API Key 每窗口请求数，0 = 不限，默认 300
- RATE_LIMIT_WINDOW  窗口秒数，默认 60
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# 本地开发来源：127.0.0.1 / localhost 任意端口（同源或跨端口预览面板）
LOCAL_ORIGIN_REGEX = r"^http://(127\.0\.0\.1|localhost)(:\d+)?$"

# 无需认证、不限流的路径（Render 健康检查 + API 文档 + 静态首页）
DEFAULT_EXEMPT_PATHS = frozenset(
    {"/", "/healthz", "/docs", "/openapi.json", "/redoc", "/favicon.ico"}
)


@dataclass
class SecurityConfig:
    api_key: str | None = None
    cors_origins: list[str] = field(default_factory=list)
    rate_limit_ip: int = 120          # 0 = 不限
    rate_limit_key: int = 300         # 0 = 不限
    rate_window: int = 60
    exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS


def security_config_from_env(env: dict | None = None) -> SecurityConfig:
    env = env if env is not None else os.environ

    def _int(name: str, default: int) -> int:
        try:
            return max(0, int(env.get(name, "").strip() or default))
        except ValueError:
            return default

    origins = [o.strip() for o in env.get("CORS_ORIGINS", "").split(",") if o.strip()]
    return SecurityConfig(
        api_key=env.get("API_KEY", "").strip() or None,
        cors_origins=origins,
        rate_limit_ip=_int("RATE_LIMIT_IP", 120),
        rate_limit_key=_int("RATE_LIMIT_KEY", 300),
        rate_window=_int("RATE_LIMIT_WINDOW", 60),
    )


class APIKeyMiddleware(BaseHTTPMiddleware):
    """校验 X-API-Key 请求头；豁免路径与 OPTIONS 预检直接放行。"""

    def __init__(self, app, api_key: str, exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS):
        super().__init__(app)
        self._api_key = api_key
        self._exempt = set(exempt_paths)

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in self._exempt or request.method == "OPTIONS":
            return await call_next(request)
        if request.headers.get("X-API-Key") != self._api_key:
            return JSONResponse(
                status_code=401,
                content={"detail": "无效或缺失的 API Key（请求头 X-API-Key）"},
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """进程内滑动窗口限流：IP 与 API Key 双维度独立计数。

    clock 参数便于测试注入假时钟；asyncio 单线程下临界区无 await，天然原子。
    """

    def __init__(
        self,
        app,
        ip_limit: int = 120,
        key_limit: int = 300,
        window: int = 60,
        exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS,
        clock=time.monotonic,
    ):
        super().__init__(app)
        self._ip_limit = ip_limit
        self._key_limit = key_limit
        self._window = window
        self._exempt = set(exempt_paths)
        self._clock = clock
        self._buckets: dict[str, deque] = {}

    def _hit(self, bucket: str, limit: int) -> int | None:
        """记录一次命中；超限返回需等待秒数，未超限返回 None。"""
        if limit <= 0:
            return None
        now = self._clock()
        dq = self._buckets.setdefault(bucket, deque())
        while dq and now - dq[0] >= self._window:
            dq.popleft()
        if len(dq) >= limit:
            return max(1, int(self._window - (now - dq[0])) + 1)
        dq.append(now)
        return None

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in self._exempt or request.method == "OPTIONS":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        wait = self._hit(f"ip:{client_ip}", self._ip_limit)
        if wait is None:
            api_key = request.headers.get("X-API-Key")
            if api_key:
                wait = self._hit(f"key:{api_key}", self._key_limit)
        if wait is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": f"请求过于频繁，请 {wait}s 后重试"},
                headers={"Retry-After": str(wait)},
            )
        return await call_next(request)


def install_security(app: FastAPI, sec: SecurityConfig, rate_limit_clock=None) -> None:
    """把安全层装到 app 上（洋葱顺序：CORS 最外 → 限流 → Key 校验 → 路由）。

    注意 Starlette 的 add_middleware 是 insert(0)——最后 add 的在最外层，
    因此调用顺序必须与注释一致。rate_limit_clock 仅供测试注入假时钟。
    """
    if sec.api_key:
        app.add_middleware(APIKeyMiddleware, api_key=sec.api_key, exempt_paths=sec.exempt_paths)
    if sec.rate_limit_ip > 0 or sec.rate_limit_key > 0:
        app.add_middleware(
            RateLimitMiddleware,
            ip_limit=sec.rate_limit_ip,
            key_limit=sec.rate_limit_key,
            window=sec.rate_window,
            exempt_paths=sec.exempt_paths,
            clock=rate_limit_clock or time.monotonic,
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sec.cors_origins,
        allow_origin_regex=LOCAL_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
