"""把可观测性装到 FastAPI app：配置结构化日志 + 请求日志中间件。

与 install_security 同形：install_observability(app, cfg) 在 app 创建后调用一次。
enabled=False 时整体跳过（极端裁剪场景），等价于不装本层。
"""

from __future__ import annotations

from ..observability.logging_setup import (
    ObservabilityConfig,
    configure_logging,
    observability_config_from_env,
)
from .middleware import RequestLoggingMiddleware


def install_observability(app, cfg: ObservabilityConfig | None = None) -> None:
    cfg = cfg or observability_config_from_env()
    if not cfg.enabled:
        return
    configure_logging(cfg)
    # 最后 add 的最外层（Starlette insert(0)），因此本中间件包住安全层与路由
    app.add_middleware(RequestLoggingMiddleware)
