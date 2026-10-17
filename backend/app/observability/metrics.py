"""进程级指标累加器（P2 可观测性）。

单实例够用；多实例聚合需 Prometheus/Redis 导出，属后续项。
所有累加在锁内完成，asyncio 单线程 + 多线程部署（如 ThreadPoolExecutor 调 LLM）均安全。

Token 计量额外提供「按任务（run_id）归因」能力（P3 token 计量增强）：
通过 contextvar 在当前 LLM 调用链上绑定 run_id，使每次 token 消耗同时累加到
进程级总量与对应任务的独立桶；任务桶随运行记录持久化到 state.json，重启后仍可查看。
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections import deque
from dataclasses import dataclass, field

# 当前正在执行的任务（run_id）；由流水线 _invoke 在开始执行时设定、结束时清除。
# 用 contextvar 而非普通变量，保证即便未来 LangGraph 切线程也能正确归属性。
_current_run: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mgmt_current_run", default=None
)

# 每个任务的 token 桶：{run_id: {"prompt": int, "completion": int, "calls": int}}
_RUN_TOKENS: dict[str, dict[str, int]] = {}


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

    # ------------------------------------------------------------------ 任务绑定
    def set_current_run(self, run_id: str) -> None:
        """在执行某任务（run_id）期间调用，使后续 token/调用计数归属该任务。"""
        _current_run.set(run_id)

    def clear_current_run(self) -> None:
        """任务执行结束调用，解绑。务必在 finally 中调用。"""
        _current_run.set(None)

    @staticmethod
    def run_tokens(run_id: str) -> dict:
        """返回某任务的累计 token 桶（幂等，未知任务返回全 0）。"""
        b = _RUN_TOKENS.get(run_id, {})
        prompt = b.get("prompt", 0)
        completion = b.get("completion", 0)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "tokens_total": prompt + completion,
            "llm_calls": b.get("calls", 0),
        }

    # ------------------------------------------------------------------ 请求/错误
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

    # ------------------------------------------------------------------ LLM
    def record_llm_call(self, ms: float) -> None:
        """记录一次 LLM 调用耗时（毫秒），并归属当前任务。"""
        with self._lock:
            self._last_llm_ms = ms
            self._llm_calls += 1
            rid = _current_run.get()
            if rid is not None:
                _RUN_TOKENS.setdefault(rid, {"prompt": 0, "completion": 0, "calls": 0})["calls"] += 1

    def record_llm_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """记录一次 LLM 调用的 token 消耗（估算值，见 tokens.estimate_tokens），并归属当前任务。"""
        p = max(0, prompt_tokens)
        c = max(0, completion_tokens)
        with self._lock:
            self._prompt_tokens += p
            self._completion_tokens += c
            rid = _current_run.get()
            if rid is not None:
                b = _RUN_TOKENS.setdefault(rid, {"prompt": 0, "completion": 0, "calls": 0})
                b["prompt"] += p
                b["completion"] += c

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
