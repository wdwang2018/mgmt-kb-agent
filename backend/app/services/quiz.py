"""测验题库生成与自动评分（#2）。

设计要点：
- 题库从已成型的课程包派生（模块大纲 + 理论要点 + 评估题），保证「考的是课上讲的」；
- 生成走 LLM（complete_json, purpose="quiz_generate"），但离线 Mock 也能确定性产出；
- 评分是**纯规则**：每题有唯一 answer_index，与学员作答下标比对即可，不依赖 LLM，
  故可离线、可复现、可解释（每题都带 explanation）。
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.models import CoursePackage, QuizItem

_SYSTEM = (
    "你是培训测评设计助手。基于给定的课程包（模块大纲与理论要点），出一套闭卷测验题。\n"
    "要求：\n"
    "1. 每题必须是单选题（mcq），有 4 个选项，且 answer_index 指向唯一正确项；\n"
    "2. 题干与正确项必须来自课程包的真实内容（模块标题 / 理论要点 / 评估题），不要编造；\n"
    "3. 干扰项要 plausible（看似合理但错误），不要凑数；\n"
    "4. 每题给 explanation（含正确项依据与出处），bloom 取 1-5；\n"
    "5. 输出 {items: [...]} 结构，字段见下。"
)


def _slim(pkg: CoursePackage) -> dict[str, Any]:
    """只挑生成题库需要的字段，避免 prompt 过大。"""
    d = pkg.model_dump()
    return {
        "need": d.get("need"),
        "modules": d.get("modules"),
        "content": d.get("content"),
        "objectives": d.get("objectives"),
        "assessments": d.get("assessments"),
    }


def _norm_item(raw: dict[str, Any], idx: int) -> dict[str, Any]:
    """把 LLM 返回的单题规整成 QuizItem 可接受的字典。缺字段用安全默认值补。"""
    opts = raw.get("options") or []
    ai = raw.get("answer_index")
    if not isinstance(ai, int) or not (0 <= ai < len(opts)):
        ai = None  # 规整失败则判为开放题（无答案，不参与自动评分）
    return {
        "id": raw.get("id") or f"Q{idx+1:02d}",
        "kind": raw.get("kind", "mcq"),
        "stem": raw.get("stem", f"第 {idx+1} 题"),
        "options": [str(o) for o in opts],
        "answer_index": ai,
        "bloom": max(1, min(5, int(raw.get("bloom", 2) or 2))),
        "explanation": raw.get("explanation", ""),
        "module_id": raw.get("module_id", ""),
        "unit_ids": raw.get("unit_ids") or [],
    }


def generate_quiz(pkg: CoursePackage, llm, max_items: int = 6) -> list[QuizItem]:
    """从课程包生成题库（结构化 QuizItem 列表）。"""
    user = json.dumps(_slim(pkg), ensure_ascii=False)
    data = llm.complete_json("quiz_generate", _SYSTEM, user, fallback={"items": []})
    items = [QuizItem(**_norm_item(r, i)) for i, r in enumerate(data.get("items", []))]
    return items[:max_items]


def grade_quiz(items: list[QuizItem], answers: list[int]) -> dict[str, Any]:
    """规则化自动评分：逐题比对 answer_index。

    返回 {score, total, percent, detail:[{index,selected,correct,answer_index}]}，
    detail 供前端逐题回显正确/错误与解析。
    """
    total = len(items)
    correct = 0
    detail = []
    for i, it in enumerate(items):
        sel = answers[i] if i < len(answers) else -1
        ok = it.answer_index is not None and sel == it.answer_index
        if ok:
            correct += 1
        detail.append({
            "index": i,
            "selected": sel,
            "correct": ok,
            "answer_index": it.answer_index,
        })
    percent = round(100 * correct / total) if total else 0
    return {"score": correct, "total": total, "percent": percent, "detail": detail}
