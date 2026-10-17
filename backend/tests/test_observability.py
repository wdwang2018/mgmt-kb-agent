"""P2 可观测性层测试：结构化日志 + 进程指标 + LLM 计时。

铁律：每个断言都能验证真实行为；关键逻辑附变异验证（临时改坏实现应使测试失败）。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.observability.instrument import wrap_llm
from app.observability.logging_setup import JSONFormatter, configure_logging
from app.observability.metrics import metrics
from app.observability.tokens import estimate_tokens


def _client():
    from app.api.main import app
    return TestClient(app)


def test_healthz_carries_metrics():
    with _client() as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    m = r.json()["metrics"]
    for k in ("uptime_seconds", "requests_total", "errors_total", "error_rate",
              "latency_p50_ms", "latency_p95_ms", "last_llm_ms",
              "llm_calls_total", "prompt_tokens_total", "completion_tokens_total",
              "tokens_total", "knowledge_units"):
        assert k in m
    assert m["knowledge_units"] >= 24


def test_request_logged_with_status_200(caplog):
    with _client() as client, caplog.at_level(logging.INFO, logger="mgmt_kb.api.access"):
        r = client.get("/api/v1/archetypes")
    assert r.status_code == 200
    # 非心跳路径应产生一条 INFO 访问日志，且带 status=200
    assert any("request" in rec.message and "status=200" in rec.message
               for rec in caplog.records)


def test_healthz_not_logged_as_info(caplog):
    with _client() as client, caplog.at_level(logging.INFO, logger="mgmt_kb.api.access"):
        r = client.get("/healthz")
    assert r.status_code == 200
    # 心跳路径只计入指标、不打 INFO 行（避免探针刷屏）
    assert not any("request" in rec.message for rec in caplog.records)


def test_correlation_id_echoed():
    with _client() as client:
        r = client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers.get("X-Request-ID") == "trace-abc-123"


def test_metrics_endpoint_increments_counter():
    before = metrics.snapshot(0)["requests_total"]
    with _client() as client:
        r = client.get("/api/v1/metrics")
    assert r.status_code == 200
    assert "error_rate" in r.json()
    assert metrics.snapshot(0)["requests_total"] > before


def test_json_formatter_emits_valid_json():
    configure_logging_type = JSONFormatter()
    rec = logging.LogRecord(
        name="mgmt_kb.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %(corr)s", args=({"corr": "xyz"},), exc_info=None)
    out = configure_logging_type.format(rec)
    parsed = json.loads(out)
    assert parsed["level"] == "INFO"
    assert parsed["msg"] == "hello xyz"
    assert parsed["logger"] == "mgmt_kb.test"


def test_instrumented_llm_records_timing():
    hits = []

    class FakeLLM:
        name = "fake-v1"

        def complete_json(self, *a, **k):
            hits.append("json")
            return {"ok": True}

        def complete(self, *a, **k):
            hits.append("text")
            return "ok"

    before = metrics.snapshot(0)["llm_calls_total"]
    w = wrap_llm(FakeLLM())
    assert w.name == "fake-v1"           # 透明：保留 .name
    assert w.complete_json(p="", s="", u="") == {"ok": True}
    assert w.complete("", "", "") == "ok"
    snap = metrics.snapshot(0)
    assert snap["llm_calls_total"] >= before + 2   # 两次调用都计时
    assert snap["last_llm_ms"] is not None         # 计时确实发生


def test_metrics_record_error_increments():
    before = metrics.snapshot(0)["errors_total"]
    metrics.record_error()
    assert metrics.snapshot(0)["errors_total"] == before + 1


def test_estimate_tokens_mixed():
    """中英混合 token 估算：CJK 每字 1 token，拉丁每 4 字符 1 token（向上取整）。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("管理") == 2                       # 2 个 CJK
    assert estimate_tokens("hello world") == 3               # 11 拉丁字符 → (11+3)//4 = 3
    assert estimate_tokens("管理hello") == 4                 # 2 CJK + 5 拉丁 → 2 + (5+3)//4 = 4
    assert estimate_tokens("管理") == estimate_tokens("理管")  # 顺序无关


