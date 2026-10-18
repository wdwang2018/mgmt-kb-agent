"""领域模型：知识单元 / 能力目标 / 课程包等核心 Pydantic 模型。

设计原则：
- 本模块只依赖 pydantic 与 constants，不碰任何外部服务（端口-适配器架构的领域内核）。
- 所有知识单元必须可溯源（source.locator 必填），无据不答的工程约束从这里开始。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .constants import COMPETENCY_DOMAINS, COURSE_TYPES, DILEMMA_INDEX, EVIDENCE_LEVELS


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class EvidenceLevel(str, Enum):
    A = "A"
    B = "B"
    C = "C"


class KUType(str, Enum):
    CONCEPT = "概念"
    THEORY = "理论"
    MODEL = "模型"
    TOOL = "工具"
    FINDING = "研究结论"
    METHOD = "方法论"


class RelationType(str, Enum):
    PROPONENT = "提出者"      # 理论-提出者
    SCHOOL = "学派"           # 理论-学派归属
    CONTROVERSY = "争议"      # 理论-争议观点
    PREREQUISITE = "前置"     # 知识单元-前置单元
    APPLICATION = "应用"      # 理论-应用场景/案例
    COMPARE = "对比"          # 对立/互补观点


class Bucket(str, Enum):
    FORMAL = "formal"
    SOCIAL = "social"
    PRACTICE = "practice"


# ---------------------------------------------------------------------------
# 知识单元（L2 层核心资产）
# ---------------------------------------------------------------------------
class SourceRef(BaseModel):
    """引用定位：每个结论可回溯到 书名/作者/章节定位。"""

    book: str = Field(description="来源书名")
    author: str = Field(description="作者")
    year: Optional[int] = None
    locator: str = Field(description="定位：章节/页码/小节，如「第3章」")
    isbn: Optional[str] = None
    copyright_status: Literal["公版", "授权", "要点化", "原创"] = Field(
        default="要点化",
        description="版权状态：只存摘要+要点+引用定位，不整文复制",
    )


class Misconception(BaseModel):
    """学员常见误区 vs 理论纠正（G4 认知冲突门禁的数据源）。"""

    claim: str = Field(description="学员常见误区（错误的流行说法）")
    correction: str = Field(description="理论/实证纠正")


class Relation(BaseModel):
    """图谱关系抽取产物（P0 落 JSON，P1 迁 Neo4j）。"""

    type: RelationType
    target: str = Field(description="关系对象：学者名/学派名/单元ID/案例ID")
    note: Optional[str] = None


class KnowledgeUnit(BaseModel):
    """L2 知识单元：概念/理论/模型/工具/研究结论，按管理困境挂接。"""

    id: str = Field(pattern=r"^KU-(LEAD|STRAT|CLASSIC|BEHAV|CHINA|QUALITY|ORGLEARN|CONT|DEC|LDS)-\d{3}$")
    title: str
    type: KUType
    domains: list[str] = Field(description="胜任力域代码，如 TEAM/STRAT")
    dilemmas: list[str] = Field(description="困境索引代码，如 D01")
    evidence_level: EvidenceLevel
    contested: bool = Field(default=False, description="是否存在学派争议，需并列呈现")
    lineage: list[str] = Field(
        default_factory=list,
        description="学派/年代谱系代码（双轴索引之一），如 L-CLASSIC/L-BEHAV/L-CHINA，"
        "见 domain.constants.THEORY_LINEAGE",
    )
    source: SourceRef
    key_points: list[str] = Field(min_length=2, description="要点化摘要（非原文复制）")
    misconceptions: list[Misconception] = Field(default_factory=list)
    applicable_context: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list, description="前置单元ID")
    related: list[str] = Field(default_factory=list, description="关联单元ID")
    relations: list[Relation] = Field(default_factory=list)
    tools: list[dict[str, str]] = Field(default_factory=list, description="[{name, usage}]")


# ---------------------------------------------------------------------------
# 知识流水线（P1）：L1 语料层 → L2 草稿 → 质检 → 人工审核 → 入库
# ---------------------------------------------------------------------------
class CorpusChunk(BaseModel):
    """L1 语料层：带定位的语料片段，采集产物，溯源链条的起点。"""

    id: str = Field(pattern=r"^CC-\d{4}$", description="语料编号 CC-0001")
    book: str = Field(description="来源书名")
    author: str = Field(description="作者")
    year: Optional[int] = None
    locator: str = Field(description="定位：章节/页码/小节")
    copyright_status: Literal["公版", "授权", "要点化", "原创"] = "要点化"
    text: str = Field(description="语料正文（授权摘要/要点化转述，不整文复制）")
    unit_id: Optional[str] = Field(default=None, description="回链：该语料被抽成的知识单元 KU 编号")
    ingested_by: str = Field(default="manual", description="采集方式：manual/auto/backfill")
    status: Literal["raw", "extracted"] = Field(default="raw", description="raw=待加工，extracted=已抽成草稿")


class QualityCheck(BaseModel):
    """单项质检结果（K1-K5 之一）。"""

    code: str = Field(description="检查项代码，如 K1")
    name: str = Field(description="检查项名称")
    passed: bool
    detail: str = ""


class DraftQualityReport(BaseModel):
    """草稿质检报告：五项规则门禁，任一不过即打回。"""

    passed: bool
    checks: list[QualityCheck] = Field(default_factory=list)

    def failed_codes(self) -> list[str]:
        return [c.code for c in self.checks if not c.passed]


class UnitDraft(BaseModel):
    """加工草稿：从语料抽取、待人工审核的知识单元（未入库不参与检索）。"""

    id: str = Field(pattern=r"^DR-\d{4}$", description="草稿编号 DR-0001")
    unit: KnowledgeUnit = Field(description="草稿本体（id 为占位，入库时分配正式编号）")
    corpus_ids: list[str] = Field(default_factory=list, description="来源语料 CC 编号")
    quality: Optional[DraftQualityReport] = None
    status: Literal["pending_review", "approved", "rejected", "committed"] = "pending_review"
    review_note: Optional[str] = None
    reviewer: str = ""
    created_at: str = ""
    updated_at: str = ""


class DraftReview(BaseModel):
    """审核决定：批准（commit 时分配正式 ID 入库）/ 打回（携修改意见）。"""

    draft_id: str
    action: Literal["approve", "reject"]
    note: str = ""
    reviewer: str = "reviewer"


# ---------------------------------------------------------------------------
# 培训需求与能力差距（生成器输入契约）
# ---------------------------------------------------------------------------
class TrainingNeed(BaseModel):
    """需求诊断单：课程设计生成器的唯一输入。"""

    audience: str = Field(description="培训对象，如「新任经理」")
    audience_level: int = Field(default=2, ge=1, le=5, description="对象当前能力层级")
    headcount: int = Field(ge=1)
    duration_minutes: int = Field(ge=60, description="总时长（分钟）")
    business_pain: str = Field(description="业务痛点，如「跨部门推不动」")
    desired_behavior: str = Field(description="期望的行为改变")
    industry: str = Field(default="通用")
    theme_hint: Optional[str] = Field(default=None, description="主题线索，如「新任管理者转型」")


class CompetencyTarget(BaseModel):
    """能力差距分析产物：目标域 + 目标层级 + 依据。"""

    domain: str
    target_level: int = Field(ge=1, le=5)
    gap: str = Field(description="差距描述（现状 vs 目标）")
    rationale: str = Field(description="判定依据（来自需求痛点映射）")


class GapAnalysis(BaseModel):
    targets: list[CompetencyTarget]
    summary: str
    dilemmas: list[str] = Field(default_factory=list, description="识别出的困境标签")


class LearningObjective(BaseModel):
    """可测量的学习目标（布鲁姆层级 + 对齐活动与评估）。"""

    id: str
    text: str
    bloom: int = Field(ge=1, le=5)
    domain: str
    target_level: int
    linked_activity_ids: list[str] = Field(default_factory=list)
    linked_assessment_ids: list[str] = Field(default_factory=list)


class ModulePlan(BaseModel):
    """模块大纲（时间轴到 15 分钟粒度）。"""

    id: str
    title: str
    minutes: int
    objective_ids: list[str]
    bucket_mix: dict[str, int] = Field(default_factory=dict, description="formal/social/practice 分钟数")
    focus_domains: list[str] = Field(default_factory=list)
    focus_dilemmas: list[str] = Field(default_factory=list)


class TheoryPoint(BaseModel):
    """模块理论要点：内容 + 引用（G2 溯源门禁的数据源）。"""

    text: str
    unit_id: str
    citation: str = Field(description="人类可读引用串，如「Hill《成为管理者》第2章」")
    evidence_level: str
    misconceptions: list[str] = Field(default_factory=list)


class Activity(BaseModel):
    id: str
    module_id: str
    title: str
    bucket: Bucket
    minutes: int
    objective_ids: list[str]
    description: str
    tools: list[str] = Field(default_factory=list)


class AssessmentItem(BaseModel):
    """情境判断题（SJT）或案例讨论题 + 评分量规。"""

    id: str
    kind: Literal["SJT", "case_discussion", "rubric"]
    stem: str
    options: list[str] = Field(default_factory=list)
    answer_index: Optional[int] = None
    rationale: Optional[str] = None
    module_id: str
    objective_ids: list[str]
    unit_ids: list[str] = Field(default_factory=list)


class GateReport(BaseModel):
    gate: str
    passed: bool
    issues: list[str] = Field(default_factory=list)


class CoursePackage(BaseModel):
    """课程包：生成器的最终输出契约。"""

    run_id: str
    need: TrainingNeed
    gap: GapAnalysis
    objectives: list[LearningObjective]
    modules: list[ModulePlan]
    content: dict[str, list[TheoryPoint]] = Field(default_factory=dict)
    activities: list[Activity] = Field(default_factory=list)
    assessments: list[AssessmentItem] = Field(default_factory=list)
    gates: list[GateReport] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list, description="引用清单（书名+定位）")
    human_reviewed: bool = False


class ArticleSource(str, Enum):
    """文章摄入来源。"""

    PASTE = "paste"
    URL = "url"
    FILE = "file"


class ArticleAnalysis(BaseModel):
    """文章解析产物：读完一篇文章，判断它能否成课、该做成哪种课。

    语义边界（与语料层同源）：summary 是「要点化转述」，不是原文摘录。
    对外展示时不得把 summary 当作原文引用——那会假装我们持有原文。
    """

    source: ArticleSource
    title: str = Field(description="识别出的文章标题；无法确定时给推测标题")
    summary: list[str] = Field(description="3-5 条要点（要点化转述，非原文摘录）")
    core_concepts: list[str] = Field(default_factory=list, description="核心概念，用于回查库内知识单元")

    domains: list[str] = Field(default_factory=list, description="命中能力域代码")
    dilemmas: list[str] = Field(default_factory=list, description="命中困境代码 D01-D24")
    lineages: list[str] = Field(default_factory=list, description="命中学派谱系代码 L-*")
    evidence_level: str = Field(default="C", description="A/B/C 证据等级")

    feasible: bool = Field(description="能否做成课件")
    feasibility_reason: str = Field(description="判定理由（可行则说依据，不可行则说缺什么）")
    course_type: str = Field(default="HALF_DAY", description="推荐课件类型代码")
    related_units: list[str] = Field(default_factory=list, description="关联的知识单元 id")

    audience_reco: str = Field(default="", description="推荐受众画像（对外公开课文风）")
    suggested_theme: str = Field(default="", description="建议填 theme_hint")
    suggested_pain: str = Field(default="", description="建议填 business_pain")
    suggested_behavior: str = Field(default="", description="建议填 desired_behavior")
    # 三个数值建议一律 Optional + None：给出建议才注入，否则保留用户手填值。
    # 若用默认值（180/30/2），fill_training_need 的 if 判断恒为真，会把用户
    # 手填的时长与班型强行覆盖掉——宁可 None 也不猜。
    suggested_duration: Optional[int] = Field(default=None, description="建议时长（分钟）")
    suggested_headcount: Optional[int] = Field(default=None, description="建议班型人数（公开课典型 20-40）")
    suggested_level: Optional[int] = Field(default=None, ge=1, le=5, description="建议受众层级 L1-L5")

    warnings: list[str] = Field(default_factory=list, description="提示：抓取失败/过短/证据不足等")
    raw_chars: int = Field(default=0, description="摄入正文字符数（用于判断是否过短/超长被截断）")

    def fill_training_need(self, need: "TrainingNeed") -> "TrainingNeed":
        """把解析结论注入需求诊断单，供课程设计流水线直接消费。"""
        if self.suggested_theme:
            need.theme_hint = self.suggested_theme
        if self.suggested_pain:
            need.business_pain = self.suggested_pain
        if self.suggested_behavior:
            need.desired_behavior = self.suggested_behavior
        if self.audience_reco:
            need.audience = self.audience_reco
        if self.suggested_duration:
            need.duration_minutes = self.suggested_duration
        if self.suggested_headcount:
            need.headcount = self.suggested_headcount
        if self.suggested_level:
            need.audience_level = self.suggested_level
        return need


def domain_name(code: str) -> str:
    return COMPETENCY_DOMAINS[code]["name"]


def dilemma_name(code: str) -> str:
    return DILEMMA_INDEX[code]


def anchor_text(code: str, level: int) -> str:
    return COMPETENCY_DOMAINS[code]["anchors"][level]


def evidence_desc(level: str) -> str:
    return EVIDENCE_LEVELS[level]
