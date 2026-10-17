"""检索层对比评测：BM25(memory) vs 稠密向量(milvus + GLM embedding-3)。

用法（Key 由调用方注入环境变量，脚本不落盘、不打印密钥）：
    GLM_API_KEY=<key> python scripts/bench_retrieval.py

评测两组查询：
  A. 黄金集 12 条（eval/golden/course-design-v1.jsonl，带 expected_unit）—— 客观基准
  B. 语义改写 6 条（人工构造，刻意避开原查询中的专有名词与关键词）—— 检验语义召回能力

指标：recall@1 / recall@3 / recall@5 / MRR，外加单次查询延迟。
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.kb_json import JsonKnowledgeStore  # noqa: E402
from app.adapters.retrieval_memory import MemoryRetriever  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

# B 组：语义改写查询（人工构造，期望单元为我的人工判定，非黄金集）
PARAPHRASE = [
    ("下面的人一遇到问题就来问我要答案，我把活派给他们又担心出乱子", "KU-LEAD-006"),
    ("辅导下属时先让他自己说出想达成什么、现在卡在哪儿，再一起想办法", "KU-LEAD-004"),
    ("给同事提意见时，要讲清楚当时具体发生了什么、带来了什么后果", "KU-LEAD-005"),
    ("业务还在摸索阶段，用硬性考核指标还是用更有野心的目标来牵引", "KU-STRAT-007"),
    ("怎么判断公司手里的资源能不能变成长期的竞争优势", "KU-STRAT-005"),
    ("为什么很多战略文件写出来全是口号，看不出到底要做什么取舍", "KU-STRAT-003"),
]


def load_dotenv_if_exists() -> None:
    """从仓库根目录 .env 读取密钥（.env 已在 .gitignore 中），避免密钥出现在命令行。"""
    env_file = REPO / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_golden() -> list[tuple[str, str]]:
    import json
    rows = (REPO / "eval" / "golden" / "course-design-v1.jsonl").read_text(
        encoding="utf-8").splitlines()
    out = []
    for line in rows:
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("kind") == "query" and r.get("expected_unit"):
            out.append((r["query"], r["expected_unit"]))
    return out


def evaluate(name: str, retriever, pairs: list[tuple[str, str]], top_k: int = 5) -> dict:
    r1 = r3 = r5 = 0
    rr = []
    lat = []
    for q, expected in pairs:
        t0 = time.perf_counter()
        hits = retriever.search(q, top_k=top_k)
        lat.append((time.perf_counter() - t0) * 1000)
        ids = [u.id for u in hits]
        if expected in ids[:1]:
            r1 += 1
        if expected in ids[:3]:
            r3 += 1
        if expected in ids[:5]:
            r5 += 1
        rank = ids.index(expected) + 1 if expected in ids else None
        rr.append(1.0 / rank if rank else 0.0)
    n = len(pairs)
    return {
        "mode": name,
        "n": n,
        "recall@1": round(r1 / n, 3),
        "recall@3": round(r3 / n, 3),
        "recall@5": round(r5 / n, 3),
        "MRR": round(sum(rr) / n, 3),
        "avg_ms": round(statistics.mean(lat), 1),
    }


def main() -> int:
    load_dotenv_if_exists()
    key = os.environ.get("GLM_API_KEY") or os.environ.get("ZHIPU_API_KEY")
    if not key:
        print("未找到密钥。请在仓库根目录创建 .env（已 git 忽略），内容一行：")
        print("    GLM_API_KEY=你的智谱APIKey")
        print("然后重跑：python scripts/bench_retrieval.py")
        return 2

    from app.adapters.embedding_glm import GLMEmbedding
    from app.adapters.retrieval_milvus import MilvusRetriever

    import shutil
    tmp_root = Path(tempfile.mkdtemp())
    review_tmp = tmp_root / "reviews"
    store = JsonKnowledgeStore(REPO / "knowledge", review_log_dir=review_tmp)
    units = store.load_units()
    print(f"知识单元数：{len(units)}")

    bm25 = MemoryRetriever(units)

    emb = GLMEmbedding()
    probe = emb.embed(["连通性检查"])
    print(f"Embedding：{emb.name}  声明维度={emb.dim}  实测维度={len(probe[0])}  last_error={emb.last_error}")
    if emb.last_error or len(probe[0]) != emb.dim:
        print("在线 embedding 异常，终止。")
        return 3

    tmp = Path(tempfile.mkdtemp())
    vec_start = time.perf_counter()
    dense = MilvusRetriever(embedder=emb, units=units, uri=str(tmp / "bench.db"))
    print(f"向量库构建（{len(units)} 单元 embedding + 入库）：{time.perf_counter() - vec_start:.1f}s")

    from app.adapters.retrieval_hybrid import HybridRetriever
    hybrid = HybridRetriever(MemoryRetriever(units), dense)

    modes = [
        ("BM25 (memory)", bm25),
        ("向量 (milvus+glm)", dense),
        ("混合 (hybrid)", hybrid),
    ]

    groups = [("A 黄金集(12条)", load_golden()), ("B 语义改写(6条)", PARAPHRASE)]

    print("\n" + "=" * 78)
    for gname, pairs in groups:
        print(f"\n【{gname}】")
        print(f"{'模式':<20}{'R@1':>7}{'R@3':>7}{'R@5':>7}{'MRR':>8}{'平均延迟':>10}")
        results = {}
        for mname, r in modes:
            results[mname] = evaluate(mname, r, pairs)
            row = results[mname]
            print(f"{row['mode']:<20}{row['recall@1']:>7}{row['recall@3']:>7}"
                  f"{row['recall@5']:>7}{row['MRR']:>8}{str(row['avg_ms']) + 'ms':>10}")
        b = results["BM25 (memory)"]["MRR"]
        v = results["向量 (milvus+glm)"]["MRR"]
        h = results["混合 (hybrid)"]["MRR"]
        print(f"  → 混合相对 BM25：{h - b:+.3f} | 混合相对纯向量：{h - v:+.3f}")
        print(f"  → 关键判定：混合是否同时不低于两者 —— {'是 ✓' if h >= max(b, v) - 1e-9 else '否 ✗'}")

    print("\n" + "=" * 78)
    print("【逐条明细：B 组语义改写】")
    for q, exp in PARAPHRASE:
        b = [u.id for u in bm25.search(q, top_k=3)]
        v = [u.id for u in dense.search(q, top_k=3)]
        h = [u.id for u in hybrid.search(q, top_k=3)]
        bmark = "✓" if exp in b[:1] else ("~" if exp in b else "✗")
        vmark = "✓" if exp in v[:1] else ("~" if exp in v else "✗")
        hmark = "✓" if exp in h[:1] else ("~" if exp in h else "✗")
        print(f"\n  查询：{q}")
        print(f"    期望 {exp}")
        print(f"      BM25:{bmark}{b}")
        print(f"      向量 :{vmark}{v}")
        print(f"      混合 :{hmark}{h}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
