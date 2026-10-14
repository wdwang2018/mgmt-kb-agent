"""九节点流水线实现（LangGraph 节点函数）。

每个节点签名统一为 fn(state, deps) -> dict（部分状态更新），
同一套节点函数可被 LangGraph StateGraph 消费，也可被自研 runner 消费。
节点内不直接 import 适配器实现，只通过 NodeDeps 使用端口。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..domain.constants import BLOOM_VERBS
from ..domain.models import (
    Activity,
    AssessmentItem,
    Bucket,
    CompetencyTarget,
    CoursePackage,
    GapAnalysis,
    LearningObjective,
    ModulePlan,
    TheoryPoint,
    TrainingNeed,
    anchor_text,
    domain_name,
)
from ..ports import LLMClient, KnowledgeStore, Retriever
from .archetypes import ARCHETYPES, BUCKET_NAMES, normalize_mix, rebalance_course_buckets, scale_to_duration, select_archetype
from .gates import run_gates
from .hitl import human_gate
from .state import CourseDesignState

# ---------------------------------------------------------------------------
# 系统提示词（生产模式由 DeepSeek 适配器消费；Mock 模式仅作占位）
# ---------------------------------------------------------------------------
SYS_PARSE = "你是管理培训需求分析专家。从培训方的原始描述中提取结构化需求诊断单，只输出 JSON。"
SYS_GAP = "你是管理能力建模专家。基于业务痛点判定核心能力差距，输出一句话诊断摘要。"
SYS_OBJ = ("你是教学设计专家。按布鲁姆认知层级把能力目标改写为可测量、可观察的学习目标，只输出 JSON。"
           "格式：{\"objectives\": [{\"target_index\": 整数, \"text\": \"字符串\", \"bloom\": 1-5}]}，"
           "text 必须是纯字符串，不要使用对象或嵌套结构。")
SYS_ACT = "你是培训活动设计师。为模块生成结构化的教学活动说明，突出讲师观察点。"
SYS_ASM = ("你是管理测评专家。围绕学员常见误区生成情境判断题（SJT），选项包含一个符合理论证据的最佳答案，只输出 JSON。"
           "格式：{\"stem\": \"字符串\", \"options\": [\"字符串\", ...], \"answer_index\": 0起始整数, \"rationale\": \"字符串\"}；"
           "options 必须是 4 个纯字符串（不要 {id, text} 对象），answer_index 指正确选项在 options 中的下标。")

MAX_RETRIES = 4


# ---------------------------------------------------------------------------
# LLM 输出规范化：在线模型（GLM/DeepSeek）返回 shape 多变，统一在此收敛，
# 任何不合规字段都回退到调用方传入的 fallback，绝不让脏数据击穿 pydantic 模型。
# ---------------------------------------------------------------------------
def _as_str(v: Any, default: str = "") -> str:
    return v.strip() if isinstance(v, str) and v.strip() else default


def _as_int(v: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _norm_options(raw: Any, fallback: list[str]) -> list[str]:
    """选项规范化：接受字符串数组或 {id,text} 对象数组；不足 2 项则整体回退。"""
    if not isinstance(raw, list) or len(raw) < 2:
        return list(fallback)
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            t = item.get("text") or item.get("content") or item.get("option") or item.get("label")
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
    return out if len(out) >= 2 else list(fallback)


def _norm_answer_index(raw: Any, options: list[str]) -> int:
    """答案下标规范化：支持 0 基整数、'2' 字符串数字、'B' 字母 id；越界回退 0。"""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw if 0 <= raw < len(options) else 0
    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            i = int(s)
            return i if 0 <= i < len(options) else 0
        if len(s) == 1 and s.upper() in "ABCDEFGH":
            i = ord(s.upper()) - ord("A")
            return i if 0 <= i < len(options) else 0
    return 0

# 需求关键词 → 能力域 / 困境标签 增强映射：让差距分析响应需求本身，而非只吃原型标签
KEYWORD_DOMAINS = [
    ("变革", "CHANGE"), ("战略", "STRAT"), ("共识", "STRAT"), ("解码", "STRAT"),
    ("教练", "COACH"), ("反馈", "COACH"), ("绩效", "COACH"), ("辅导", "COACH"),
    ("沟通", "COMM"), ("跨部门", "COMM"), ("协同", "COMM"), ("影响", "COMM"),
    ("授权", "TEAM"), ("冲突", "TEAM"), ("团队", "TEAM"),
    ("执行", "EXEC"), ("落地", "EXEC"), ("时间", "EXEC"), ("目标", "STRAT"),
    ("创新", "CHANGE"),
]
KEYWORD_DILEMMAS = [
    ("变革", "D05"), ("跨部门", "D04"), ("授权", "D02"), ("反馈", "D10"),
    ("绩效", "D09"), ("1on1", "D10"), ("冲突", "D03"), ("共识", "D07"),
    ("解码", "D08"), ("目标", "D16"), ("时间", "D17"), ("人才", "D12"),
    ("梯队", "D24"), ("创新", "D21"), ("危机", "D20"), ("会议", "D14"),
]


@dataclass
class NodeDeps:
    llm: LLMClient
    retriever: Retriever
    store: KnowledgeStore
    cases: list[dict] = field(default_factory=list)
    output_dir: Path = field(default_factory=lambda: Path("runs"))
    hitl_mode: str = "interrupt"  # interrupt（LangGraph）| pause（自研 runner）
    gate_rule: Optional[dict] = None
    max_retries: int = MAX_RETRIES


def _units_index(deps: NodeDeps) -> set[str]:
    units = getattr(deps.retriever, "units", None)
    if isinstance(units, dict):
        return set(units.keys())
    return {u.id for u in deps.store.load_units()}


# ---------------------------------------------------------------------------
# 1. 需求解析
# ---------------------------------------------------------------------------
def parse_need(state: CourseDesignState, deps: NodeDeps) -> dict:
    need = state["need"]
    raw = f"{need.business_pain}。{need.desired_behavior}"
    data = need.model_dump()
    res = deps.llm.complete_json("parse_need", SYS_PARSE,
                                 json.dumps({"raw": raw}, ensure_ascii=False), fallback=data)
    merged = dict(data)
    for k in ("audience", "business_pain", "desired_behavior", "industry", "theme_hint"):
        v = _as_str(res.get(k), "")
        if v:
            merged[k] = v
    merged["audience_level"] = _as_int(res.get("audience_level"), need.audience_level, 1, 5)
    merged["headcount"] = _as_int(res.get("headcount"), need.headcount, 1, 10000)
    merged["duration_minutes"] = _as_int(res.get("duration_minutes"), need.duration_minutes, 15, 10080)
    return {"need": TrainingNeed(**merged), "status": "need_parsed"}


# ---------------------------------------------------------------------------
# 2. 能力差距分析（后挂人工确认：能力目标选错，全盘皆错）
# ---------------------------------------------------------------------------
def gap_analysis(state: CourseDesignState, deps: NodeDeps) -> dict:
    need = state["need"]
    archetype = select_archetype(need.theme_hint, need.business_pain, need.desired_behavior)

    domain_counter = Counter(d for m in archetype.modules for d in m["focus_domains"])
    targets = []
    for domain, _ in domain_counter.most_common(4):
        level = min(5, max(2, need.audience_level + 1))
        targets.append(CompetencyTarget(
            domain=domain,
            target_level=level,
            gap=f"现状约 L{need.audience_level} → 目标 L{level}：{anchor_text(domain, level)}",
            rationale=f"业务痛点映射：{need.business_pain[:60]}",
        ))
    # 关键词增强：原型标签之外的域/困境，按需求文本补齐（最多各 2 个）
    # 注意：必须在构造 GapAnalysis 之前完成域增强——pydantic 会拷贝列表，
    # 构造后往旧列表追加不会反映到 gap.targets。
    text = f"{need.business_pain}{need.desired_behavior}{need.theme_hint or ''}"
    have_domains = {t.domain for t in targets}
    for kw, d in KEYWORD_DOMAINS:
        if d not in have_domains and kw in text:
            level = min(5, max(2, need.audience_level + 1))
            targets.append(CompetencyTarget(
                domain=d, target_level=level,
                gap=f"现状约 L{need.audience_level} → 目标 L{level}：{anchor_text(d, level)}",
                rationale=f"需求关键词命中：{kw}"))
            have_domains.add(d)
            if len(targets) >= 6:
                break

    gap = GapAnalysis(targets=targets, summary="", dilemmas=list(archetype.dilemma_tags))

    have_dilemmas = set(gap.dilemmas)
    for kw, d in KEYWORD_DILEMMAS:
        if d not in have_dilemmas and kw in text:
            gap.dilemmas.append(d)
            have_dilemmas.add(d)
            if len(gap.dilemmas) >= 6:
                break
    gap.summary = deps.llm.complete("gap_summary", SYS_GAP, json.dumps({
        "pain": need.business_pain,
        "desired": need.desired_behavior,
        "targets": [{"domain_name": domain_name(t.domain), "target_level": t.target_level} for t in targets],
    }, ensure_ascii=False))

    payload = {"summary": gap.summary, "targets": [t.model_dump() for t in gap.targets]}
    decision, updates = human_gate(state, deps, "confirm_targets", payload)
    if decision is None:
        return {**updates, "gap": gap, "archetype_key": archetype.key}

    if decision.get("action") == "revise" and decision.get("targets"):
        try:
            gap.targets = [CompetencyTarget(**t) for t in decision["targets"]]
        except Exception:
            pass
    return {"gap": gap, "archetype_key": archetype.key, "status": "gap_confirmed"}


# ---------------------------------------------------------------------------
# 3. 学习目标生成（布鲁姆层级，可测量）
# ---------------------------------------------------------------------------
def generate_objectives(state: CourseDesignState, deps: NodeDeps) -> dict:
    gap = state["gap"]
    targets_payload = [{
        "domain": t.domain,
        "domain_name": domain_name(t.domain),
        "target_level": t.target_level,
        "gap_short": t.gap[:40],
        "anchor": anchor_text(t.domain, t.target_level),
    } for t in gap.targets]
    fallback = {"objectives": [
        {"target_index": i,
         "text": f"能够运用{domain_name(t.domain)}相关方法，在真实管理情境中达成「{anchor_text(t.domain, t.target_level)}」",
         "bloom": min(5, t.target_level)}
        for i, t in enumerate(gap.targets)]}
    res = deps.llm.complete_json("objectives", SYS_OBJ,
                                 json.dumps({"targets": targets_payload}, ensure_ascii=False),
                                 fallback=fallback)
    raw_list = res.get("objectives") or fallback["objectives"]
    objectives: list[LearningObjective] = []
    for i, o in enumerate(raw_list):
        if not isinstance(o, dict):
            o = {}
        ti = _as_int(o.get("target_index"), i, 0, len(gap.targets) - 1)
        t = gap.targets[ti]
        bloom = _as_int(o.get("bloom"), t.target_level, 1, 5)
        text = _as_str(o.get("text"), fallback["objectives"][ti]["text"])
        objectives.append(LearningObjective(
            id=f"OBJ-{i + 1:02d}", text=text, bloom=bloom,
            domain=t.domain, target_level=t.target_level))
    return {"objectives": objectives, "status": "objectives_generated"}


# ---------------------------------------------------------------------------
# 4. 模块规划（时间轴到 15 分钟粒度；重试时按 feedback 修正配比）
# ---------------------------------------------------------------------------
def plan_modules(state: CourseDesignState, deps: NodeDeps) -> dict:
    need = state["need"]
    archetype = ARCHETYPES[state["archetype_key"]]
    scaled = scale_to_duration(archetype, need.duration_minutes)
    for m in scaled.modules:  # 先规范化（清掉 <10 分钟的小桶），再做课程级配比修复
        m["bucket_mix"] = normalize_mix(m["bucket_mix"])
    rebalance_course_buckets(scaled.modules)

    objectives = state["objectives"]
    modules: list[ModulePlan] = []
    for i, m in enumerate(scaled.modules):
        mod_objs = [o for o in objectives if o.domain in m["focus_domains"]] or list(objectives)
        modules.append(ModulePlan(
            id=f"M{i + 1}", title=m["title"], minutes=m["minutes"],
            objective_ids=[o.id for o in mod_objs],
            bucket_mix=dict(m["bucket_mix"]),
            focus_domains=m["focus_domains"], focus_dilemmas=m["focus_dilemmas"]))

    # 目标全覆盖保证：原型未覆盖的域（如关键词增强出的 CHANGE）也必须挂到某模块，
    # 否则该学习目标既无活动也无评估，G1 必挂。挂到时长最大的模块承载。
    covered = {oid for m in modules for oid in m.objective_ids}
    for o in objectives:
        if o.id not in covered and modules:
            max(modules, key=lambda m: m.minutes).objective_ids.append(o.id)

    return {"modules": modules, "status": "modules_planned"}


# ---------------------------------------------------------------------------
# 5. 内容匹配检索（能力域 × 困境 双路召回，GraphRAG 脉络 P1 接入）
# ---------------------------------------------------------------------------
def match_content(state: CourseDesignState, deps: NodeDeps) -> dict:
    content: dict[str, list[TheoryPoint]] = {}
    citations: list[str] = []
    used: set[str] = set()
    for m in state["modules"]:
        units = deps.retriever.dual_recall(m.focus_domains, m.focus_dilemmas, top_k=3)
        # 跨模块去重：优先用未在前序模块出现的单元；不够时允许复用（保证每模块有内容）
        fresh = [u for u in units if u.id not in used]
        picked = (fresh or units)[:2]
        points = []
        for u in picked:
            citation = f"{u.source.author}《{u.source.book}》{u.source.locator}"
            points.append(TheoryPoint(
                text=u.key_points[0], unit_id=u.id, citation=citation,
                evidence_level=u.evidence_level.value,
                misconceptions=[x.claim for x in u.misconceptions]))
            used.add(u.id)
            if citation not in citations:
                citations.append(citation)
        content[m.id] = points
    return {"content": content, "citations": citations, "status": "content_matched"}


# ---------------------------------------------------------------------------
# 6. 教学活动编排（70-20-10 分桶）
# ---------------------------------------------------------------------------
def design_activities(state: CourseDesignState, deps: NodeDeps) -> dict:
    objectives = {o.id: o for o in state["objectives"]}
    activities: list[Activity] = []
    idx = 1
    for m in state["modules"]:
        mix = m.bucket_mix
        plan = []
        if mix.get("practice", 0) >= 10:
            plan.append(("practice", mix["practice"], f"{m.title}·情境演练",
                         ["角色扮演", "行动学习"]))
        if mix.get("social", 0) >= 10:
            plan.append(("social", mix["social"], f"{m.title}·小组研讨",
                         ["小组讨论", "同伴反馈"]))
        if mix.get("formal", 0) >= 10:
            plan.append(("formal", mix["formal"], f"{m.title}·理论讲授与案例解析",
                         ["案例讲解"]))
        for bucket, minutes, title, tools in plan:
            # 超过 90 分钟的活动拆分为多段，保证课堂节奏
            if minutes <= 90:
                chunks = [minutes]
            elif minutes <= 180:
                chunks = [90, minutes - 90]
            else:
                chunks = [60, minutes - 120, 60]
            for ci, chunk in enumerate([c for c in chunks if c > 0]):
                suffix = f"（第{ci + 1}段）" if len(chunks) > 1 else ""
                aid = f"ACT-{idx:02d}"
                idx += 1
                desc = deps.llm.complete("activity_desc", SYS_ACT, json.dumps({
                    "title": title + suffix, "bucket": bucket, "bucket_name": BUCKET_NAMES[bucket],
                    "minutes": chunk, "objective_ids": m.objective_ids, "tools": tools,
                }, ensure_ascii=False))
                activities.append(Activity(
                    id=aid, module_id=m.id, title=title + suffix, bucket=Bucket(bucket),
                    minutes=chunk, objective_ids=list(m.objective_ids),
                    description=desc or f"{chunk} 分钟{BUCKET_NAMES[bucket]}：{title}{suffix}",
                    tools=tools))
    for a in activities:  # 双向链接（G1 数据源）
        for oid in a.objective_ids:
            if oid in objectives and a.id not in objectives[oid].linked_activity_ids:
                objectives[oid].linked_activity_ids.append(a.id)
    return {"activities": activities, "objectives": list(objectives.values()),
            "status": "activities_designed"}


# ---------------------------------------------------------------------------
# 7. 评估设计（SJT 情境判断 + 案例讨论 + 评分量规）
# ---------------------------------------------------------------------------
def design_assessments(state: CourseDesignState, deps: NodeDeps) -> dict:
    objectives = {o.id: o for o in state["objectives"]}
    assessments: list[AssessmentItem] = []
    idx = 1

    # 7.1 SJT：每模块取一个「常见误区 vs 理论证据」
    for m in state["modules"]:
        points = state["content"].get(m.id, [])
        mis_point = next((p for p in points if p.misconceptions), None)
        if not mis_point:
            continue
        unit = deps.retriever.by_ids([mis_point.unit_id])
        claim = mis_point.misconceptions[0]
        correction = next((x.correction for x in unit[0].misconceptions if x.claim == claim),
                          "按理论证据执行") if unit else "按理论证据执行"
        fallback = {
            "stem": f"关于「{unit[0].title if unit else '管理理论'}」，以下哪种做法最符合理论证据？",
            "options": [correction, "顺其自然等下属领悟", "用更高压管控弥补", "只在考核时集中说明"],
            "answer_index": 0, "rationale": f"理论证据支持：{correction}",
        }
        res = deps.llm.complete_json("assessment_stem", SYS_ASM, json.dumps({
            "unit_id": mis_point.unit_id,
            "unit_title": unit[0].title if unit else "管理理论",
            "misconception_claim": claim, "correction": correction,
        }, ensure_ascii=False), fallback=fallback)
        aid = f"ASM-{idx:02d}"
        idx += 1
        options = _norm_options(res.get("options"), fallback["options"])
        assessments.append(AssessmentItem(
            id=aid, kind="SJT",
            stem=_as_str(res.get("stem"), fallback["stem"]),
            options=options,
            answer_index=_norm_answer_index(res.get("answer_index", res.get("answer")), options),
            rationale=_as_str(res.get("rationale"), fallback["rationale"]),
            module_id=m.id, objective_ids=list(m.objective_ids),
            unit_ids=[mis_point.unit_id]))

    # 7.2 案例讨论题：与识别困境交集，最多 2 个；挂到困境交集最多的模块
    gap_dilemmas = set(state["gap"].dilemmas)
    for case in deps.cases:
        if len([a for a in assessments if a.kind == "case_discussion"]) >= 2:
            break
        case_dilemmas = set(case.get("dilemmas", []))
        if not (case_dilemmas & gap_dilemmas):
            continue
        best_module = max(state["modules"],
                          key=lambda m: len(case_dilemmas & set(m.focus_dilemmas)),
                          default=None)
        aid = f"ASM-{idx:02d}"
        idx += 1
        assessments.append(AssessmentItem(
            id=aid, kind="case_discussion",
            stem=f"案例讨论：{case['title']}——{case['context']}",
            rationale="讨论要点：" + "；".join(case.get("discussion_questions", [])),
            module_id=best_module.id if best_module else "M1",
            objective_ids=list(best_module.objective_ids) if best_module else [],
            unit_ids=[r for r in case.get("theory_refs", [])]))

    # 7.3 评分量规：挂到最后模块（演练/共创）
    if state["modules"]:
        last = state["modules"][-1]
        aid = f"ASM-{idx:02d}"
        idx += 1
        assessments.append(AssessmentItem(
            id=aid, kind="rubric",
            stem=f"评分量规：{last.title}",
            rationale=("四维量规：1) 概念运用准确（0-3 分）2) 情境分析深度（0-3 分）"
                       "3) 行为可执行性（0-3 分）4) 反思与迁移（0-3 分）；≥9 分视为达成行为改变"),
            module_id=last.id, objective_ids=list(last.objective_ids), unit_ids=[]))

    for a in assessments:  # 双向链接（G1 数据源）
        for oid in a.objective_ids:
            if oid in objectives and a.id not in objectives[oid].linked_assessment_ids:
                objectives[oid].linked_assessment_ids.append(a.id)
    return {"assessments": assessments, "objectives": list(objectives.values()),
            "status": "assessments_designed"}


# ---------------------------------------------------------------------------
# 8. 质量门禁（G1-G4；不过则重试，四次不过转人工）
# ---------------------------------------------------------------------------
def quality_gate(state: CourseDesignState, deps: NodeDeps) -> dict:
    reports = run_gates(state, _units_index(deps), deps.gate_rule)
    passed = all(r.passed for r in reports)
    if passed:
        return {"gate_reports": reports, "gates_passed": True, "status": "gates_passed"}

    issues = [i for r in reports for i in r.issues]
    retries = int(state.get("retries", 0))
    if retries < deps.max_retries:
        return {"gate_reports": reports, "retries": retries + 1,
                "feedback": issues, "status": "retrying"}

    decision, updates = human_gate(state, deps, "human_review",
                                   {"issues": issues, "reports": [r.model_dump() for r in reports]})
    if decision is None:
        return {**updates, "gate_reports": reports}
    if decision.get("action") == "approve":
        return {"gate_reports": reports, "gates_passed": True, "status": "gates_passed"}
    # revise：回到模块规划再来一轮；累计两轮人工修订仍不过则强制签发（防死循环）
    if int(state.get("human_revisions", 0)) >= 1:
        return {"gate_reports": reports, "gates_passed": True, "status": "gates_passed_forced"}
    return {"gate_reports": reports, "retries": 0,
            "human_revisions": int(state.get("human_revisions", 0)) + 1,
            "feedback": issues, "status": "retrying"}


# ---------------------------------------------------------------------------
# 9. 打包输出（课程包 + Markdown 讲义 + 落盘）
# ---------------------------------------------------------------------------
def package_output(state: CourseDesignState, deps: NodeDeps) -> dict:
    pkg = CoursePackage(
        run_id=state["run_id"],
        need=state["need"], gap=state["gap"], objectives=state["objectives"],
        modules=state["modules"], content=state["content"], activities=state["activities"],
        assessments=state["assessments"], gates=state["gate_reports"],
        citations=state["citations"],
        human_reviewed=any(not r.passed for r in state["gate_reports"]),
    )
    from ..services.render import render_markdown  # 延迟导入避免环
    md = render_markdown(pkg)
    out_dir = Path(deps.output_dir) / state["run_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "course-package.md").write_text(md, encoding="utf-8")
    (out_dir / "package.json").write_text(
        pkg.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    deps.store.log_review(state["run_id"], "package_generated",
                          {"gates_passed": pkg.human_reviewed is False,
                           "human_reviewed": pkg.human_reviewed})
    return {"package": pkg, "package_markdown": md, "status": "completed"}


NODES = {
    "parse_need": parse_need,
    "gap_analysis": gap_analysis,
    "generate_objectives": generate_objectives,
    "plan_modules": plan_modules,
    "match_content": match_content,
    "design_activities": design_activities,
    "design_assessments": design_assessments,
    "quality_gate": quality_gate,
    "package_output": package_output,
}
