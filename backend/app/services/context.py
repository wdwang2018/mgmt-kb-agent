"""组合根：装配适配器 → NodeDeps → 流水线（应用启动的唯一接线处）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..adapters.kb_json import JsonKnowledgeStore
from ..adapters.llm_factory import get_llm
from ..adapters.retrieval_memory import MemoryRetriever
from ..graph.nodes import NodeDeps
from .pipeline import CourseDesignPipeline

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class AppContext:
    store: JsonKnowledgeStore
    retriever: MemoryRetriever
    pipeline: CourseDesignPipeline
    knowledge_dir: Path
    output_dir: Path


def build_context(knowledge_dir: str | Path | None = None,
                  output_dir: str | Path | None = None,
                  llm_mode: str | None = None,
                  hitl_mode: str = "interrupt") -> AppContext:
    if llm_mode:
        os.environ["LLM_MODE"] = llm_mode
    k_dir = Path(knowledge_dir) if knowledge_dir else REPO_ROOT / "knowledge"
    o_dir = Path(output_dir) if output_dir else REPO_ROOT / "runs"

    store = JsonKnowledgeStore(k_dir, review_log_dir=o_dir / "reviews")
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
    return AppContext(store=store, retriever=retriever, pipeline=pipeline,
                      knowledge_dir=k_dir, output_dir=o_dir)
