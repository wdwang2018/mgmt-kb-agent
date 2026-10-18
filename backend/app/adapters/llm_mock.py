"""Mock LLM 适配器：确定性模板生成器（P0 离线可跑、测试可回归）。

定位说明（诚实边界）：
- 本适配器保证流水线在无 API Key 环境下端到端可运行、可测试、可评测；
- 它只做「结构化装配 + 模板措辞」，内容的事实性来自检索到的知识单元（引用真实）；
- 生产环境内容深度由 DeepSeek 适配器承担，切换方式见 llm_factory.py。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..ports import LLMClient

# 新任管理者转型 / 战略共识 的主题关键词（parse_need 规则用）
_NEW_MANAGER_KW = ["新任", "新经理", "晋升", "转身", "转型", "角色转换", "从技术到管理", "骨干"]
_STRATEGY_KW = ["战略", "共识", "解码", "目标对齐", "OKR", "KPI", "落地", "打仗", "战略宣贯"]

# 文章解析（purpose=analyze_article）的规则化关键词表。
# 只做命中、不做理解——Mock 无法判断文章深浅，故可行判定刻意保守（见 _analyze_article）。
_ARTICLE_DOMAIN_KW = {
    "STRAT": ["战略", "竞争优势", "定位", "蓝海", "商业模式", "护城河"],
    "TEAM": ["团队", "领导力", "领导", "下属", "带人", "班子成员"],
    "CHANGE": ["变革", "转型", "颠覆式", "创新", "组织变革"],
    "COMM": ["沟通", "表达", "影响力", "说服", "谈判", "汇报"],
    "COACH": ["教练", "辅导", "带教", "反馈", "提问", "GROW"],
    "EXEC": ["执行", "落地", "绩效", "目标管理", "效率", "复盘"],
}
_ARTICLE_DILEMMA_KW = {
    "D01": ["新经理", "角色转型", "晋升", "从骨干到管理", "埋头做事"],
    "D02": ["授权", "放权", "不敢放", "放了乱"],
    "D03": ["团队冲突", "内耗", "人际摩擦"],
    "D04": ["跨部门", "协同", "推不动", "扯皮"],
    "D05": ["变革阻力", "抵触", "消极对抗"],
    "D06": ["战略方向", "不知道往哪打"],
    "D07": ["共识", "各唱各调"],
    "D08": ["执行断裂", "策略停在", "落不了地"],
    "D09": ["绩效面谈", "谈分低"],
    "D10": ["反馈", "说崩", "不说"],
    "D11": ["激励失效", "留不住心", "加薪也"],
    "D12": ["骨干流失", "人才保留", "关键人才"],
    "D13": ["向上沟通", "要资源", "向上管理"],
    "D14": ["会议低效", "会而不议", "议而不决"],
    "D15": ["决策拖延", "优柔寡断", "决策质量"],
    "D16": ["目标设定", "目标对齐", "目标虚设"],
    "D17": ["时间管理", "优先级", "被事务拖"],
    "D18": ["新人融入", "文化传承", "融入慢"],
    "D19": ["远程", "混合团队", "看不见"],
    "D20": ["危机", "压力应对", "慌乱"],
    "D21": ["创新推动", "怕犯错", "不试错"],
    "D22": ["资源争夺", "预算博弈"],
    "D23": ["问责", "追责", "无人负责"],
    "D24": ["继任", "人才梯队", "无人可用"],
}
_ARTICLE_LINEAGE_KW = {
    "L-CLASSIC": ["泰勒", "法约尔", "韦伯", "科学管理", "官僚制"],
    "L-BEHAV": ["马斯洛", "麦格雷戈", "赫茨伯格", "激励理论", "行为科学"],
    "L-CONTINGENCY": ["权变", "费德勒", "情境领导", "路径-目标", "明茨伯格"],
    "L-DECISION": ["西蒙", "卡尼曼", "有限理性", "决策科学", "系统1"],
    "L-STRATEGY": ["波特", "钱德勒", "克里斯坦森", "五种力量"],
    "L-LEADERSHIP": ["科特", "库泽斯", "格林里夫", "柯林斯", "五级领导"],
    "L-QUALITY": ["戴明", "精益", "丰田", "大野耐一", "质量管理"],
    "L-ORGLEARN": ["野中郁次郎", "学习型组织", "SECI", "知识创造"],
    "L-CHINA": ["曾仕强", "华为", "海尔", "本土管理", "东方管理"],
}


# 正文哨兵标记（与 article_analyzer 的 _USER 模板严格对应）
_BODY_RE = re.compile(r"【正文】\s*\n(.*?)\n【正文结束】", re.S)


def _slice_body(prompt: str) -> str:
    """从完整 prompt 里切出正文；切不到就原样返回（便于直接拿正文做单测）。"""
    m = _BODY_RE.search(prompt)
    return m.group(1).strip() if m else prompt


def _pick(options: list[str], seed: str) -> str:
    return options[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(options)]


class MockLLM(LLMClient):
    name = "mock-deterministic-v1"

    # ------------------------------------------------------------------ parse
    def _parse_need(self, raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
        """规则化需求解析：只补全缺失/可高置信推断的字段，绝不覆盖调用方已提供的结构化值。

        痛点/期望行为等自由文本的深度解析是在线 LLM 的职责；Mock 仅做确定性子集。
        """
        theme = None
        if any(k in raw for k in _NEW_MANAGER_KW):
            theme = "新任管理者转型"
        elif any(k in raw for k in _STRATEGY_KW):
            theme = "战略共识与解码"

        audience = fallback.get("audience") or "中层管理者"
        if "高管" in raw or "总监" in raw:
            audience = "高管"
        elif "中层" in raw:
            audience = "中层管理者"
        elif "新任" in raw or "新经理" in raw:
            audience = "新任经理"

        m = re.search(r"(互联网|制造|零售|金融|医药|教育|软件)", raw)
        industry = m.group(1) if m else (fallback.get("industry") or "通用")

        audience_level = fallback.get("audience_level") or 2
        if theme and audience_level == 2:  # 仅在调用方未显式指定时按主题推断
            audience_level = 2 if theme == "新任管理者转型" else 3

        return {
            "audience": audience,
            "audience_level": audience_level,
            "headcount": fallback.get("headcount", 24),
            "duration_minutes": fallback.get("duration_minutes", 360),
            "industry": industry,
            "theme_hint": theme,
        }

    def _analyze_article(self, raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
        """规则化文章解析：只做关键词命中，不判断文章深浅。

        诚实边界（与在线模型的分工）：
        - 命中能力域/困境/学派靠关键词表，摘要靠按句截取，二者都是转述的近似而非理解；
        - 因此 feasible 判定刻意保守：正文够长 **且** 命中已知能力域才判可行，
          其余一律判不可行并说清缺什么。Mock 不把命中不了的东西包装成能成课。
        """
        text = raw or ""

        def hits(table: dict[str, list[str]]) -> list[str]:
            return [c for c, kws in table.items() if any(k in text for k in kws)]

        domains = hits(_ARTICLE_DOMAIN_KW)
        dilemmas = hits(_ARTICLE_DILEMMA_KW)
        lineages = hits(_ARTICLE_LINEAGE_KW)

        # 按句切分（换行不当句子边界）：文章的自然换行会把一句切成两行，
        # 若把 \n 也算断句，摘要就会是「带了团队之后突然失灵了」这种碎句。
        sents = [re.sub(r"\s+", " ", s).strip()
                 for s in re.split(r"[。！？]+", text) if len(s.strip()) >= 8]
        summary = sents[:4]

        # 课型按正文长度推荐：内容量决定了撑得起多久的课
        n = len(text)
        if n < 800:
            ctype = "MICRO"
        elif n < 2000:
            ctype = "SHORT"
        elif n < 5000:
            ctype = "HALF_DAY"
        elif n < 12000:
            ctype = "FULL_DAY"
        else:
            ctype = "SERIES"
        minutes = {"MICRO": 60, "SHORT": 120, "HALF_DAY": 180,
                   "FULL_DAY": 360, "SERIES": 720}[ctype]

        long_enough = n >= 300
        feasible = long_enough and bool(domains)
        if feasible:
            reason = f"正文 {n} 字，命中能力域「{'、'.join(domains)}」，具备成课基础"
        elif not long_enough:
            reason = f"正文仅 {n} 字，内容量不足以支撑 60 分钟以上的课程"
        else:
            reason = "未命中任何已知能力域，课程定位不清晰，需人工确认主题方向"

        # 证据等级：认不出实证依据就取最保守的 C
        ev = "B" if any(k in text for k in ("研究", "实证", "元分析", "实验")) else "C"

        # 核心概念：取实际命中的关键词本身（去重保序）
        concepts: list[str] = []
        for table in (_ARTICLE_DOMAIN_KW, _ARTICLE_LINEAGE_KW):
            for code in [c for c in table if c in domains or c in lineages]:
                for k in table[code]:
                    if k in text and k not in concepts:
                        concepts.append(k)
        # 标题取首行：按句切分的第一句会把换行后的正文一起吞进来
        first_line = text.strip().split("\n")[0].strip()
        return {
            "title": (first_line[:40] if first_line else "（未能识别标题）"),
            "summary": summary,
            "core_concepts": concepts[:5],
            "domains": domains, "dilemmas": dilemmas, "lineages": lineages,
            "evidence_level": ev,
            "feasible": feasible,
            "feasibility_reason": reason,
            "course_type": ctype,
            "audience_reco": "企业管理者与部门负责人" if feasible else "",
            "suggested_theme": (concepts[0] if concepts else ""),
            "suggested_pain": "",
            "suggested_behavior": "",
            "suggested_duration": minutes if feasible else None,
            "suggested_headcount": 30 if feasible else None,
            "suggested_level": 2 if feasible else None,
        }

    def complete_json(self, purpose: str, system: str, user: str, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            ctx = json.loads(user) if user.strip().startswith("{") else {"raw": user}
        except json.JSONDecodeError:
            ctx = {"raw": user}

        if purpose == "parse_need":
            return self._parse_need(ctx.get("raw", ""), fallback)

        if purpose == "objectives":
            out = []
            for i, t in enumerate(ctx.get("targets", [])):
                verb = _pick(["能够", "可以", "会"], f"{purpose}{i}")
                out.append({
                    "target_index": i,
                    "text": f"{verb}在真实管理情境中{t['anchor']}",
                    "bloom": min(5, t["target_level"]),
                })
            return {"objectives": out}

        if purpose == "assessment_stem":
            unit_id = ctx.get("unit_id", "KU-X")
            seed = int(hashlib.md5(unit_id.encode()).hexdigest(), 16)
            pos = seed % 4
            correct = ctx["correction"]
            pool = [
                "完全顺其自然，等下属自己领悟",
                "用更高压的管控弥补当前不足",
                "只在绩效考核时集中说明问题",
                "先按过去证明有效的经验继续执行",
                "把问题归因于下属态度并当众批评",
                "用统一流程一刀切地处理所有情境",
            ]
            distractors = [pool[(seed + 1 + i) % len(pool)] for i in range(3)]
            options = distractors[:]
            options.insert(pos, correct)
            return {
                "stem": f"关于「{ctx['unit_title']}」，以下哪种做法最符合理论证据？",
                "options": options[:4],
                "answer_index": pos,
                "rationale": f"理论证据支持：{correct}",
            }

        if purpose == "analyze_article":
            return self._analyze_article(_slice_body(ctx.get("raw", "")), fallback)

        if purpose == "quiz_generate":
            return self._quiz_generate(ctx, fallback)

        if purpose == "operations_generate":
            return self._operations_generate(ctx, fallback)

        return fallback

    def complete(self, purpose: str, system: str, user: str) -> str:
        try:
            ctx = json.loads(user) if user.strip().startswith("{") else {"raw": user}
        except json.JSONDecodeError:
            ctx = {"raw": user}

        if purpose == "gap_summary":
            targets = ctx.get("targets", [])
            names = "、".join(f"{t['domain_name']}（目标 L{t['target_level']}）" for t in targets)
            return f"基于业务痛点「{ctx.get('pain', '')}」与期望行为「{ctx.get('desired', '')}」，判定核心差距集中在：{names}。"

        if purpose == "objective_text":
            return ctx.get("template", "")

        if purpose == "module_title":
            return ctx.get("template", "")

        if purpose == "activity_desc":
            tools = "、".join(ctx.get("tools", [])) or "无"
            return (f"{ctx.get('minutes', 30)} 分钟{ctx.get('bucket_name', '')}活动：{ctx.get('title', '')}。"
                    f"对应学习目标：{'、'.join(ctx.get('objective_ids', []))}。"
                    f"使用工具：{tools}。讲师观察点：学员是否出现目标行为。")

        if purpose == "package_positioning":
            return (f"面向{ctx.get('audience', '管理者')}（{ctx.get('headcount', 0)} 人，"
                    f"{ctx.get('duration_minutes', 0) // 60} 小时）的{ctx.get('theme', '管理')}课程，"
                    f"聚焦「{ctx.get('pain', '')}」，以行为改变为验收标准。")

        if purpose == "kb_chat":
            return self._chat_kb(ctx)

        return ""

    # ------------------------------------------------------------------ 新增三用途（#1/#2/#3）
    def _chat_kb(self, ctx: dict[str, Any]) -> str:
        """知识库问答：基于检索上下文拼答案（Mock 不编造库外内容）。"""
        context = ctx.get("context") or []
        q = ctx.get("question", "")
        if not context:
            return f"关于「{q}」，库内暂无相关知识单元。建议补充该主题语料后再提问。"
        top = context[0]
        first_kp = (top.get("key_points") or [""])[0]
        parts = [f"关于「{q}」，知识库中与「{top.get('title', '')}」"
                 f"（{top.get('citation', '')}，证据 {top.get('evidence_level', '')} 级）相关：{first_kp}。"]
        if len(context) > 1:
            others = "、".join(f"{c.get('title', '')}（{c.get('citation', '')}）" for c in context[1:4])
            parts.append(f"相关单元还有：{others}，可在课程设计中直接引用。")
        return "".join(parts)

    def _quiz_generate(self, ctx: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        """题库：每个模块出一道单选，正确项取自该模块首条理论要点（真实内容）。"""
        modules = ctx.get("modules") or []
        content = ctx.get("content") or {}
        distractor_pool = [
            "完全顺其自然，等下属自己领悟",
            "用更高压的管控弥补当前不足",
            "只在绩效考核时集中说明问题",
            "把问题归因于下属态度并当众批评",
            "用统一流程一刀切地处理所有情境",
            "依赖个人直觉而非结构化工具",
        ]
        items = []
        for i, m in enumerate(modules[:6]):
            mid = m.get("id", f"M{i+1}")
            title = m.get("title", mid)
            pts = content.get(mid) or []
            correct = (pts[0].get("text") if pts else "") or title
            unit_ids = [pts[0].get("unit_id")] if pts and pts[0].get("unit_id") else []
            seed = int(hashlib.md5(mid.encode()).hexdigest(), 16)
            distract = [distractor_pool[(seed + 1 + j) % len(distractor_pool)] for j in range(3)]
            opts = distract[:]
            pos = seed % 4
            opts.insert(pos, correct)
            items.append({
                "id": f"Q{i+1:02d}",
                "kind": "mcq",
                "stem": f"关于「{title}」模块，以下说法正确的是？",
                "options": opts[:4],
                "answer_index": pos,
                "bloom": 2,
                "explanation": f"正确项见课程理论要点：{correct}",
                "module_id": mid,
                "unit_ids": unit_ids,
            })
        return {"items": items}

    def _operations_generate(self, ctx: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        """运营包：对外公开课文风，聚焦招生 / 课前预热 / 课后 30-60-90 行为迁移。"""
        need = ctx.get("need") or {}
        audience = need.get("audience", "管理者")
        theme = need.get("theme_hint", "管理")
        pain = need.get("business_pain", "")
        industry = need.get("industry", "通用")
        positioning = (f"面向{industry}行业的{audience}，围绕「{theme}」聚焦「{pain}」，"
                       f"以可观测的行为改变为验收标准。")
        return {
            "audience_positioning": positioning,
            "selling_points": [
                "理论源自经典管理学著作，每条要点可溯源到具体章节",
                "按 70-20-10 设计：课堂以练习与真实情境为主，讲授从简",
                "配套课前预习与课后 30-60-90 天行为迁移跟进",
            ],
            "promotion_channels": [
                {"channel": "视频号短视频", "method": "发布 60 秒管理场景剧，评论区领课程大纲", "fit": "适合个人 IP 引流"},
                {"channel": "企业内训邀约", "method": "向 HR/培训负责人发送试讲邀约与真实案例", "fit": "适合 B 端成单"},
                {"channel": "公众号推文", "method": "拆解一个常见管理误区成文，文末放报名入口", "fit": "适合内容沉淀与长尾"},
            ],
            "pre_class_prep": [
                "发送预习问卷，收集学员真实管理困境",
                "提供 1 篇核心阅读材料（要点化转述，注明出处）",
                "建立班级群并完成线上破冰",
            ],
            "post_class_followup": [
                {"phase": "30", "goal": "把课堂工具在真实情境用起来", "actions": ["学员提交 1 个已应用的管理场景", "讲师逐一点评反馈"]},
                {"phase": "60", "goal": "形成团队共同语言与习惯", "actions": ["主管旁观并给正向反馈", "小组内互评应用案例"]},
                {"phase": "90", "goal": "行为可观测地迁移", "actions": ["复盘会展示前后行为对比", "把目标行为纳入绩效观察项"]},
            ],
        }
