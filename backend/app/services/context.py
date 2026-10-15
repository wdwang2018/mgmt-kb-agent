"""组合根：装配适配器 → NodeDeps → 流水线（应用启动的唯一接线处）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..adapters.corpus_json import JsonCorpusStore
from ..adapters.kb_json import JsonKnowledgeStore
from ..adapters.llm_factory import get_llm
from ..adapters.retrieval_memory import MemoryRetriever
from ..graph.knowledge_nodes import KBNodeDeps
from ..graph.nodes import NodeDeps
from .kb_pipeline import KnowledgePipeline
from .pipeline import CourseDesignPipeline

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class AppContext:
    store: JsonKnowledgeStore
    corpus_store: JsonCorpusStore
    retriever: MemoryRetriever
    pipeline: CourseDesignPipeline
    kb_pipeline: KnowledgePipeline
    knowledge_dir: Path
    output_dir: Path

    def reindex(self) -> None:
        """知识流水线入库后重建检索索引（MemoryRetriever.reload 原地刷新，引用不变）。"""
        self.retriever.reload(self.store.load_units())


def build_context(knowledge_dir: str | Path | None = None,
                  output_dir: str | Path | None = None,
                  llm_mode: str | None = None,
                  hitl_mode: str = "interrupt") -> AppContext:
    if llm_mode:
        os.environ["LLM_MODE"] = llm_mode
    k_dir = Path(knowledge_dir) if knowledge_dir else REPO_ROOT / "knowledge"
    o_dir = Path(output_dir) if output_dir else REPO_ROOT / "runs"

    store = JsonKnowledgeStore(k_dir, review_log_dir=o_dir / "reviews")
    corpus_store = JsonCorpusStore(k_dir)
    units = store.load_units()
    retriever = MemoryRetriever(units)
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
                     knowledge_dir=k_dir, output_dir=o_dir)
    kb_deps.on_commit = lambda unit_id: ctx.reindex()  # 入库即重索引
    return ctx
