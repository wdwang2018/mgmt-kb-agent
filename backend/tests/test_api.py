"""API 冒烟测试：FastAPI 路由 + HITL 挂起/恢复的 HTTP 层验证。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

NEED = {
    "audience": "新任经理", "audience_level": 2, "headcount": 24,
    "duration_minutes": 360,
    "business_pain": "新经理仍埋头做事，下属得不到成长",
    "desired_behavior": "会授权、会给反馈、坚持 1on1",
    "industry": "互联网", "theme_hint": "新任管理者转型",
}


def _client():
    from app.api.main import app
    return TestClient(app)


def test_healthz():
    with _client() as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["knowledge_units"] >= 24  # 基线 24，知识流水线持续入库


def test_run_then_resume_then_package():
    with _client() as client:
        r = client.post("/api/v1/runs", json=NEED)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "awaiting_human"
        run_id = body["run_id"]
        assert body["interrupt"]["type"] == "confirm_targets"

        r2 = client.post(f"/api/v1/runs/{run_id}/resume", json={"decision": {"action": "approve"}})
        assert r2.status_code == 200
        assert r2.json()["status"] == "completed"

        r3 = client.get(f"/api/v1/runs/{run_id}")
        assert r3.status_code == 200 and r3.json()["status"] == "completed"

        r4 = client.get(f"/api/v1/runs/{run_id}/package.md")
        assert r4.status_code == 200
        assert "## 十、引用清单" in r4.text


def test_kb_units_filters():
    with _client() as client:
        r = client.get("/api/v1/kb/units", params={"domain": "STRAT"})
        assert r.status_code == 200
        units = r.json()
        assert units and all("STRAT" in u["domains"] for u in units)

        r2 = client.get("/api/v1/kb/units", params={"q": "GROW 教练"})
        assert r2.status_code == 200
        assert any(u["id"] == "KU-LEAD-004" for u in r2.json())


def test_archetypes_endpoint():
    with _client() as client:
        r = client.get("/api/v1/archetypes")
    assert r.status_code == 200
    keys = {a["key"] for a in r.json()}
    assert {"new_manager_transition", "strategy_alignment_workshop", "generic_leadership"} <= keys


def test_runs_list_and_static_workbench():
    """运行记录列表 + Web 工作台静态挂载（API 路由不被遮蔽）。"""
    with _client() as client:
        # 静态挂载：根路径返回工作台 HTML
        r = client.get("/")
        assert r.status_code == 200
        assert "管理学知识库 Agent" in r.text
        assert "需求诊断单" in r.text

        # API 路由优先于静态挂载
        r2 = client.get("/healthz")
        assert r2.status_code == 200 and r2.json()["status"] == "ok"

        # runs 列表：字段完整（Web 工作台消费）
        r3 = client.post("/api/v1/runs", json=NEED)
        run_id = r3.json()["run_id"]
        client.post(f"/api/v1/runs/{run_id}/resume", json={"decision": {"action": "approve"}})
        r4 = client.get("/api/v1/runs")
        assert r4.status_code == 200
        row = next((x for x in r4.json() if x["run_id"] == run_id), None)
        assert row is not None
        assert row["status"] == "completed"
        assert row["audience"] == "新任经理"
        assert row["has_package"] is True

        # detail 计数（工作台统计卡片消费）
        r5 = client.get(f"/api/v1/runs/{run_id}")
        d = r5.json()["detail"]
        assert d["modules_count"] > 0
        assert d["activities_count"] > 0
        assert d["assessments_count"] > 0
        assert d["citations_count"] > 0
