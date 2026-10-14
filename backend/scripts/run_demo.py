"""CLI Demo：用一个真实培训需求端到端跑通课程设计生成器。

用法：
    python scripts/run_demo.py                     # 默认：新任管理者转型（1 天）
    python scripts/run_demo.py --interactive       # 人工挂起点需手动确认（演示 HITL）
    python scripts/run_demo.py --llm deepseek      # 切换在线模型（需 DEEPSEEK_API_KEY）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.models import TrainingNeed
from app.services.context import build_context

DEMO_NEED = TrainingNeed(
    audience="新任经理（晋升 6 个月内）",
    audience_level=2,
    headcount=24,
    duration_minutes=360,
    business_pain="新经理仍埋头做事，下属得不到成长，跨部门协作全部卡在经理这里",
    desired_behavior="完成从个人贡献者到团队领导者的转身：会授权、会给反馈、坚持 1on1",
    industry="互联网",
    theme_hint="新任管理者转型",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interactive", action="store_true", help="人工挂起等待手动确认")
    parser.add_argument("--llm", default=None, help="mock | deepseek")
    args = parser.parse_args()

    ctx = build_context(llm_mode=args.llm)
    print(f"[demo] LLM={ctx.pipeline.deps.llm.name}  知识单元={len(ctx.retriever.units)}  案例={len(ctx.pipeline.deps.cases)}")

    if args.interactive:
        result = ctx.pipeline.run(DEMO_NEED)
        while result.status == "awaiting_human":
            print("\n===== 等待人工审核 =====")
            print(json.dumps(result.interrupt, ensure_ascii=False, indent=2)[:2000])
            print("输入决定 JSON，如 {\"action\":\"approve\"} 或 {\"action\":\"revise\",\"feedback\":\"...\"}")
            line = input("decision> ").strip()
            decision = json.loads(line) if line else {"action": "approve"}
            result = ctx.pipeline.resume(result.run_id, decision)
    else:
        result = ctx.pipeline.run_auto(DEMO_NEED)

    print(f"\n[demo] run_id={result.run_id}  status={result.status}")
    if result.status != "completed":
        print("[demo] 流水线未完成（可能仍在等待人工）")
        return 1

    pkg = result.package
    print(f"[demo] 模块数={len(pkg.modules)}  活动数={len(pkg.activities)}  "
          f"评估题数={len(pkg.assessments)}  引用数={len(pkg.citations)}")
    print(f"[demo] 门禁：{[(g.gate, g.passed) for g in pkg.gates]}")
    md_file = ctx.output_dir / result.run_id / "course-package.md"
    print(f"[demo] 课程包：{md_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
