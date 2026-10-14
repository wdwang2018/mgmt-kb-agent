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

        return ""
