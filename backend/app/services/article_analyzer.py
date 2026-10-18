"""文章解析：读完一篇文章，判断它能否成课、该做成哪种课。

设计说明
--------
1. **刻意不做成流水线节点**：课程设计流水线在线一轮 12–14 分钟，而解析是交互式动作
   （用户要看结果再决定要不要成课）。故解析走独立端点、单次 LLM 调用。
2. **LLM 输出一律规范化后再用**：模型会用代码、中文名、字符串、单值等各种格式返回，
   本模块统一收敛成合法代码列表；非法代码直接丢弃而不是硬塞进领域层。
3. **失败不硬造结论**：LLM 返回不可用就退到 fallback，feasible 默认 False 并由
   feasibility_reason 说清原因。宁可判断不了，也不编造一份看起来专业的解析。
4. summary 是要点化转述，对外不得当作原文引用——这是与语料层同源的版权边界。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..domain.constants import COMPETENCY_DOMAINS, COURSE_TYPES, DILEMMA_INDEX, THEORY_LINEAGE
from ..domain.models import ArticleAnalysis, ArticleSource
from ..ports import LLMClient, Retriever

PURPOSE = "analyze_article"

# 对外公开课口径：本解析服务的受众与课型建议都按这个场景措辞
_SCENE = "对外招生的管理类公开课（商业讲师面向社会学员招生）"

_SYSTEM = f"""你是管理培训领域的课程设计顾问。用户给你一篇文章正文，
你要判断它能否改造成一门{_SCENE}，并给出结构化判断。

严格约束（违反即为错误输出）：
1. summary 必须是你的要点化转述，**禁止整段复制原文**。
2. domains / dilemmas / lineages / course_type 只能使用允许清单里的代码，不得自造。
3. 证据等级必须诚实：有多项研究支撑才给 A，有研究但情境依赖给 B，
   纯个人观点、自媒体感悟、无出处的说法一律给 C。不许把鸡汤抬成实证。
4. 判断不可行时要如实说不可行（feasible=false 并在 reason 里说清缺什么），
   例如：只是资讯/书评/无方法、内容过薄不足 60 分钟、讲的是观点无可迁移行为。
   宁可判不可行，也不要把不适合成课的内容包装成课。

输出 JSON（字段名必须完全一致）：
{{
  "title": "文章标题",
  "summary": ["要点一", "要点二", "要点三"],
  "core_concepts": ["核心概念A", "核心概念B"],
  "domains": ["TEAM"],
  "dilemmas": ["D01"],
  "lineages": ["L-LEADERSHIP"],
  "evidence_level": "B",
  "feasible": true,
  "feasibility_reason": "可行/不可行的具体依据",
  "course_type": "HALF_DAY",
  "audience_reco": "适合报名的学员画像（一句话）",
  "suggested_theme": "课程主题名",
  "suggested_pain": "学员带着什么痛点来",
  "suggested_behavior": "学完能做什么",
  "suggested_duration": 180,
  "suggested_headcount": 30,
  "suggested_level": 2
}}
"""

_USER = """请用下面的正文做判断。

【允许的能力域代码】{domains}
【允许的困境代码】{dilemmas}
【允许的学派代码】{lineages}
【允许的课件类型代码】{course_types}

