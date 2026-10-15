"""知识流水线五节点（P1）：采集 → 加工 → 质检 → 人工审核 → 入库。

与课程设计九节点平行的一条 LangGraph 流水线，消费同一套端口：
- 加工节点在线模式由 GLM/DeepSeek 承担，Mock 模式走确定性规则抽取（离线可测）；
- 人工审核复用 human_gate 双模式（interrupt / pause），批准才分配正式编号入库；
- 质检 K1-K5 是知识质量的上游门禁：溯源/代码合法/要点化/误区结构/唯一性。

节点签名统一为 fn(state, deps) -> dict（部分状态更新）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from ..domain.constants import COMPETENCY_DOMAINS, DILEMMA_INDEX
from ..domain.models import (
    CorpusChunk,
    DraftQualityReport,
    EvidenceLevel,
    KnowledgeUnit,
    KUType,
    Misconception,
    QualityCheck,
    Relation,
    SourceRef,
    UnitDraft,
)
from ..ports import LLMClient, KnowledgeStore
from .hitl import human_gate

_TZ = timezone(timedelta(hours=8))

try:
    from typing import NotRequired, TypedDict  # Python 3.11+
except Exception:  # pragma: no cover
    from typing_extensions import NotRequired, TypedDict  # type: ignore


class KBState(TypedDict, total=False):
    run_id: str
    corpus: Optional[CorpusChunk]                 # 本次加工的语料（单块）
    draft: Optional[UnitDraft]                    # 加工产物（待质检/审核）
    quality: Optional[DraftQualityReport]
    status: str                                   # created/extracted/validated/awaiting_human/committed/rejected
    decision: Optional[dict[str, Any]]            # 人工审核决定
    committed_unit_id: Optional[str]
    error: NotRequired[str]


def kb_initial_state(run_id: str, corpus: CorpusChunk) -> dict[str, Any]:
    return {"run_id": run_id, "corpus": corpus, "draft": None, "quality": None,
            "status": "created", "decision": None, "committed_unit_id": None}


@dataclass
class KBNodeDeps:
    llm: LLMClient
    store: KnowledgeStore
    corpus_store: Any                              # JsonCorpusStore（语料层存取）
    hitl_mode: str = "interrupt"                   # interrupt | pause
    auto_approve: bool = False                     # Demo/评测用：跳过 interrupt 直接批准


# ---------------------------------------------------------------------------
# LLM 输出规范化（在线模型 shape 抖动统一收敛，脏数据回退规则抽取）
# ---------------------------------------------------------------------------
def _as_str(v: Any, default: str = "") -> str:
    return v.strip() if isinstance(v, str) and v.strip() else default


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


# ---------------------------------------------------------------------------
# Mock 规则抽取：确定性、离线可测。关键词表命中域/困境，句子切分出要点。
# ---------------------------------------------------------------------------
_DOMAIN_KEYWORDS = {
    "STRAT": ["战略", "策略", "愿景", "定位", "竞争", "商业模式", "第二曲线"],
    "TEAM": ["团队", "下属", "管理者", "经理", "领导", "带人", "班子"],
    "COMM": ["沟通", "表达", "倾听", "汇报", "谈判", "汇报线", "向上"],
    "COACH": ["教练", "反馈", "辅导", "培养", "带教", "发展下属"],
    "EXEC": ["执行", "落地", "目标", "优先级", "推进", "闭环", "复盘"],
    "CHANGE": ["变革", "转型", "抵触", "阻力", "变革管理"],
}

_DILEMMA_KEYWORDS = {
    "D01": ["新经理", "角色转变", "角色转型", "晋升", "埋头", "个人贡献"],
    "D02": ["授权", "放手", "放了乱", "不敢放"],
    "D06": ["方向", "战略迷失", "往哪打", "迷茫"],
    "D07": ["共识", "各唱各调", "对齐", "拉齐"],
    "D08": ["执行断裂", "停在ppt", "落地难", "执行不力"],
    "D16": ["目标设定", "目标虚设", "上下一致", "okr", "kpi"],
    "D17": ["时间管理", "优先级", "事务拖着"],
}

_SENT_SPLIT = re.compile(r"[。；;！!？?]\s*|\n+")


def _match_codes(text: str, table: dict[str, list[str]], limit: int) -> list[str]:
    low = text.lower()
    hits = [code for code, kws in table.items() if any(k.lower() in low for k in kws)]
    return hits[:limit] if hits else []


def _split_points(text: str, max_points: int = 3) -> list[str]:
    parts = [p.strip(" ，,、") for p in _SENT_SPLIT.split(text)]
    return [p for p in parts if len(p) >= 8][:max_points]


def _points_or_fallback(text: str) -> list[str]:
    """要点兜底：句子级切分不足 2 条时放宽长度门槛二级切分，保证 pydantic min_length=2 不崩；
    仍不足则硬切文本（质检 K3 会因单条过短打回，这是设计内的质量反馈）。"""
    points = _split_points(text, 3)
    if len(points) >= 2:
        return points
    loose = [p.strip(" ，,、") for p in _SENT_SPLIT.split(text) if p.strip(" ，,、")][:3]
    if len(loose) >= 2:
        return loose
    head, tail = text[:40].strip(), text[40:80].strip()
    return [head or text[:40], tail or "（语料过短，待补充）"]


def _short_title(text: str, limit: int = 30) -> str:
    """标题截断：优先在 30 字内的最后一个标点处收尾，避免半句 cutoff。"""
    head = text[:limit]
    cut = max(head.rfind("，"), head.rfind("、"), head.rfind("。"))
    return head[:cut] if cut >= 10 else head


def mock_extract(chunk: CorpusChunk) -> UnitDraft:
    """Mock 抽取：语料 → 合规草稿（type 默认理论，证据等级默认 B）。"""
    text = chunk.text
    type_ = KUType.THEORY
    if any(k in text for k in ["模型", "框架"]):
        type_ = KUType.MODEL
    elif any(k in text for k in ["步骤", "方法", "流程"]):
        type_ = KUType.METHOD
    domains = _match_codes(text, _DOMAIN_KEYWORDS, 2) or ["TEAM"]
    dilemmas = _match_codes(text, _DILEMMA_KEYWORDS, 2) or ["D01"]
    title_text = _short_title(text)
    unit = KnowledgeUnit(
        id="KU-LEAD-000",  # 占位，commit 时分配正式编号
        title=title_text,
        type=type_,
        domains=domains,
        dilemmas=dilemmas,
        evidence_level=EvidenceLevel.B,
        contested=False,
        source=SourceRef(book=chunk.book, author=chunk.author, year=chunk.year,
                         locator=chunk.locator, copyright_status=chunk.copyright_status),
        key_points=_points_or_fallback(text),
        misconceptions=[],
        applicable_context=[],
        relations=[Relation(type="提出者", target=chunk.author)],
    )
    return UnitDraft(id="DR-0000", unit=unit, corpus_ids=[chunk.id],
                     status="pending_review", created_at=_now(), updated_at=_now())


SYS_EXTRACT = (
    "你是知识萃取专家。把给定的管理类语料抽取为一个知识单元，只输出 JSON。"
    "格式：{\"title\": \"字符串(≤30字)\", \"type\": \"概念|理论|模型|工具|研究结论|方法论\", "
    "\"domains\": [\"STRAT|TEAM|CHANGE|COMM|COACH|EXEC\" 选1-2个], "
    "\"dilemmas\": [\"D01-D24\" 选1-2个], \"evidence_level\": \"A|B|C\", "
    "\"key_points\": [\"要点化摘要，2-4条，每条为纯字符串\"], "
    "\"misconceptions\": [{\"claim\": \"学员常见误区\", \"correction\": \"理论纠正\"}], "
    "\"applicable_context\": [\"适用情境\"]}。"
    "所有字段必须是纯字符串/字符串数组，不要使用对象嵌套（misconceptions 除外）。"
)


def online_extract(llm: LLMClient, chunk: CorpusChunk) -> UnitDraft:
    """在线抽取：GLM/DeepSeek 产出结构化 JSON，任何脏 shape 都回退 Mock 规则抽取。"""
    fallback = {"title": _short_title(chunk.text), "type": "理论", "domains": [], "dilemmas": [],
                "evidence_level": "B", "key_points": [], "misconceptions": [],
                "applicable_context": []}
    try:
        raw = llm.complete_json(
            purpose="kb_extract",
            system=SYS_EXTRACT,
            user=f"语料（{chunk.author}《{chunk.book}》{chunk.locator}）：\n{chunk.text}",
            fallback=fallback,
        )
    except Exception:
        return mock_extract(chunk)

    type_map = {t.value: t for t in KUType}
    # 先规范化，不达标（域为空/要点不足 2 条/标题为空）直接回退规则抽取，
    # 绝不让脏 shape 进入 pydantic 构造（min_length=2 会抛 ValidationError）。
    domains = [d for d in _as_list(raw.get("domains")) if isinstance(d, str) and d.strip()]
    dilemmas = [d for d in _as_list(raw.get("dilemmas")) if isinstance(d, str) and d.strip()]
    points = [p for p in (_as_str(x) for x in _as_list(raw.get("key_points"))) if p]
    title = _as_str(raw.get("title")) or _short_title(chunk.text)
    if not domains or len(points) < 2 or not title:
        return mock_extract(chunk)
    evidence = _as_str(raw.get("evidence_level"), "B")
    unit = KnowledgeUnit(
        id="KU-LEAD-000",
        title=title,
        type=type_map.get(_as_str(raw.get("type")), KUType.THEORY),
        domains=domains,
        dilemmas=dilemmas,
        evidence_level=EvidenceLevel(evidence) if evidence in ("A", "B", "C") else EvidenceLevel.B,
        contested=False,
        source=SourceRef(book=chunk.book, author=chunk.author, year=chunk.year,
                         locator=chunk.locator, copyright_status=chunk.copyright_status),
        key_points=points,
        misconceptions=[Misconception(claim=_as_str(m.get("claim")), correction=_as_str(m.get("correction")))
                        for m in _as_list(raw.get("misconceptions")) if isinstance(m, dict)],
        applicable_context=[_as_str(x) for x in _as_list(raw.get("applicable_context")) if _as_str(x)],
        relations=[Relation(type="提出者", target=chunk.author)],
    )
    # dilemmas 为空时质检 K2 会打回，这是设计内的质量反馈（不静默补造）
    return UnitDraft(id="DR-0000", unit=unit, corpus_ids=[chunk.id],
                     status="pending_review", created_at=_now(), updated_at=_now())


# ---------------------------------------------------------------------------
# 质检 K1-K5
# ---------------------------------------------------------------------------
def _norm_title(t: str) -> str:
    return re.sub(r"[\s，。、（）()：:的了和与及]", "", t)


def validate_draft(draft: UnitDraft, existing_units: list[KnowledgeUnit],
                   corpus_text: str = "") -> DraftQualityReport:
    u = draft.unit
    checks: list[QualityCheck] = []

    # K1 溯源完整：无据不答的工程约束，引用定位必填
    s = u.source
    ok = bool(s.book.strip() and s.author.strip() and s.locator.strip())
    checks.append(QualityCheck(code="K1", name="溯源完整", passed=ok,
                               detail=f"{s.author}《{s.book}》{s.locator}" if ok else "书名/作者/定位不可为空"))

    # K2 代码合法：域/困境/类型必须在受控代码表内
    bad_d = [d for d in u.domains if d not in COMPETENCY_DOMAINS]
    bad_g = [d for d in u.dilemmas if d not in DILEMMA_INDEX]
    ok = bool(u.domains) and bool(u.dilemmas) and not bad_d and not bad_g
    detail = "域/困境代码合法" if ok else f"非法代码 domain={bad_d} dilemma={bad_g} 或为空"
    checks.append(QualityCheck(code="K2", name="代码合法", passed=ok, detail=detail))

    # K3 要点化：≥2 条要点且每条 8-200 字；拼接不得长于语料原文（防整段复制）
    lens = [len(p) for p in u.key_points]
    total = sum(lens)
    ok = len(u.key_points) >= 2 and all(8 <= n <= 200 for n in lens)
    if ok and corpus_text and total > len(corpus_text):
        ok = False
    checks.append(QualityCheck(code="K3", name="要点化摘要", passed=ok,
                               detail=f"{len(u.key_points)} 条要点，共 {total} 字" if ok
                               else "要点需 2-4 条、每条 8-200 字且不得整段复制语料"))

    # K4 误区结构：有则结构必须完整（claim≠correction 均非空）
    ok = True
    for m in u.misconceptions:
        if not m.claim.strip() or not m.correction.strip() or m.claim == m.correction:
            ok = False
    checks.append(QualityCheck(code="K4", name="误区结构", passed=ok,
                               detail=f"{len(u.misconceptions)} 条误区" if ok else "误区 claim/correction 不可为空或相同"))

    # K5 唯一性：与现有单元标题不可重复或高度相似（Jaccard≥0.85 视为重复）
    def jaccard(a: str, b: str) -> float:
        sa, sb = set(a), set(b)
        return len(sa & sb) / max(len(sa | sb), 1)
    nt = _norm_title(u.title)
    dup = next((e for e in existing_units
                if _norm_title(e.title) == nt or jaccard(nt, _norm_title(e.title)) >= 0.85), None)
    checks.append(QualityCheck(code="K5", name="唯一性", passed=dup is None,
                               detail="标题无重复" if dup is None else f"与现有单元「{dup.title}」重复/高相似"))

    return DraftQualityReport(passed=all(c.passed for c in checks), checks=checks)


def next_unit_id(existing: list[KnowledgeUnit], domains: list[str]) -> str:
    """commit 时分配正式编号：含 STRAT 走 STRAT 序列，否则 LEAD 序列，均顺延查重。"""
    prefix = "STRAT" if "STRAT" in domains else "LEAD"
    used = {int(u.id.split("-")[2]) for u in existing if u.id.startswith(f"KU-{prefix}-")}
    n = 1
    while n in used:
        n += 1
    return f"KU-{prefix}-{n:03d}"


# ---------------------------------------------------------------------------
# 五节点
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def ingest_corpus(state: KBState, deps: KBNodeDeps) -> dict:
    """采集：规范化语料块并落 L1 层（corpus/）。"""
    chunk = state["corpus"]
    if chunk.id == "CC-0000":  # 未分配编号的输入
        chunk.id = deps.corpus_store.next_chunk_id()
    deps.corpus_store.save_chunk(chunk)
    return {"corpus": chunk, "status": "ingested"}


def extract_draft(state: KBState, deps: KBNodeDeps) -> dict:
    """加工：语料 → 草稿。在线模式走 LLM，Mock/在线失败走规则抽取。"""
    chunk = state["corpus"]
    if deps.llm.name.startswith("mock"):
        draft = mock_extract(chunk)
    else:
        draft = online_extract(deps.llm, chunk)
    draft.id = f"DR-{draft.corpus_ids[0].split('-')[1]}"  # 与语料编号对应，便于回溯
    return {"draft": draft, "status": "extracted"}


def validate_draft_node(state: KBState, deps: KBNodeDeps) -> dict:
    """质检：K1-K5 规则门禁 + 与现有单元查重。"""
    draft = state["draft"]
    report = validate_draft(draft, deps.store.load_units(), state["corpus"].text)
    draft.quality = report
    draft.updated_at = _now()
    status = "validated" if report.passed else "rejected"
    return {"draft": draft, "quality": report, "status": status}


def human_review_node(state: KBState, deps: KBNodeDeps) -> dict:
    """人工审核：质检通过的草稿挂起等待审核员批准/打回。"""
    draft = state["draft"]
    payload = {
        "draft_id": draft.id,
        "title": draft.unit.title,
        "domains": draft.unit.domains,
        "dilemmas": draft.unit.dilemmas,
        "key_points": draft.unit.key_points,
        "source": f"{draft.unit.source.author}《{draft.unit.source.book}》{draft.unit.source.locator}",
        "quality": [{"code": c.code, "name": c.name, "passed": c.passed, "detail": c.detail}
                    for c in (draft.quality.checks if draft.quality else [])],
    }
    if deps.auto_approve:
        return {"decision": {"action": "approve", "reviewer": "auto", "note": "自动批准（Demo/评测）"},
                "status": "reviewed"}
    decision, updates = human_gate(state, deps, "kb_draft_review", payload)
    if decision is None:
        return updates
    return {"decision": decision, "status": "reviewed"}


def reject_draft(state: KBState, deps: KBNodeDeps) -> dict:
    """打回：草稿标记 rejected，携审核意见落台账（修正后可重新提交语料）。"""
    draft = state["draft"]
    decision = state.get("decision") or {}
    draft.status = "rejected"
    draft.review_note = decision.get("note", "")
    draft.reviewer = decision.get("reviewer", "reviewer")
    draft.updated_at = _now()
    deps.store.log_review(state["run_id"], "kb_draft_reject",
                          {"draft_id": draft.id, "note": draft.review_note})
    return {"draft": draft, "status": "rejected"}


def commit_draft(state: KBState, deps: KBNodeDeps) -> dict:
    """入库：分配正式编号 → 写 units → 语料标记 extracted → 台账。"""
    draft = state["draft"]
    decision = state.get("decision") or {}
    if decision.get("action") == "reject":  # 防御：路由异常时也不误入库
        return reject_draft(state, deps)
    reviewer = decision.get("reviewer", "reviewer")

    unit = draft.unit.model_copy(deep=True)
    unit.id = next_unit_id(deps.store.load_units(), unit.domains)
    deps.store.save_unit(unit)

    chunk = state["corpus"].model_copy(deep=True)
    chunk.status = "extracted"
    chunks = [c for c in deps.corpus_store.load_chunks() if c.id != chunk.id] + [chunk]
    deps.corpus_store.save_chunks(chunks)

    draft.status = "committed"
    draft.reviewer = reviewer
    draft.review_note = decision.get("note", "")
    draft.updated_at = _now()
    deps.store.log_review(state["run_id"], "kb_commit",
                          {"draft_id": draft.id, "unit_id": unit.id, "reviewer": reviewer})
    return {"draft": draft, "committed_unit_id": unit.id, "status": "committed"}


KB_NODES = {
    "ingest_corpus": ingest_corpus,
    "extract_draft": extract_draft,
    "validate_draft": validate_draft_node,
    "human_review": human_review_node,
    "reject_draft": reject_draft,
    "commit_draft": commit_draft,
}