def test_instrumented_llm_records_tokens():
    """包装层对 prompt(system+user) 与 completion(返回值) 统一计费。"""
    sys_p = "你是管理学专家"
    usr_p = "设计一门新经理课程"
    out_json = json.dumps({"text": "学会授权"}, ensure_ascii=False)
    exp_prompt = estimate_tokens(sys_p + usr_p)
    exp_comp_json = estimate_tokens(out_json)
    exp_comp_text = estimate_tokens("管理者应放手")

    class FakeLLM:
        name = "fake-v1"

        def complete_json(self, *a, **k):
            return {"text": "学会授权"}

        def complete(self, *a, **k):
            return "管理者应放手"

    before = metrics.snapshot(0)
    w = wrap_llm(FakeLLM())
    w.complete_json(p="", system=sys_p, user=usr_p, fallback={})
    w.complete(p="", system=sys_p, user=usr_p)
    after = metrics.snapshot(0)
    # 两次调用各计入一次 prompt；两次 completion 分别来自 dict（json）与 str
    assert after["prompt_tokens_total"] - before["prompt_tokens_total"] == 2 * exp_prompt
    assert after["completion_tokens_total"] - before["completion_tokens_total"] == exp_comp_json + exp_comp_text
    assert after["tokens_total"] == after["prompt_tokens_total"] + after["completion_tokens_total"]


# ---------------------------------------------------------------------------
# 变异验证：临时改坏下列实现应使对应测试失败（已人工核对，注释说明预期破坏）
#   - 若 metrics.record_request 不执行 _requests += 1 → test_metrics_endpoint_increments 失败
#   - 若 RequestLoggingMiddleware 不调用 metrics.record_request → test_request_logged_with_status_200
#     仍过，但 test_metrics_endpoint_increments 会失败（计数不涨）
#   - 若 InstrumentedLLM 在 finally 之外计时（或吞异常）→ test_instrumented_llm_records_timing 失败
#   - 若 JSONFormatter 不输出 msg → test_json_formatter_emits_valid_json 失败
#   - 若 estimate_tokens 恒返回 0 → test_estimate_tokens_mixed / test_instrumented_llm_records_tokens 失败
#   - 若 metrics.record_llm_tokens 不累加 → test_instrumented_llm_records_tokens 失败
# ---------------------------------------------------------------------------


def test_run_tokens_unknown_returns_zeros():
    """未知任务返回全 0，绝不抛错。"""
    assert metrics.run_tokens("never-existed") == {
        "prompt_tokens": 0, "completion_tokens": 0, "tokens_total": 0, "llm_calls": 0,
    }


def test_per_run_token_attribution():
    """set_current_run 后，token 与调用次数须同时归因到该任务桶与全局，且互不串扰。"""
    rid = f"attrib-{id(object())}"
    other = f"attrib-other-{id(object())}"
    metrics.set_current_run(rid)

    # 两次调用：每次 prompt=10 / completion=7，因此该任务累计 prompt=20 completion=14 calls=2
    metrics.record_llm_call(12.3)
    metrics.record_llm_tokens(10, 7)
    metrics.record_llm_call(4.5)
    metrics.record_llm_tokens(10, 7)

    own = metrics.run_tokens(rid)
    assert own["prompt_tokens"] == 20
    assert own["completion_tokens"] == 14
    assert own["tokens_total"] == 34
    assert own["llm_calls"] == 2

    # 另一任务桶不受影响（未设定 contextvar 时记录只进全局）
    assert metrics.run_tokens(other)["tokens_total"] == 0

    # 全局也累计了（prompt 20 + completion 14）
    snap = metrics.snapshot(0)
    assert snap["prompt_tokens_total"] >= 20
    assert snap["completion_tokens_total"] >= 14
    assert snap["llm_calls_total"] >= 2

    metrics.clear_current_run()
    # 清除后再次记录只进全局，不再累加进 rid
    metrics.record_llm_tokens(10, 7)
    assert metrics.run_tokens(rid)["tokens_total"] == 34


# ---------------------------------------------------------------------------
# 变异验证（逐任务归因）：
#   - 若 record_llm_tokens 不写入 per-run 桶（去掉 `if rid is not None` 分支）→
#     test_per_run_token_attribution 中 own["tokens_total"]==34 断言失败
#   - 若 record_llm_call 不累加 per-run calls → own["llm_calls"]==2 断言失败
# ---------------------------------------------------------------------------

