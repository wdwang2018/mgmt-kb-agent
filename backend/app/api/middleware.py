"""P2 可观测性：请求日志中间件（关联 ID + 延迟 + 状态码）。

- 透传/生成 X-Request-ID（外部传入则复用，便于跨服务追踪）；
- 记录 method / path / status / 毫秒延迟 / 客户端 IP / 是否带 Key；
- 未捕获异常：记错误日志（含堆栈）并计入错误指标后原样抛出；
- /healthz 是心跳，仅计入指标、不打 INFO 行（避免日志被探针刷屏）。
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

from ..observability.metrics import metrics

_logger = logging.getLogger("mgmt_kb.api.access")

# 心跳路径：只计数不打 INFO（Render 每几秒探一次）
_QUIET_PATHS = frozenset({"/healthz"})


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        corr = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        start = time.perf_counter()
        client_ip = request.client.host if request.client else "unknown"
        has_key = bool(request.headers.get("X-API-Key"))
        path = request.url.path
        try:
            response = await call_next(request)
        except Exception:
            ms = round((time.perf_counter() - start) * 1000, 1)
            metrics.record_error()
            _logger.error(
                "request failed | corr=%s method=%s path=%s ms=%.1f",
                corr, request.method, path, ms, exc_info=True)
            raise
        ms = round((time.perf_counter() - start) * 1000, 1)
        status = response.status_code
        metrics.record_request(status, ms)
        if path not in _QUIET_PATHS:
            _logger.info(
                "request | corr=%s method=%s path=%s status=%d ms=%.1f ip=%s key=%s",
                corr, request.method, path, status, ms, client_ip,
                "1" if has_key else "0")
        response.headers["X-Request-ID"] = corr
        return response
