"""测验题库生成与自动评分测试（#2）。

覆盖：
- 服务级：generate_quiz（题库源自课程包真实模块/理论要点）、grade_quiz（规则化评分）；
- 端点级：/quiz 生成并落盘、/quiz/submit 评分、未生成即提交 → 404；
- 变异验证：把 grade_quiz 改成「永远算对」后，部分得分测试必须失败（非空转）。
"""

from __future__ import annotations

import json

import pytest

from app.adapters.llm_mock import MockLLM
from app.domain.models import CoursePackage, QuizItem
from app.services.quiz import _norm_item, generate_quiz, grade_quiz

# 紧凑但完整的样本包（模块 + content 理论要点，保证题库「考的是课上讲的」）
SAMPLE = {
    "run_id": "run-test-quiz",
    "need": {
        "audience": "新任经理", "audience_level": 2, "headcount": 24,
        "duration_minutes": 360, "business_pain": "新经理仍埋头做事，下属得不到成长",
        "desired_behavior": "会授权、会给反馈、坚持 1on1", "industry": "互联网",
        "theme_hint": "新任管理者转型",
    },
    "gap": {"targets": [], "summary": "", "dilemmas": ["D01"]},
    "objectives": [],
    "modules": [
        {"id": "M1", "title": "开场：转身之痛", "minutes": 30, "objective_ids": [],
         "focus_domains": ["TEAM"]},
        {"id": "M2", "title": "授权：五级阶梯", "minutes": 60, "objective_ids": [],
         "focus_domains": ["TEAM", "COACH"]},
    ],
    "content": {
        "M1": [{"text": "管理是计划/组织/指挥/协调/控制五大职能", "unit_id": "KU-CLASSIC-002",
                "citation": "Fayol《工业管理与一般管理》", "evidence_level": "C"}],
        "M2": [{"text": "X-Y 理论：人性假设决定管理方式", "unit_id": "KU-BEHAV-002",
                "citation": "McGregor《企业的人性面》", "evidence_level": "C"}],
    },
    "citations": [],
}


def _pkg() -> CoursePackage:
    return CoursePackage.model_validate(SAMPLE)


def test_generate_quiz_tied_to_modules():
    """题库每模块一题，正确项取自该模块首条理论要点（真实内容，非编造）。"""
    items = generate_quiz(_pkg(), MockLLM())
    assert len(items) == 2, items
    for it, mod in zip(items, SAMPLE["modules"]):
        assert isinstance(it, QuizItem)
        assert mod["title"] in it.stem
        assert len(it.options) == 4
        assert it.answer_index is not None and 0 <= it.answer_index < 4
        # 正确项必须是该模块的理论要点文本
        assert it.options[it.answer_index] == SAMPLE["content"][mod["id"]][0]["text"]
        assert it.explanation, "解析不能为空"
        assert it.module_id == mod["id"]


def test_generate_quiz_caps_at_six():
    """超过 6 个模块时题库封顶 6 题，避免问卷过长。"""
    d = json.loads(json.dumps(SAMPLE))
    d["modules"] = [{"id": f"M{i}", "title": f"模块{i}", "minutes": 15,
                     "objective_ids": []} for i in range(1, 10)]
    items = generate_quiz(CoursePackage.model_validate(d), MockLLM())
    assert len(items) == 6


def test_grade_perfect():
    items = generate_quiz(_pkg(), MockLLM())
    answers = [it.answer_index for it in items]
    res = grade_quiz(items, answers)
    assert res["score"] == res["total"] == 2
    assert res["percent"] == 100
    assert all(r["correct"] for r in res["detail"])


def test_grade_partial_and_missing():
    """答错一题 + 漏答一题：只有全对才计分，漏答（-1）不计对。"""
    items = generate_quiz(_pkg(), MockLLM())
    correct = [it.answer_index for it in items]
    wrong_first = (correct[0] + 1) % 4          # 第 1 题选错
    res = grade_quiz(items, [wrong_first, -1])   # 第 2 题漏答
    assert res["score"] == 0
    assert res["detail"][0]["correct"] is False
    assert res["detail"][1]["correct"] is False


def test_open_item_never_scored():
    """answer_index 为 None 的开放题不参与自动评分（永不算对）。"""
    items = [QuizItem(id="Q1", stem="x", options=["a", "b"], answer_index=None)]
    res = grade_quiz(items, [0])
    assert res["score"] == 0 and res["total"] == 1


def test_norm_item_bad_answer_index_dropped():
    """answer_index 越界/缺失时降级为 None（宁可不评分，不错评）。"""
    item = _norm_item({"stem": "x", "options": ["a", "b"], "answer_index": 9}, 0)
    assert item["answer_index"] is None


# ---------------------------------------------------------------------------
# 端点集成：直接写 package.json，避免跑 12 分钟流水线；强制 Mock 离线可评。
# ---------------------------------------------------------------------------
@pytest.fixture
def client_with_pkg(tmp_path):
    import app.api.main as main
    from app.services.context import build_context
    from fastapi.testclient import TestClient

    saved = main.ctx
    run_id = "run-test-quiz"
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


def test_quiz_endpoint_generate_and_grade(client_with_pkg):
    client, run_id, tmp_path = client_with_pkg
    g = client.post(f"/api/v1/runs/{run_id}/quiz")
    assert g.status_code == 200
    items = g.json()["items"]
    assert len(items) == 2 and all(i["answer_index"] is not None for i in items)
    # 落盘校验：quiz.json 应真实写入 run 目录
    assert (tmp_path / run_id / "quiz.json").exists()

    answers = [i["answer_index"] for i in items]
    s = client.post(f"/api/v1/runs/{run_id}/quiz/submit", json={"answers": answers})
    assert s.status_code == 200
    body = s.json()
    assert body["score"] == body["total"] == 2 and body["percent"] == 100


def test_quiz_submit_before_generate_404(tmp_path):
    """未生成测验就提交 → 404，提示先生成。"""
    import app.api.main as main
    from app.services.context import build_context
    from fastapi.testclient import TestClient

    saved = main.ctx
    run_id = "run-no-quiz"
    (tmp_path / run_id).mkdir(parents=True, exist_ok=True)
    (tmp_path / run_id / "package.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    ctx = build_context(llm_mode="mock", output_dir=str(tmp_path))
    ctx.llm = MockLLM()
    main.ctx = ctx
    try:
        c = TestClient(main.app)
        r = c.post(f"/api/v1/runs/{run_id}/quiz/submit", json={"answers": [0]})
        assert r.status_code == 404
    finally:
        main.ctx = saved


def test_quiz_endpoint_no_package_404(tmp_path):
    """课程包未生成 → /quiz 返回 404。"""
    import app.api.main as main
    from app.services.context import build_context
    from fastapi.testclient import TestClient

    saved = main.ctx
    ctx = build_context(llm_mode="mock", output_dir=str(tmp_path))
    ctx.llm = MockLLM()
    main.ctx = ctx
    try:
        c = TestClient(main.app)
        assert c.post("/api/v1/runs/run-missing/quiz").status_code == 404
    finally:
        main.ctx = saved
