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
from collections import Counter
from typing import Any

from ..domain.constants import COMPETENCY_DOMAINS, COURSE_TYPES, DILEMMA_INDEX, THEORY_LINEAGE
from ..domain.models import ArticleAnalysis, ArticleSource, ValueFlag, Verdict
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


# ---------------------------------------------------------------------------
# 内容价值判断层（P6+）：文章一进来先跑零成本规则预筛，
# 尽早揪出「软文 / 哗众取宠 / 无实际价值」，避免把时间浪费在伪干货上。
#
# 设计要点：
# 1. 纯正则 + 简单统计，不调用任何模型，毫秒级；离线(Mock)环境下也完全可用。
#    早判的意义就是"不浪费时间"——若靠 LLM 判断就失去了"早"的价值。
# 2. 每个标记带 severity：hard=建议放弃（调用方可据此短路、不调 LLM）；
#    soft=需谨慎（仍跑 LLM，但结论标 CAUTION 让用户复核）。
# 3. 不掩盖：软文也可能碰巧讲对一句，故 hard 标记同时附 detail 让用户复核。
# ---------------------------------------------------------------------------
_SOFT_SELL_HARD = [
    "立即购买", "限时优惠", "限时特惠", "扫码", "二维码", "加我微信", "加微信",
    "添加客服", "客服微信", "私信我", "关注公众号", "报名咨询", "课程顾问",
    "抢名额", "免费领取", "0元", "点击下方", "回复暗号", "领取资料", "添加老师",
]
_PRICE_RE = re.compile(r"[¥$￥]\s*\d|\d+\s*元|元\s*（|元/|万元|千元|￥\d")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")

_CLICKBAIT_WORDS = [
    "震惊", "揭秘", "颠覆认知", "不可思议", "99%的人", "90%的人", "必看", "速看",
    "再不看", "删前必读", "突发", "看完秒懂", "这才是", "别再", "所有人都错了",
    "真相", "内部", "干货", "惊呆", "不敢相信", "万万没想到", "颠覆", "竟然",
    "原来如此", "一招", "速收藏", "炸锅", "刷屏", "朋友圈疯传", "沸腾", "深度好文",
]
_METHOD_WORDS = ["模型", "框架", "步骤", "方法", "清单", "原则", "公式", "流程",
                 "标准", "指标", "SOP", "机制", "策略", "工具", "量表", "维度", "评估"]
_CITE_WORDS = ["研究", "实证", "据", "《", "》", "教授", "博士", "作者", "实验", "数据",
               "调查", "报告", "meta", "Meta", "论文", "期刊"]
_FARM_WORDS = ["你一定要知道", "人生感悟", "别让", "余生", "格局", "认知", "底层逻辑",
               "开窍", "醒悟", "通透", "活法", "赢家", "穷人思维", "富人思维"]


def _effective_len(text: str) -> int:
    """去除空白后的有效字符数（过薄判定用）。"""
    return len(re.sub(r"\s+", "", text))


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。！？!?\n]+", text) if s.strip()]


def _cheap_title(text: str) -> str:
    for line in text.split("\n"):
        line = line.strip()
        if line:
            return line[:40]
    return "（未能识别标题）"


def _cheap_summary(text: str) -> list[str]:
    sents = _split_sentences(text)[:3]
    return [re.sub(r"\s+", " ", s).strip()[:120] for s in sents if s.strip()]


