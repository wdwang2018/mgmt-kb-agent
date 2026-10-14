"""课程包 Markdown 渲染器（讲义/讲师手册的 P0 交付格式）。"""

from __future__ import annotations

from ..domain.models import CoursePackage, domain_name

_BUCKET_CN = {"formal": "正式", "social": "人际", "practice": "实践"}


def render_markdown(pkg: CoursePackage) -> str:
    n = pkg.need
    L: list[str] = []
    L.append(f"# 《{pkg.need.theme_hint or '管理培训'}》课程包")
    L.append("")
    L.append(f"- 运行编号：{pkg.run_id}")
    L.append(f"- 培训对象：{n.audience}（{n.headcount} 人，当前约 L{n.audience_level}）")
    L.append(f"- 总时长：{n.duration_minutes // 60} 小时 ｜ 行业：{n.industry}")
    L.append(f"- 业务痛点：{n.business_pain}")
    L.append(f"- 期望行为改变：{n.desired_behavior}")
    L.append(f"- 人工审核：{'已签发（含门禁未过项）' if pkg.human_reviewed else '自动通过'}")
    L.append("")

    L.append("## 一、课程定位")
    L.append("")
    L.append(f"面向{n.audience}的实战课程，围绕「{n.business_pain}」设计，"
             f"以「{n.desired_behavior}」为行为改变验收标准。")
    L.append("")

    L.append("## 二、能力差距映射")
    L.append("")
    L.append("| 胜任力域 | 目标层级 | 差距描述 | 判定依据 |")
    L.append("|---|---|---|---|")
    for t in pkg.gap.targets:
        L.append(f"| {domain_name(t.domain)} | L{t.target_level} | {t.gap} | {t.rationale} |")
    L.append("")
    L.append(f"识别出的管理困境：{'、'.join(pkg.gap.dilemmas)}")
    L.append("")

    L.append("## 三、学习目标（布鲁姆层级，可测量）")
    L.append("")
    L.append("| 编号 | 学习目标 | 布鲁姆 | 所属域 |")
    L.append("|---|---|---|---|")
    for o in pkg.objectives:
        L.append(f"| {o.id} | {o.text} | L{o.bloom} | {domain_name(o.domain)} |")
    L.append("")

    L.append("## 四、模块大纲（15 分钟粒度）")
    L.append("")
    L.append("| 编号 | 模块 | 时长 | 配比（正式/人际/实践） | 覆盖目标 |")
    L.append("|---|---|---|---|---|")
    for m in pkg.modules:
        mix = m.bucket_mix
        L.append(f"| {m.id} | {m.title} | {m.minutes} 分钟 | "
                 f"{_BUCKET_CN['formal']}{mix.get('formal', 0)}／{_BUCKET_CN['social']}{mix.get('social', 0)}／"
                 f"{_BUCKET_CN['practice']}{mix.get('practice', 0)} | {'、'.join(m.objective_ids)} |")
    L.append("")

    L.append("## 五、模块内容详表（理论要点 + 引用）")
    for m in pkg.modules:
        L.append("")
        L.append(f"### {m.id} {m.title}")
        L.append("")
        for p in pkg.content.get(m.id, []):
            L.append(f"- **理论要点**：{p.text}")
            L.append(f"  - 引用：{p.citation}（证据等级 {p.evidence_level}）")
            for mis in p.misconceptions:
                L.append(f"  - 常见误区：{mis}")
    L.append("")

    L.append("## 六、教学活动（70-20-10）")
    L.append("")
    L.append("| 编号 | 所属模块 | 活动 | 类型 | 时长 | 工具 |")
    L.append("|---|---|---|---|---|---|")
    for a in pkg.activities:
        L.append(f"| {a.id} | {a.module_id} | {a.title} | {_BUCKET_CN.get(a.bucket.value, a.bucket.value)} | "
                 f"{a.minutes} 分钟 | {'、'.join(a.tools) or '—'} |")
    L.append("")

    L.append("## 七、评估方案")
    L.append("")
    for a in pkg.assessments:
        if a.kind == "SJT":
            L.append(f"**{a.id}（情境判断）** {a.stem}")
            for i, opt in enumerate(a.options):
                mark = " ✅" if i == a.answer_index else ""
                L.append(f"- {chr(65 + i)}. {opt}{mark}")
            if a.rationale:
                L.append(f"- 解析：{a.rationale}")
            L.append("")
        elif a.kind == "case_discussion":
            L.append(f"**{a.id}（案例讨论）** {a.stem}")
            if a.rationale:
                L.append(f"- {a.rationale}")
            L.append("")
        else:
            L.append(f"**{a.id}（{a.stem}）**")
            if a.rationale:
                L.append(f"- {a.rationale}")
            L.append("")

    L.append("## 八、讲师手册要点")
    L.append("")
    for m in pkg.modules:
        acts = [a for a in pkg.activities if a.module_id == m.id]
        tips = []
        if any(a.bucket.value == "practice" for a in acts):
            tips.append("演练环节严格控时，观察点优先于纠正")
        if any(a.bucket.value == "social" for a in acts):
            tips.append("讨论环节用世界咖啡/换桌机制保证观点迭代")
        if not tips:
            tips.append("讲授环节每 15 分钟插入一个提问检查点")
        L.append(f"- {m.id} {m.title}：{'；'.join(tips)}")
    L.append("")

    L.append("## 九、质量门禁报告")
    L.append("")
    L.append("| 门禁 | 结果 | 问题 |")
    L.append("|---|---|---|")
    for g in pkg.gates:
        L.append(f"| {g.gate} | {'✅ 通过' if g.passed else '❌ 未过'} | {'；'.join(g.issues) or '—'} |")
    L.append("")

    L.append("## 十、引用清单")
    L.append("")
    for i, c in enumerate(pkg.citations, 1):
        L.append(f"{i}. {c}")
    L.append("")
    return "\n".join(L)
