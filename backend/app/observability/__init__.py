"""P2 可观测性层：结构化日志 + 进程指标 + LLM 调用计时。

设计原则：
- 环境变量驱动，未配置时默认 INFO / text 格式 / 不落盘（仅控制台），零门槛；
- 日志格式支持 json（便于 Render/ELK 等集中采集）与 text（本地可读）；
- 指标为进程内累加器（单实例够用；多实例聚合需 Prometheus/Redis，属后续项）；
- LLM 调用用透明包装器计时，异常不吞、不影响主链路。
"""

from .logging_setup import (
    ObservabilityConfig,
    configure_logging,
    get_logger,
    observability_config_from_env,
)
from .metrics import Metrics, metrics

__all__ = [
    "ObservabilityConfig",
    "configure_logging",
    "get_logger",
    "observability_config_from_env",
    "Metrics",
    "metrics",
]