【正文】
{text}
【正文结束】
"""

# 正文哨兵：Mock 适配器靠它从完整 prompt 里精确切出正文。
# 少了这层，Mock 会把「【允许的能力域代码】STRAT、TEAM...」这类提示清单当成文章
# 内容去做关键词命中与摘要，产出「摘要里混着代码清单」的荒唐结果。
_BODY_START = "【正文】"
_BODY_END = "【正文结束】"


def _fallback() -> dict[str, Any]:
    """调用方契约：传给 complete_json 的 fallback 必须本身合法。

    刻意把 feasible 设为 False——解析不出来时不应该假装能成课。
    """
    return {
        "title": "（未能识别标题）",
        "summary": [],
        "core_concepts": [],
        "domains": [], "dilemmas": [], "lineages": [],
        "evidence_level": "C",
        "feasible": False,
        "feasibility_reason": "未能完成解析（规则化判断不足或模型不可用），请改用在线模型或人工判断",
        "course_type": "HALF_DAY",
        "audience_reco": "", "suggested_theme": "", "suggested_pain": "",
        "suggested_behavior": "",
        "suggested_duration": None, "suggested_headcount": None, "suggested_level": None,
    }


# ---------------------------------------------------------------------------
# 规范化：把 LLM 的各种返回格式收敛成合法代码
# ---------------------------------------------------------------------------
def _norm_str_list(v: Any) -> list[str]:
    """接受 list / 逗号或顿号分隔的字符串 / 单个字符串 / None。

    无论进来的形态是什么，最后都统一走一遍分隔符切分——否则
    ["a", "b\nTEAM"] 这类混合输入会漏切，脏数据会进到下游。
    """
    if v is None:
        raw: list[str] = []
    elif isinstance(v, str):
        raw = [v]
    elif isinstance(v, (list, tuple)):
        raw = []
        for it in v:
            if isinstance(it, dict):  # 常见：模型返回 {"code": "TEAM"} 之类
                raw.append(str(it.get("code") or it.get("name") or it.get("id") or ""))
            else:
                raw.append(str(it))
    else:
        raw = [str(v)]

    out: list[str] = []
    for r in raw:
        for part in re.split(r"[,，、;；\n]+", r):
            p = part.strip()
            if p:
                out.append(p)
    return out


def _norm_sentences(v: Any) -> list[str]:
    """句子列表规范化：**只按换行切，绝不按逗号切**。

    逗号是句子内部的标点，不是列表分隔符。若拿 _norm_str_list（为代码列表设计、
    按逗号切分）来处理摘要，会把「所谓准备度，由能力与意愿两个维度构成」切成两句碎句。
    """
    if v is None:
        raw: list[str] = []
    elif isinstance(v, str):
        raw = v.split("\n")
    elif isinstance(v, (list, tuple)):
        raw = []
        for it in v:
            if isinstance(it, dict):
                raw.append(str(it.get("text") or it.get("summary") or it.get("value") or ""))
            else:
                raw.append(str(it))  # 元素内部换行归入本句，不另起一条
    else:
        raw = [str(v)]
    return [re.sub(r"\s+", " ", p).strip() for p in raw if p and p.strip()]


def _code_table(mapping: dict) -> dict[str, str]:
    """把 {code: {...}/str} 统一成 {code: 中文名}，兼容两种取值形态。"""
    out = {}
    for code, meta in mapping.items():
        if isinstance(meta, str):
            out[code] = meta
        elif isinstance(meta, dict):
            out[code] = str(meta.get("name", ""))
    return out


def _resolve_codes(v: Any, table: dict[str, str]) -> list[str]:
    """先按代码精确匹配，再按中文名反查；非法项直接丢弃，去重保序。"""
    out: list[str] = []
    for item in _norm_str_list(v):
        code = item.strip()
        if code in table:
            if code not in out:
                out.append(code)
            continue
        # 中文名反查（模型常给中文名而非代码）
        hit = next((c for c, n in table.items() if n and n == item), None)
        if hit is None:  # 退化：包含匹配，如“团队管理”命中“团队管理”
            hit = next((c for c, n in table.items() if n and (n in item or item in n)), None)
        if hit and hit not in out:
            out.append(hit)
    return out


def _norm_evidence(v: Any) -> str:
    s = _norm_str_list(v)
    raw = (s[0] if s else str(v or "")).strip().upper()
    if raw in {"A", "B", "C"}:
        return raw
    # 中文描述反查
    for code, desc in (("A", "实证强支持"), ("B", "实证支持"), ("C", "经验")):
        if desc in raw:
            return code
    return "C"  # 认不出来就取最保守的 C，绝不抬升


def _norm_course_type(v: Any) -> str:
    s = _norm_str_list(v)
    raw = (s[0] if s else str(v or "")).strip().upper().replace("-", "_").replace(" ", "_")
    if raw in COURSE_TYPES:
        return raw
    # 双向包含：模型常给简称「全天」「微课」，它们是被包含在全名里的那一侧
    for code, meta in COURSE_TYPES.items():
        if code in raw or meta["name"] in raw or raw in meta["name"]:
            return code
    return "HALF_DAY"


def _norm_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in {"true", "1", "yes", "y", "是", "可行", "可以", "能"}


def _norm_int(v: Any, default: int | None = None) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _clean_prose(s: Any, limit: int = 400) -> str:
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    return t[:limit]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def analyze_article(
    text: str,
    source: ArticleSource,
    llm: LLMClient,
    retriever: Retriever | None = None,
    intake_warnings: list[str] | None = None,
) -> ArticleAnalysis:
    """解析一篇文章。text 为摄入后的正文（可能是截断过的）。"""
    user = _USER.format(
        domains=", ".join(COMPETENCY_DOMAINS),
        dilemmas=", ".join(DILEMMA_INDEX),
        lineages=", ".join(THEORY_LINEAGE),
        course_types=", ".join(COURSE_TYPES),
        text=text,
    )
    raw = llm.complete_json(PURPOSE, _SYSTEM, user, _fallback())
    data = raw if isinstance(raw, dict) else _fallback()

    domains = _resolve_codes(data.get("domains"), _code_table(COMPETENCY_DOMAINS))
    dilemmas = _resolve_codes(data.get("dilemmas"), _code_table(DILEMMA_INDEX))
    lineages = _resolve_codes(data.get("lineages"), _code_table(THEORY_LINEAGE))

    # 摘要用 _norm_sentences（按句保留），不能用 _norm_str_list（会按逗号切碎句子）
    summary: list[str] = []
    for s in _norm_sentences(data.get("summary")):
        c = _clean_prose(s, 300)
        if c and c not in summary:
            summary.append(c)
    summary = summary[:6]

    concepts = [c for c in (_clean_prose(x, 60) for x in _norm_str_list(data.get("core_concepts"))) if c]

    feasible = _norm_bool(data.get("feasible"))
    # 兜底：连摘要都没有，说明这次解析实质失败，不能报“可行”
    if not summary:
        feasible = False

    analysis = ArticleAnalysis(
        source=source,
        title=_clean_prose(data.get("title"), 120) or "（未能识别标题）",
        summary=summary,
        core_concepts=concepts[:8],
        domains=domains,
        dilemmas=dilemmas,
        lineages=lineages,
        evidence_level=_norm_evidence(data.get("evidence_level")),
        feasible=feasible,
        feasibility_reason=_clean_prose(data.get("feasibility_reason"), 300)
        or "解析未给出判定理由",
        course_type=_norm_course_type(data.get("course_type")),
        related_units=[],
        audience_reco=_clean_prose(data.get("audience_reco"), 200),
        suggested_theme=_clean_prose(data.get("suggested_theme"), 120),
        suggested_pain=_clean_prose(data.get("suggested_pain"), 200),
        suggested_behavior=_clean_prose(data.get("suggested_behavior"), 200),
        suggested_duration=_norm_int(data.get("suggested_duration")),
        suggested_headcount=_norm_int(data.get("suggested_headcount")),
        suggested_level=_norm_int(data.get("suggested_level")),
        raw_chars=len(text),
    )

    # 关联知识单元：优先按标签双路召回，再按核心概念文本检索补齐
    analysis.related_units = _find_related_units(analysis, retriever)

    warns = list(intake_warnings or [])
    if not summary:
        warns.append("解析未产出要点，结论不可用，建议换用在线模型或人工判断")
    if feasible and analysis.evidence_level == "C":
        warns.append("证据等级为 C（经验流派），成课时须标明观点来源、不可当作实证结论讲授")
    if feasible and not domains:
        warns.append("未命中任何能力域，课程定位可能不清晰")
    analysis.warnings = warns
    return analysis


def _find_related_units(a: ArticleAnalysis, retriever: Retriever | None, top: int = 6) -> list[str]:
    """回查库内已有知识单元：这是本项目区别于通用摘要工具的价值所在。

    检索失败不抛异常——没有关联单元只是少一个增益，不该让整次解析失败。
    """
    if retriever is None:
        return []
    found: list[str] = []
    try:
        if a.domains and a.dilemmas:
            for u in retriever.dual_recall(a.domains, a.dilemmas, top_k=top):
                if u.id not in found:
                    found.append(u.id)
        for c in a.core_concepts[:3]:
            for u in retriever.search(c, top_k=3):
                if u.id not in found:
                    found.append(u.id)
            if len(found) >= top:
                break
    except Exception:
        return found  # 检索不可用时静默降级，不牵连解析主流程
    return found[:top]
