"""回填 L1 语料层：把每个单元的每条 key_point 落成一条 CorpusChunk（要点级粒度）。

## 它做什么

对每个知识单元的每条要点，生成一条 CC 语料记录：
    CC.text    = 该条要点（要点化转述）
    CC.locator = 该单元 source.locator（如「LPC 量表与情境控制三因素」）
    CC.unit_id = 来源单元 KU 编号（回链）
由此把溯源粒度从「能引到书」降到「每条要点都能引到具体章节」。

## 它明确**不**做什么（重要，别误解）

CorpusChunk.text 的 schema 语义是「授权摘要/要点化转述，**不整文复制**」。
所以这里生成的是**要点化转述**，不是、也不可能是原书逐字原文——我们没有书本全文。
生成物一律标记 `ingested_by="backfill"`，表示「由既有单元反推回填」而非「从书上人工抄录」，
以便与人工采集条目区分、日后可用真书原文替换。

## 存储陷阱（务必守住）

JsonCorpusStore.save_chunks() 会全量重写到**单一文件** corpus-v1.jsonl，
而 load_chunks() 读取 corpus 目录下**所有** *.jsonl。因此绝不能另建第二个 jsonl：
一旦流水线再调用 save_chunk()，两个文件的内容会被并进 corpus-v1.jsonl 而旧文件仍在，
下次 load 就全部重复。本脚本只经 save_chunks() 写入那一个文件。

用法（幂等，可反复重跑）：
    python scripts/backfill_corpus.py             # 写入
    python scripts/backfill_corpus.py --dry-run   # 只看不写
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # backend/scripts/x.py -> 仓库根
DEFAULT_KNOWLEDGE = ROOT / "knowledge"

sys.path.insert(0, str(ROOT / "backend"))

from app.adapters.corpus_json import JsonCorpusStore  # noqa: E402
from app.adapters.kb_json import JsonKnowledgeStore   # noqa: E402
from app.domain.models import CorpusChunk, KnowledgeUnit  # noqa: E402


def guess_unit_id(chunk: CorpusChunk, units: list[KnowledgeUnit]) -> str | None:
    """为人工采集的语料猜测所属单元。

    两级判定，避免书名重名导致误配：
      1) 书名 **且** 作者都命中 -> 强证据，唯一匹配即采纳
      2) 否则退化为仅书名命中 -> 仍要求唯一
    命中多条一律放弃（宁可不链，不可错链）。
    """
    cb = (chunk.book or "").lower()
    ca = (chunk.author or "").lower()
    author_parts = [p for p in re.split(r"[·\s,，.]+", ca) if p]

    strong, weak = [], []
    for u in units:
        if not u.source:
            continue
        sb = (u.source.book or "").lower()
        sa = (u.source.author or "").lower()
        title_hit = bool(cb and sb) and (cb in sb or sb in cb)
        if not title_hit:
            continue
        author_hit = bool(sa) and any(p in sa for p in author_parts)
        (strong if author_hit else weak).append(u.id)

    if len(strong) == 1:
        return strong[0]
    if len(weak) == 1:
        return weak[0]
    return None


def build_chunks(units: list[KnowledgeUnit], manual: list[CorpusChunk]) -> list[CorpusChunk]:
    """保留人工条目 + 为每个单元的每条要点重建一条语料。"""
    chunks: list[CorpusChunk] = list(manual)

    start = max((int(c.id.split("-")[1]) for c in manual), default=0) + 1
    seq = start
    for u in sorted(units, key=lambda x: x.id):
        if not u.source:
            continue
        for kp in u.key_points:
            chunks.append(
                CorpusChunk(
                    id=f"CC-{seq:04d}",
                    book=u.source.book,
                    author=u.source.author,
                    year=u.source.year,
                    locator=u.source.locator,
                    copyright_status=u.source.copyright_status,  # 继承，不自作主张
                    text=kp,
                    unit_id=u.id,
                    ingested_by="backfill",
                    status="extracted",                          # 已被抽成单元
                )
            )
            seq += 1
    return chunks


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 L1 语料层（要点级粒度）")
    ap.add_argument("--knowledge", default=str(DEFAULT_KNOWLEDGE), help="knowledge 目录")
    ap.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    args = ap.parse_args()

    k_dir = Path(args.knowledge)
    unit_store = JsonKnowledgeStore(str(k_dir))
    corpus_store = JsonCorpusStore(str(k_dir))

    units = unit_store.load_units()
    existing = corpus_store.load_chunks()
    manual = [c for c in existing if c.ingested_by != "backfill"]

    # 给人工条目补回链
    for c in manual:
        if c.unit_id is None:
            guessed = guess_unit_id(c, units)
            if guessed:
                c.unit_id = guessed
                print(f"[link] {c.id} 《{c.book}》 -> {guessed}")

    chunks = build_chunks(units, manual)
    generated = [c for c in chunks if c.ingested_by == "backfill"]
    linked = {c.unit_id for c in generated}

    print(f"units                 : {len(units)}")
    print(f"已有语料（非回填）    : {len(manual)}")
    print(f"回填生成语料          : {len(generated)}  (CC-{min(int(c.id.split('-')[1]) for c in generated):04d}"
          f" .. CC-{max(int(c.id.split('-')[1]) for c in generated):04d})")
    print(f"覆盖单元数            : {len(linked)} / {len(units)}")
    print(f"要点总数校验          : {sum(len(u.key_points) for u in units)}")
    print(f"版权标记继承          : {dict(Counter(c.copyright_status for c in generated))}")

    # 陷阱护栏：只允许单个 jsonl，否则 save/load 语义会导致重复加载
    jsonls = sorted((k_dir / "corpus").glob("*.jsonl"))
    if len(jsonls) > 1:
        print(f"[FATAL] corpus 目录出现多个 jsonl，save_chunks 全量重写会造成重复加载：{[p.name for p in jsonls]}")
        return 2

    ids = [c.id for c in chunks]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    if dupes:
        print(f"[FATAL] 语料编号重复：{dupes}")
        return 2

    if args.dry_run:
        print("[dry-run] 未写入。样例：")
        for c in generated[:2]:
            print("   ", c.model_dump_json(exclude_none=True))
        return 0

    corpus_store.save_chunks(chunks)
    print(f"[ok] 已写入 {jsonls[0].name if jsonls else 'corpus-v1.jsonl'}，共 {len(chunks)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
