"""结构化日志配置（P2 可观测性）。

提供：
- observability_config_from_env：从环境变量读取 LOG_LEVEL / LOG_FORMAT / LOG_DIR；
- configure_logging：装配根日志（控制台 + 可选文件落盘，text/json 双格式）；
- get_logger：按模块取 logger（统一前缀 mgmt_kb）。

落盘目录不存在/不可写时自动退回仅控制台，绝不因日志配置失败阻断启动。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 日志默认落盘目录：仓库根 working/logs/（已在 .gitignore 排除）
DEFAULT_LOG_DIR = Path(__file__).resolve().parents[3] / "working" / "logs"

# LogRecord 自带的保留字段，JSON 序列化时跳过，避免与业务字段冲突
_RESERVED_FIELDS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


def _level(name: str) -> int:
    return getattr(logging, (name or "INFO").upper(), logging.INFO)


@dataclass
class ObservabilityConfig:
    log_level: str = "INFO"
    log_format: str = "text"            # text | json
    log_dir: str | None = None         # 非空则落盘（RotatingFileHandler）
    enabled: bool = True


def observability_config_from_env(env: dict | None = None) -> ObservabilityConfig:
    env = env if env is not None else os.environ
    fmt = (env.get("LOG_FORMAT") or "text").lower()
    log_dir = env.get("LOG_DIR") or None
    return ObservabilityConfig(
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
        log_format="json" if fmt == "json" else "text",
        log_dir=log_dir,
    )


class JSONFormatter(logging.Formatter):
    """把日志记录序列化为单行 JSON，业务 extra 字段一并保留（便于集中采集）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in _RESERVED_FIELDS and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(cfg: ObservabilityConfig | None = None) -> None:
    """配置根日志：控制台（始终）+ 可选文件落盘；text/json 双格式。

    重复调用安全：先清掉旧 handler，避免 lifespan/测试多次构建时重复堆叠。
    """
    cfg = cfg or ObservabilityConfig()
    root = logging.getLogger()
    root.setLevel(_level(cfg.log_level))
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter: logging.Formatter = (
        JSONFormatter() if cfg.log_format == "json" else
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if cfg.log_dir:
        try:
            d = Path(cfg.log_dir)
            d.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                d / "api.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
            fh.setFormatter(formatter)
            root.addHandler(fh)
        except OSError:
            root.warning("日志落盘目录不可用，已退回仅控制台: %s", cfg.log_dir)

    # 第三方库降噪，避免淹没业务日志
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
