"""组合根：装配适配器 → NodeDeps → 流水线（应用启动的唯一接线处）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..adapters.corpus_json import JsonCorpusStore
from ..adapters.kb_json import JsonKnowledgeStore
from ..adapters.llm_factory import get_llm
from ..adapters.retriever_factory import get_retriever
from ..graph.knowledge_nodes import KBNodeDeps
from ..graph.nodes import NodeDeps
from ..ports import Retriever
from .kb_pipeline import KnowledgePipeline
from .pipeline import CourseDesignPipeline

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_dotenv(env_file: Path | None = None) -> None:
    """把 .env 注入环境变量（setdefault，不覆盖既有环境变量）。

    .env 已在 .gitignore 中，是密钥的落盘位置；此前仅评测脚本会加载，
    导致服务启动时拿不到 Key —— 现由组合根统一加载，脚本与 Web 行为一致。

    env_file 可注入以便测试指向临时文件（避免测试触碰仓库真实 .env，
    也避免 setdefault 的全局副作用污染同会话内的其它测试）。
    """
    path = env_file if env_file is not None else REPO_ROOT / ".env"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))
    except OSError:
        pass  # .env 不可读时不阻断启动（退回纯环境变量/mock 模式）


@dataclass
class AppContext:
    store: JsonKnowledgeStore
    corpus_store: JsonCorpusStore
    retriever: Retriever
    pipeline: CourseDesignPipeline
    kb_pipeline: KnowledgePipeline
    knowledge_dir: Path
    output_dir: Path
    vector_mode: str = "memory"
    vector_note: str | None = None

    def reindex(self) -> None:
        """知识流水线入库后重建检索索引（适配器 reload 原地刷新，引用不变）。

        memory 模式：全量重建 BM25 统计量；
        milvus 模式：upsert 向量并删除已移除项，保证索引与 JSONL 真相源一致。
        """
        self.retriever.reload(self.store.load_units())


def build_context(knowledge_dir: str | Path | None = None,
                  output_dir: str | Path | None = None,
                  llm_mode: str | None = None,
                  hitl_mode: str = "interrupt") -> AppContext:
    _load_dotenv()
    if llm_mode:
        os.environ["LLM_MODE"] = llm_mode
    k_dir = Path(knowledge_dir) if knowledge_dir else REPO_ROOT / "knowledge"
    o_dir = Path(output_dir) if output_dir else REPO_ROOT / "runs"

    store = JsonKnowledgeStore(k_dir, review_log_dir=o_dir / "reviews")
    corpus_store = JsonCorpusStore(k_dir)
    units = store.load_units()
    # 检索模式由 VECTOR_MODE 决定；milvus 不可用时工厂自动降级为 memory 并带回原因
    retriever, vector_mode, vector_note = get_retriever(
        units, uri=str(k_dir / "milvus_kb.db"))
    deps = NodeDeps(
        llm=get_llm(),
        retriever=retriever,
        store=store,
        cases=store.load_cases(),
        output_dir=o_dir,
        hitl_mode=hitl_mode,
    )
    pipeline = CourseDesignPipeline(deps)
    kb_deps = KBNodeDeps(
        llm=deps.llm,
        store=store,
        corpus_store=corpus_store,
        hitl_mode=hitl_mode,
        auto_approve=False,  # Web 审核台走真实人工挂起；Demo/测试用 run_auto
        on_commit=None,      # 占位，装配完成后绑定（闭包引用待创建的 ctx）
    )
    kb_pipeline = KnowledgePipeline(kb_deps, o_dir)
    ctx = AppContext(store=store, corpus_store=corpus_store, retriever=retriever,
                     pipeline=pipeline, kb_pipeline=kb_pipeline,
                     knowledge_dir=k_dir, output_dir=o_dir,
                     vector_mode=vector_mode, vector_note=vector_note)
    kb_deps.on_commit = lambda unit_id: ctx.reindex()  # 入库即重索引
    return ctx
