"""L1 语料层存储适配器：JSONL 文件实现（P1）。

语料是溯源链条的起点：每条语料带书名/作者/页码定位，
加工成知识单元草稿后标记 extracted；未授权的公版/授权语料
只存要点化摘要，不整文复制。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.models import CorpusChunk


class JsonCorpusStore:
    def __init__(self, knowledge_dir: str | Path):
        self.corpus_dir = Path(knowledge_dir) / "corpus"
        self.corpus_dir.mkdir(parents=True, exist_ok=True)

    def load_chunks(self) -> list[CorpusChunk]:
        chunks: list[CorpusChunk] = []
        for path in sorted(self.corpus_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    chunks.append(CorpusChunk.model_validate_json(line))
        return chunks

    def next_chunk_id(self) -> str:
        existing = [c.id for c in self.load_chunks()]
        if not existing:
            return "CC-0001"
        return f"CC-{max(int(i.split('-')[1]) for i in existing) + 1:04d}"

    def save_chunks(self, chunks: list[CorpusChunk]) -> None:
        """全量重写（语料量为数十条量级，重写比增量追加更安全，避免并发写半行）。"""
        target = self.corpus_dir / "corpus-v1.jsonl"
        with target.open("w", encoding="utf-8") as f:
            for c in chunks:
                f.write(c.model_dump_json(exclude_none=True) + "\n")

    def save_chunk(self, chunk: CorpusChunk) -> None:
        self.save_chunks(self.load_chunks() + [chunk])
