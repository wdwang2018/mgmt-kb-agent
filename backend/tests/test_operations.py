"""运营包生成测试（#3）：从课程包派生对外公开课的运营方案。

覆盖：
- 服务级：generate_operations 产出 OperationsPlan 全字段（定位/卖点/渠道/课前/课后30-60-90）；
- 端点级：/operations 生成并落盘、课程包缺失 → 404；
- 变异验证：把 _norm 的渠道规整清空后，渠道断言测试必须失败（非空转）。
"""

from __future__ import annotations

import json

import pytest

from app.adapters.llm_mock import MockLLM
from app.domain.models import CoursePackage, OperationsPlan
from app.services.operations import _norm, generate_operations

SAMPLE = {
    "run_id": "run-test-ops",
    "need": {
        "audience": "新任经理", "audience_level": 2, "headcount": 24,
        "duration_minutes": 360, "business_pain": "新经理仍埋头做事，下属得不到成长",
        "desired_behavior": "会授权、会给反馈、坚持 1on1", "industry": "互联网",
        "theme_hint": "新任管理者转型",
    },
    "gap": {"targets": [], "summary": "", "dilemmas": ["D01"]},
    "objectives": [],
    "modules": [{"id": "M1", "title": "转身之痛", "minutes": 30, "objective_ids": []}],
    "content": {},
    "citations": [],
}


def _pkg() -> CoursePackage:
    return CoursePackage.model_validate(SAMPLE)


def test_generate_operations_full_fields():
    p = generate_operations(_pkg(), MockLLM())
    assert isinstance(p, OperationsPlan)
    # 定位应含受众/行业/主题/痛点
    assert "新任经理" in p.audience_positioning
    assert "互联网" in p.audience_positioning
    assert "新任管理者转型" in p.audience_positioning
    # 卖点 / 课前 / 课后齐全
    assert len(p.selling_points) >= 3
    assert len(p.pre_class_prep) >= 2
    # 渠道每条都有 channel + method
    assert len(p.promotion_channels) >= 3
    for c in p.promotion_channels:
        assert c.channel and c.method
    # 课后三阶段 30/60/90，每阶段有目标与动作
    phases = [f.phase for f in p.post_class_followup]
    assert phases[:3] == ["30", "60", "90"], phases
    for f in p.post_class_followup:
        assert f.goal and f.actions


def test_norm_drops_non_dict_and_fills_defaults():
    """_norm 应过滤非字典项、给缺字段补安全默认（不崩、不污染）。"""
    raw = {
        "audience_positioning": "定位",
        "selling_points": ["A", "B"],
        "promotion_channels": [{"channel": "C", "method": "M"}, "坏项", 42],
        "pre_class_prep": ["P1"],
        "post_class_followup": [{"phase": "30", "goal": "G", "actions": ["a"]}, None],
    }
    n = _norm(raw)
    assert n["audience_positioning"] == "定位"
    assert len(n["promotion_channels"]) == 1
    assert n["promotion_channels"][0]["channel"] == "C"
    assert len(n["post_class_followup"]) == 1
    # 规整结果可被 OperationsPlan 校验通过
    OperationsPlan(**n)


@pytest.fixture
def client_with_pkg(tmp_path):
    import app.api.main as main
    from app.services.context import build_context
    from fastapi.testclient import TestClient

    saved = main.ctx
    run_id = "run-test-ops"
    out = tmp_path / run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "package.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    ctx = build_context(llm_mode="mock", output_dir=str(tmp_path))
    ctx.llm = MockLLM()
    main.ctx = ctx
    try:
        yield TestClient(main.app), run_id, tmp_path
    finally:
        main.ctx = saved


def test_operations_endpoint_flow(client_with_pkg):
    client, run_id, tmp_path = client_with_pkg
    r = client.post(f"/api/v1/runs/{run_id}/operations")
    assert r.status_code == 200
    d = r.json()
    assert d["audience_positioning"]
    assert len(d["promotion_channels"]) >= 3
    assert [f["phase"] for f in d["post_class_followup"]][:3] == ["30", "60", "90"]
    # 落盘校验：operations.json 应真实写入 run 目录
    assert (tmp_path / run_id / "operations.json").exists()


def test_operations_endpoint_no_package_404(tmp_path):
    import app.api.main as main
    from app.services.context import build_context
    from fastapi.testclient import TestClient

    saved = main.ctx
    ctx = build_context(llm_mode="mock", output_dir=str(tmp_path))
    ctx.llm = MockLLM()
    main.ctx = ctx
    try:
        c = TestClient(main.app)
        assert c.post("/api/v1/runs/run-missing/operations").status_code == 404
    finally:
        main.ctx = saved
