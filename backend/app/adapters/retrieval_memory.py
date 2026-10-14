"""内存混合检索适配器（P0）：BM25 文本召回 + 能力域/困境标签加权。

- 中文按二元字符组（bigram）分词，英文按词切分；
- 标签加权：困境命中权重高于能力域（困境是培训场景的第一索引）；
- dual_recall：按「能力域 × 困境」双路召回，用于内容匹配节点。
P1 替换为 Milvus（稠密 BGE-M3 + 稀疏混合检索）时保持同一协议。
"""

from __future__ import annotations

import math
import re
from collections import Counter

from ..domain.models import KnowledgeUnit
from ..ports import Retriever

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for ch in text:
        if _CJK.match(ch):
            tokens.append(ch)
    # CJK bigrams
    chars = [ch for ch in text if _CJK.match(ch)]
    tokens.extend(f"{a}{b}" for a, b in zip(chars, chars[1:]))
    tokens.extend(w.lower() for w in _WORD.findall(text))
    return tokens


class MemoryRetriever(Retriever):
    def __init__(self, units: list[KnowledgeUnit]):
        self.units = {u.id: u for u in units}
        self._docs = {u.id: tokenize(self._doc_text(u)) for u in units}
        self._df: Counter = Counter()
        for toks in self._docs.values():
            for t in set(toks):
                self._df[t] += 1
        self._avgdl = sum(len(t) for t in self._docs.values()) / max(len(self._docs), 1)

    @staticmethod
    def _doc_text(u: KnowledgeUnit) -> str:
        parts = [u.title, u.type.value, u.source.book, u.source.author]
        parts += u.key_points
        parts += [m.claim + m.correction for m in u.misconceptions]
        parts += u.applicable_context
        return " ".join(parts)

    def _bm25(self, query_tokens: list[str], doc_id: str, k1: float = 1.5, b: float = 0.75) -> float:
        tf = Counter(self._docs[doc_id])
        dl = len(self._docs[doc_id])
        n = max(len(self._docs), 1)
        score = 0.0
        for t in query_tokens:
            if t not in tf:
                continue
            idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
            score += idf * (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * dl / self._avgdl))
        return score

    def search(self, query: str, domains: list[str] | None = None,
               dilemmas: list[str] | None = None, top_k: int = 5) -> list[KnowledgeUnit]:
        q_tokens = tokenize(query)
        scored: list[tuple[float, str]] = []
        for uid in self._docs:
            u = self.units[uid]
            score = self._bm25(q_tokens, uid)
            if domains:
                score += 2.0 * len(set(domains) & set(u.domains))
            if dilemmas:
                score += 3.0 * len(set(dilemmas) & set(u.dilemmas))
            scored.append((score, uid))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [self.units[uid] for s, uid in scored[:top_k] if s > 0]

    def dual_recall(self, domains: list[str], dilemmas: list[str], top_k: int = 6) -> list[KnowledgeUnit]:
        """能力域 × 困境 双路召回：优先双命中，其次单路命中补齐。"""
        both = [u for u in self.units.values() if set(u.domains) & set(domains) and set(u.dilemmas) & set(dilemmas)]
        only_domain = [u for u in self.units.values() if set(u.domains) & set(domains) and u not in both]
        only_dilemma = [u for u in self.units.values() if set(u.dilemmas) & set(dilemmas) and u not in both]
        ordered = both + only_dilemma + only_domain
        return ordered[:top_k]

    def by_ids(self, ids: list[str]) -> list[KnowledgeUnit]:
        return [self.units[i] for i in ids if i in self.units]