def pre_screen(text: str) -> list[ValueFlag]:
    """零成本规则预筛：返回命中的内容价值标记（可能为空）。

    刻意做成纯规则、不依赖 LLM。每个标记附 detail 便于用户复核，避免误杀。
    """
    flags: list[ValueFlag] = []
    if not text or not text.strip():
        return flags

    n = _effective_len(text)
    sents = _split_sentences(text)
    n_sent = max(1, len(sents))
    exclaim = text.count("！") + text.count("!")
    question = text.count("？") + text.count("?")
    exclaim_ratio = exclaim / n_sent
    q_density = question / max(1, n / 100.0)

    # —— 软文 / 营销导流（hard）——
    hit_soft = [w for w in _SOFT_SELL_HARD if w in text]
    if hit_soft or _PRICE_RE.search(text) or _PHONE_RE.search(text) or _EMAIL_RE.search(text):
        detail = "命中营销导流信号：" + "、".join(hit_soft[:4])
        if _PRICE_RE.search(text):
            detail += "、含价格/金额"
        if _PHONE_RE.search(text) or _EMAIL_RE.search(text):
            detail += "、含联系方式"
        flags.append(ValueFlag(code="SOFT_SELL", label="软文/营销导流",
                               severity="hard", detail=detail[:120]))

    # —— 哗众取宠 / 标题党 ——
    bait = [w for w in _CLICKBAIT_WORDS if w in text]
    if len(bait) >= 2 or exclaim_ratio > 0.05:
        sev: str = "hard"
    elif bait or q_density > 3:
        sev = "soft"
    else:
        sev = ""
    if sev:
        d = []
        if bait:
            d.append("标题党词：" + "、".join(bait[:4]))
        if exclaim_ratio > 0.05:
            d.append(f"感叹句占比 {exclaim_ratio:.0%}")
        if q_density > 3:
            d.append(f"问号密度 {q_density:.1f}/百字")
        flags.append(ValueFlag(code="CLICKBAIT", label="哗众取宠/标题党",
                               severity=sev, detail="；".join(d)[:120]))

    # —— 内容过薄（soft）——
    if n < 300:
        flags.append(ValueFlag(code="THIN", label="内容过薄",
                               severity="soft", detail=f"有效正文仅 {n} 字，不足支撑 60 分钟课程"))

    # —— 无实质 / 纯感悟煽动（soft）——
    has_num = bool(re.search(r"\d", text))
    has_year = bool(re.search(r"(19|20)\d{2}", text))
    has_method = any(w in text for w in _METHOD_WORDS)
    has_cite = any(w in text for w in _CITE_WORDS)
    avg_len = n / n_sent
    if not (has_num or has_year or has_method or has_cite) and (exclaim_ratio > 0.2 or avg_len < 14):
        detail = "缺乏可验证的实质内容（无数字/年份/方法/引用）"
        if exclaim_ratio > 0.2:
            detail += f"，且感叹句占比偏高（{exclaim_ratio:.0%}）"
        if avg_len < 14:
            detail += "，句长偏短、像口号堆砌"
        flags.append(ValueFlag(code="NO_SUBSTANCE", label="无实质内容",
                               severity="soft", detail=detail[:120]))

    # —— 低质聚合（soft）——
    farm = [w for w in _FARM_WORDS if text.count(w) >= 2]
    norm = [re.sub(r"\s+", "", s) for s in sents]
    rep = [s for s, c in Counter(norm).items() if c >= 3 and len(s) >= 6]
    listy = len(re.findall(r"第\s*[一二三四五六七八九十\d]+\s*[点条步课]", text))
    if farm or rep or listy >= 6:
        d = []
        if farm:
            d.append("套路词重复：" + "、".join(farm[:3]))
        if rep:
            d.append(f"句式重复≥3次（如「{rep[0][:20]}…」）")
        if listy >= 6:
            d.append(f"「第N点」式堆砌 {listy} 处")
        flags.append(ValueFlag(code="CONTENT_FARM", label="低质聚合",
                               severity="soft", detail="；".join(d)[:120]))

    return flags


def _verdict(feasible: bool, flags: list[ValueFlag]) -> tuple[str, str]:
    """三态基本判断：DISCARD / CAUTION / COURSE（返回 code + 中文标签）。"""
    if not feasible:
        return Verdict.DISCARD.value, "建议放弃"
    if any(f.severity == "hard" for f in flags):
        return Verdict.DISCARD.value, "建议放弃"
    if any(f.severity == "soft" for f in flags):
        return Verdict.CAUTION.value, "需谨慎"
    return Verdict.COURSE.value, "可成课"



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
    text = text or ""

    # —— 内容价值判断层（先行、零成本）——
    # 强信号（强软文/标题党）直接短路，不调 LLM，把"不浪费时间"落到实处。
    flags = pre_screen(text)
    if any(f.severity == "hard" for f in flags):
        verdict, verdict_label = _verdict(False, flags)
        return ArticleAnalysis(
            source=source,
            title=_cheap_title(text),
            summary=_cheap_summary(text),
            core_concepts=[],
            domains=[], dilemmas=[], lineages=[],
            evidence_level="C",
            feasible=False,
            feasibility_reason="命中强软文/标题党信号，建议放弃，不投入成课（详见下方标记）",
            course_type="MICRO",
            related_units=[],
            audience_reco="", suggested_theme="", suggested_pain="",
            suggested_behavior="",
            suggested_duration=None, suggested_headcount=None, suggested_level=None,
            value_flags=flags,
            verdict=verdict, verdict_label=verdict_label,
            warnings=["疑似软文/标题党，已跳过深度解析以节省时间"],
            raw_chars=len(text),
        )

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

    # 贴上内容价值标记与基本判断
    analysis.value_flags = flags
    verdict, verdict_label = _verdict(feasible, flags)
    analysis.verdict = verdict
    analysis.verdict_label = verdict_label

    warns = list(intake_warnings or [])
    if not summary:
        warns.append("解析未产出要点，结论不可用，建议换用在线模型或人工判断")
    if feasible and analysis.evidence_level == "C":
        warns.append("证据等级为 C（经验流派），成课时须标明观点来源、不可当作实证结论讲授")
    if feasible and not domains:
        warns.append("未命中任何能力域，课程定位可能不清晰")
    soft_labels = [f.label for f in flags if f.severity == "soft"]
    if soft_labels:
        warns.append("内容价值判断：命中「" + "、".join(soft_labels) + "」，成课前请人工复核其实质价值")
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
