"""组合根：装配适配器 → NodeDeps → 流水线（应用启动的唯一接线处）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..adapters.corpus_json import JsonCorpusStore
from ..adapters.graph_factory import build_graph_store, build_metadata_store
from ..adapters.kb_json import JsonKnowledgeStore
from ..adapters.llm_factory import get_llm
from ..adapters.retriever_factory import get_retriever
from ..graph.checkpoint import build_checkpointer
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
    graph: object | None = None          # GraphStore | None（GRAPH_MODE=none 时为 None）
    graph_mode: str = "none"
    graph_note: str | None = None
    store_mode: str = "json"
    store_note: str | None = None
    checkpoint_mode: str = "sqlite"     # sqlite / postgres / memory（重启丢会话）
    checkpoint_note: str | None = None  # 降级原因（正常为 None）

    def reindex(self) -> None:
        """知识流水线入库后重建检索索引与知识图谱（适配器原地刷新，引用不变）。

        memory 模式：全量重建 BM25 统计量；
        milvus 模式：upsert 向量并删除已移除项，保证索引与 JSONL 真相源一致；
        kuzu/neo4j 模式：MERGE 同步单元节点与关系边（幂等，重复调用不写脏）。
        """
        units = self.store.load_units()
        self.retriever.reload(units)
        if self.graph is not None:
            self.graph.rebuild(units)


def build_context(knowledge_dir: str | Path | None = None,
                  output_dir: str | Path | None = None,
                  llm_mode: str | None = None,
                  hitl_mode: str = "interrupt") -> AppContext:
    _load_dotenv()
    if llm_mode:
        os.environ["LLM_MODE"] = llm_mode
    k_dir = Path(knowledge_dir) if knowledge_dir else REPO_ROOT / "knowledge"
    o_dir = Path(output_dir) if output_dir else REPO_ROOT / "runs"

    # 元数据存储：STORE_MODE=json（默认，JSONL）/ pg（连不上自动降级）
    store, store_mode, store_note = build_metadata_store(
        k_dir, review_log_dir=o_dir / "reviews")
    if not hasattr(store, "log_review"):  # 防御：工厂返回类型不符时退回 JSON
        store, store_mode, store_note = JsonKnowledgeStore(k_dir), "json", "store 类型异常，已退回 JSONL"
    corpus_store = JsonCorpusStore(k_dir)
    units = store.load_units()
    # 检索模式由 VECTOR_MODE 决定；milvus 不可用时工厂自动降级为 memory 并带回原因
    retriever, vector_mode, vector_note = get_retriever(
        units, uri=str(k_dir / "milvus_kb.db"))
    # 图存储：GRAPH_MODE=none（默认）/ kuzu（嵌入式）/ neo4j（服务端）
    graph, graph_mode, graph_note = build_graph_store(units)
    deps = NodeDeps(
        llm=get_llm(),
        retriever=retriever,
        store=store,
        cases=store.load_cases(),
        output_dir=o_dir,
        hitl_mode=hitl_mode,
    )
    # 检查点存储：CHECKPOINT_MODE=sqlite（默认，嵌入式文件，重启不丢会话）
    # / postgres（需 DSN，缺失自动降级）/ none（MemorySaver，仅测试用）
    checkpointer, cp_mode, cp_note = build_checkpointer()
    pipeline = CourseDesignPipeline(deps, checkpointer)
    kb_deps = KBNodeDeps(
        llm=deps.llm,
        store=store,
        corpus_store=corpus_store,
        hitl_mode=hitl_mode,
        auto_approve=False,  # Web 审核台走真实人工挂起；Demo/测试用 run_auto
        on_commit=None,      # 占位，装配完成后绑定（闭包引用待创建的 ctx）
    )
    kb_pipeline = KnowledgePipeline(kb_deps, o_dir, checkpointer)
    ctx = AppContext(store=store, corpus_store=corpus_store, retriever=retriever,
                     pipeline=pipeline, kb_pipeline=kb_pipeline,
                     knowledge_dir=k_dir, output_dir=o_dir,
                     vector_mode=vector_mode, vector_note=vector_note,
                     graph=graph, graph_mode=graph_mode, graph_note=graph_note,
                     store_mode=store_mode, store_note=store_note,
                     checkpoint_mode=cp_mode, checkpoint_note=cp_note)
    kb_deps.on_commit = lambda unit_id: ctx.reindex()  # 入库即重索引 + 同步建图
    return ctx
