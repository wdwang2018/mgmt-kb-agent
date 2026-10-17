"""进程级指标累加器（P2 可观测性）。

单实例够用；多实例聚合需 Prometheus/Redis 导出，属后续项。
所有累加在锁内完成，asyncio 单线程 + 多线程部署（如 ThreadPoolExecutor 调 LLM）均安全。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Metrics:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _start: float = field(default_factory=time.monotonic)
    _requests: int = 0
    _errors: int = 0
    _latencies: deque = field(default_factory=lambda: deque(maxlen=500))
    _last_llm_ms: float | None = None
    _llm_calls: int = 0
    _prompt_tokens: int = 0
    _completion_tokens: int = 0

    def record_request(self, status: int, ms: float) -> None:
        """记录一次 HTTP 请求（含状态码与毫秒延迟）。"""
        with self._lock:
            self._requests += 1
            self._latencies.append(ms)
            if status >= 500:
                self._errors += 1

    def record_error(self) -> None:
        """记录一次未捕获异常（5xx 之外也计入错误总数）。"""
        with self._lock:
            self._errors += 1

    def record_llm_call(self, ms: float) -> None:
        """记录一次 LLM 调用耗时（毫秒）。"""
        with self._lock:
            self._last_llm_ms = ms
            self._llm_calls += 1

    def record_llm_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """记录一次 LLM 调用的 token 消耗（估算值，见 tokens.estimate_tokens）。"""
        with self._lock:
            self._prompt_tokens += max(0, prompt_tokens)
            self._completion_tokens += max(0, completion_tokens)

    def snapshot(self, unit_count: int) -> dict:
        """导出当前指标快照（供 /healthz 与 /api/v1/metrics 使用）。"""
        with self._lock:
            lat = sorted(self._latencies)
            n = len(lat)
            p50 = lat[n // 2] if n else None
            p95 = lat[int(n * 0.95)] if n else None
            prompt = self._prompt_tokens
            completion = self._completion_tokens
            return {
                "uptime_seconds": round(time.monotonic() - self._start, 1),
                "requests_total": self._requests,
                "errors_total": self._errors,
                "error_rate": round(self._errors / max(1, self._requests), 4),
                "latency_p50_ms": round(p50, 1) if p50 is not None else None,
                "latency_p95_ms": round(p95, 1) if p95 is not None else None,
                "last_llm_ms": self._last_llm_ms,
                "llm_calls_total": self._llm_calls,
                "prompt_tokens_total": prompt,
                "completion_tokens_total": completion,
                "tokens_total": prompt + completion,
                "knowledge_units": unit_count,
            }


# 全局单例：API 与 LLM 计时包装器共享同一累加器
metrics = Metrics()
