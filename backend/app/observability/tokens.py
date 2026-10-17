"""Token 估算器（P3 · token 计量）。

为什么放在包装层而不是适配器层：
- LLM 端口（ports.LLMClient）只返回 dict/str，未携带 usage；
- GLM 适配器虽能从 OpenAI 兼容响应拿真实 usage，但 Mock/DeepSeek 不统一；
- 在 InstrumentedLLM 包装层用"文本长度估算"对 prompt/completion 统一计费，
  口径一致、可跨后端比较，且零侵入（不改端口、不改各适配器）。

估算规则（与 GLM/OpenAI 分词近似）：
- CJK 字符（含 CJK 标点 / 全角符号）：每字 ≈ 1 token；
- 拉丁字母 / 数字 / 半角标点 / 空白：约 4 字符 ≈ 1 token（向上取整）。
仅用于"消耗体量"级别的计量，非精确计费；精确值可后续由各适配器上报 usage 覆盖。
"""

from __future__ import annotations

import re

# 中日韩统一表意文字 + 扩展 A + 兼容表意 + CJK 标点/全角符号
_CJK_RE = re.compile(
    r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数（中英混合）。空串返回 0。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4
