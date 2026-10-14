"""领域模型：知识单元 / 能力目标 / 课程包等核心 Pydantic 模型。

设计原则：
- 本模块只依赖 pydantic 与 constants，不碰任何外部服务（端口-适配器架构的领域内核）。
- 所有知识单元必须可溯源（source.locator 必填），无据不答的工程约束从这里开始。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .constants import COMPETENCY_DOMAINS, DILEMMA_INDEX, EVIDENCE_LEVELS


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

    id: str = Field(pattern=r"^KU-(LEAD|STRAT)-\d{3}$")
    title: str
    type: KUType
    domains: list[str] = Field(description="胜任力域代码，如 TEAM/STRAT")
    dilemmas: list[str] = Field(description="困境索引代码，如 D01")
    evidence_level: EvidenceLevel
    contested: bool = Field(default=False, description="是否存在学派争议，需并列呈现")
    source: SourceRef
    key_points: list[str] = Field(min_length=2, description="要点化摘要（非原文复制）")
    misconceptions: list[Misconception] = Field(default_factory=list)
    applicable_context: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list, description="前置单元ID")
    related: list[str] = Field(default_factory=list, description="关联单元ID")
    relations: list[Relation] = Field(default_factory=list)
    tools: list[dict[str, str]] = Field(default_factory=list, description="[{name, usage}]")


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


def domain_name(code: str) -> str:
    return COMPETENCY_DOMAINS[code]["name"]


def dilemma_name(code: str) -> str:
    return DILEMMA_INDEX[code]


def anchor_text(code: str, level: int) -> str:
    return COMPETENCY_DOMAINS[code]["anchors"][level]


def evidence_desc(level: str) -> str:
    return EVIDENCE_LEVELS[level]
