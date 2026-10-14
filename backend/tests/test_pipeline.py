"""端到端测试：流水线跑通、HITL 挂起/恢复、人工修订路径。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.models import TrainingNeed

NEED = TrainingNeed(
    audience="新任经理（晋升 6 个月内）", audience_level=2, headcount=24,
    duration_minutes=360,
    business_pain="新经理仍埋头做事，下属得不到成长，跨部门协作全部卡在经理这里",
    desired_behavior="完成从个人贡献者到团队领导者的转身：会授权、会给反馈、坚持 1on1",
    industry="互联网", theme_hint="新任管理者转型",
)

NEED_STRATEGY = TrainingNeed(
    audience="中层管理者", audience_level=3, headcount=30, duration_minutes=720,
    business_pain="年度战略会上都点头，散会后各部门各干各的，资源投向与战略脱钩",
    desired_behavior="建立战略共识机制，把战略解码为各部门可执行的仗与承诺",
    industry="制造", theme_hint="战略共识与解码",
)


def _ctx(tmp_path):
    from app.services.context import build_context
    return build_context(output_dir=tmp_path / "runs")


def test_pipeline_auto_completes_with_all_gates_passed(tmp_path):
    ctx = _ctx(tmp_path)
    result = ctx.pipeline.run_auto(NEED)
    assert result.status == "completed", result.interrupt
    pkg = result.package
    assert len(pkg.modules) >= 5
    assert len(pkg.activities) >= len(pkg.modules)
    assert len(pkg.assessments) >= 3
    assert pkg.citations, "引用清单为空"
    assert all(g.passed for g in pkg.gates), [g.issues for g in pkg.gates if not g.passed]
    # 溯源：所有理论要点单元均存在于知识库
    unit_ids = {u.id for u in ctx.store.load_units()}
    for points in pkg.content.values():
        for p in points:
            assert p.unit_id in unit_ids
            assert p.citation
    # 产物落盘
    md = tmp_path / "runs" / result.run_id / "course-package.md"
    js = tmp_path / "runs" / result.run_id / "package.json"
    assert md.exists() and js.exists()
    assert "## 十、引用清单" in md.read_text(encoding="utf-8")


def test_pipeline_hitl_pause_and_resume(tmp_path):
    ctx = _ctx(tmp_path)
    result = ctx.pipeline.run(NEED)
    assert result.status == "awaiting_human"
    assert result.interrupt["type"] == "confirm_targets"
    assert result.interrupt["payload"]["targets"], "人工确认负载应含能力目标"

    result2 = ctx.pipeline.resume(result.run_id, {"action": "approve"})
    assert result2.status == "completed", result2.interrupt
    assert result2.package is not None


def test_pipeline_hitl_revise_targets(tmp_path):
    ctx = _ctx(tmp_path)
    result = ctx.pipeline.run(NEED)
    revised = [{"domain": "TEAM", "target_level": 4,
                "gap": "人工修订：从埋头做事转向带团队拿结果",
                "rationale": "讲师人工确认时修订"}]
    result2 = ctx.pipeline.resume(result.run_id, {"action": "revise", "targets": revised})
    assert result2.status == "completed", result2.interrupt
    domains = {t.domain for t in result2.package.gap.targets}
    assert "TEAM" in domains
    assert any(t.target_level == 4 for t in result2.package.gap.targets)


def test_pipeline_strategy_theme(tmp_path):
    ctx = _ctx(tmp_path)
    result = ctx.pipeline.run_auto(NEED_STRATEGY)
    assert result.status == "completed"
    pkg = result.package
    domains = {t.domain for t in pkg.gap.targets}
    assert "STRAT" in domains
    assert any("D07" in d or "D08" in d for d in pkg.gap.dilemmas)
    assert all(g.passed for g in pkg.gates), [g.issues for g in pkg.gates if not g.passed]


def test_get_run_from_disk(tmp_path):
    ctx = _ctx(tmp_path)
    result = ctx.pipeline.run_auto(NEED)
    fetched = ctx.pipeline.get(result.run_id)
    assert fetched is not None and fetched.status == "completed"
    assert fetched.package is not None
