"""黄金评测集运行器：P0 验收指标的度量脚本。

指标（对应 PRD P0 验收标准）：
1. 流水线完成率（12 个真实需求）
2. 四道门禁通过率（G1-G4）
3. 检索 recall@5（12 条标签查询）
4. 溯源覆盖率（理论要点有引用定位的比例）
5. 建设性对齐率（学习目标同时挂接活动与评估的比例）
6. 能力域/困境识别率（对照 golden 期望）

用法：
    python eval/run_eval.py            # 输出报告到 eval/report-<日期>.md
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.domain.models import TrainingNeed  # noqa: E402
from app.services.context import build_context  # noqa: E402

GOLDEN = REPO / "eval" / "golden" / "course-design-v1.jsonl"


def load_golden() -> tuple[list[dict], list[dict]]:
    needs, queries = [], []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        (needs if item["kind"] == "need" else queries).append(item)
    return needs, queries


def main() -> int:
    needs, queries = load_golden()
    ctx = build_context(output_dir=REPO / "eval" / "runs")

    # ---------------- 检索 recall@5 ----------------
    retrieval_rows = []
    for q in queries:
        print(f"[eval] 检索 {q['id']} ...", flush=True)
        units = ctx.retriever.search(q["query"], domains=q.get("domains"),
                                     dilemmas=q.get("dilemmas"), top_k=5)
        ids = [u.id for u in units]
        hit = q["expected_unit"] in ids
        retrieval_rows.append({"id": q["id"], "query": q["query"],
                               "expected": q["expected_unit"], "hit": hit,
                               "top": ids[:3]})
    recall_at_5 = sum(r["hit"] for r in retrieval_rows) / len(retrieval_rows)

    # ---------------- 流水线 ----------------
    pipeline_rows = []
    for item in needs:
        print(f"[eval] 流水线 {item['id']} ...", flush=True)
        need = TrainingNeed(**item["need"])
        result = ctx.pipeline.run_auto(need)
        pkg = result.package
        row = {"id": item["id"], "status": result.status}
        if pkg is None:
            row["error"] = "未完成"
            pipeline_rows.append(row)
            continue
        gates = {g.gate: g.passed for g in pkg.gates}
        domains = {t.domain for t in pkg.gap.targets}
        dilemmas = set(pkg.gap.dilemmas)
        # 溯源覆盖率
        points = [p for pts in pkg.content.values() for p in pts]
        traced = sum(1 for p in points if p.citation and p.unit_id)
        # 建设性对齐率
        aligned = sum(1 for o in pkg.objectives
                      if o.linked_activity_ids and o.linked_assessment_ids)
        row.update({
            "gates_all_passed": all(gates.values()),
            "gates": gates,
            "modules": len(pkg.modules),
            "activities": len(pkg.activities),
            "assessments": len(pkg.assessments),
            "citations": len(pkg.citations),
            "trace_coverage": traced / len(points) if points else 0.0,
            "alignment_rate": aligned / len(pkg.objectives) if pkg.objectives else 0.0,
            "domain_hit": all(d in domains for d in item["expect"]["domains"]),
            "dilemma_hit": all(d in dilemmas for d in item["expect"]["dilemmas"]),
        })
        pipeline_rows.append(row)

    done = [r for r in pipeline_rows if r.get("status") == "completed"]
    n = len(pipeline_rows)
    report = {
        "completion_rate": len(done) / n,
        "gates_pass_rate": sum(r.get("gates_all_passed", False) for r in done) / max(len(done), 1),
        "recall_at_5": recall_at_5,
        "trace_coverage": sum(r.get("trace_coverage", 0) for r in done) / max(len(done), 1),
        "alignment_rate": sum(r.get("alignment_rate", 0) for r in done) / max(len(done), 1),
        "domain_hit_rate": sum(r.get("domain_hit", False) for r in done) / max(len(done), 1),
        "dilemma_hit_rate": sum(r.get("dilemma_hit", False) for r in done) / max(len(done), 1),
    }

    # ---------------- 报告 ----------------
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    lines = [
        f"# 管理学知识库 Agent · 黄金评测报告（{today}）",
        "",
        f"- 评测集：{len(needs)} 个培训需求 + {len(queries)} 条检索查询",
        f"- LLM 适配器：{ctx.pipeline.deps.llm.name}",
        f"- 知识单元：{len(ctx.retriever.units)} ｜ 案例：{len(ctx.pipeline.deps.cases)}",
        "",
        "## 汇总指标",
        "",
        "| 指标 | 数值 | 目标 | 结论 |",
        "|---|---|---|---|",
        f"| 流水线完成率 | {report['completion_rate']:.0%} | 100% | {'✅' if report['completion_rate'] == 1 else '❌'} |",
        f"| 门禁全过率 | {report['gates_pass_rate']:.0%} | 100% | {'✅' if report['gates_pass_rate'] == 1 else '❌'} |",
        f"| 检索 recall@5 | {report['recall_at_5']:.0%} | ≥80% | {'✅' if report['recall_at_5'] >= 0.8 else '❌'} |",
        f"| 溯源覆盖率 | {report['trace_coverage']:.0%} | 100% | {'✅' if report['trace_coverage'] == 1 else '❌'} |",
        f"| 建设性对齐率 | {report['alignment_rate']:.0%} | 100% | {'✅' if report['alignment_rate'] == 1 else '❌'} |",
        f"| 能力域识别率 | {report['domain_hit_rate']:.0%} | ≥80% | {'✅' if report['domain_hit_rate'] >= 0.8 else '❌'} |",
        f"| 困境识别率 | {report['dilemma_hit_rate']:.0%} | ≥80% | {'✅' if report['dilemma_hit_rate'] >= 0.8 else '❌'} |",
        "",
        "## 需求流水线明细",
        "",
        "| 编号 | 状态 | 门禁全过 | 模块 | 活动 | 评估 | 引用 | 域识别 | 困境识别 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in pipeline_rows:
        lines.append(
            f"| {r['id']} | {r.get('status')} | {'✅' if r.get('gates_all_passed') else '❌'} | "
            f"{r.get('modules', '-')} | {r.get('activities', '-')} | {r.get('assessments', '-')} | "
            f"{r.get('citations', '-')} | {'✅' if r.get('domain_hit') else '❌'} | "
            f"{'✅' if r.get('dilemma_hit') else '❌'} |")
    lines += ["", "## 检索明细（recall@5）", "",
              "| 编号 | 查询 | 期望单元 | 命中 | Top3 |", "|---|---|---|---|---|"]
    for r in retrieval_rows:
        lines.append(f"| {r['id']} | {r['query']} | {r['expected']} | "
                     f"{'✅' if r['hit'] else '❌'} | {'、'.join(r['top'])} |")
    lines += ["", "## 说明", "",
              "- 本报告由 `eval/run_eval.py` 自动生成；Mock LLM 模式下度量的是流水线、门禁、",
              "  检索与结构化装配的正确性；内容深度指标需切换 DeepSeek 后复测。",
              "- 每次更换模型、修改索引或调整门禁规则后，必须重跑本评测，不达标不上线。"]

    out = REPO / "eval" / f"report-{today}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[eval] 报告已写入 {out}")
    return 0 if report["completion_rate"] == 1 and report["gates_pass_rate"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
