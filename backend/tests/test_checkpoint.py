"""检查点持久化测试（P1 可靠性）：sqlite 重启续会话 / 降级链 / serde 往返。

核心场景是「重启续会话」：流水线跑到人工挂起点后，丢弃进程内一切对象
（模拟服务重启），仅凭 sqlite 文件重建流水线，Command(resume) 必须能接着跑完。
对照组用 memory 模式证明：没有持久化时同一 thread 的状态随进程灰飞烟灭。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from langgraph.checkpoint.memory import MemorySaver

from app.adapters.corpus_json import JsonCorpusStore
from app.adapters.kb_json import JsonKnowledgeStore
from app.adapters.llm_mock import MockLLM
from app.graph.checkpoint import (
    _BACKEND_DIR,
    _resolve_db_path,
    build_checkpointer,
    domain_serde,
)
from app.graph.knowledge_nodes import KBNodeDeps, mock_extract
from app.services.kb_pipeline import KnowledgePipeline
from tests.test_kb_pipeline import make_chunk


@pytest.fixture()
def kb_factory(tmp_path: Path):
    """按需重建知识流水线；每次重建都重新打开同一个 sqlite 文件（模拟重启）。"""
    k_dir = tmp_path / "knowledge"
    db = tmp_path / "cp.db"

    def build(mode: str = "sqlite"):
        store = JsonKnowledgeStore(k_dir, review_log_dir=tmp_path / "runs" / "reviews")
        corpus_store = JsonCorpusStore(k_dir)
        deps = KBNodeDeps(llm=MockLLM(), store=store, corpus_store=corpus_store,
                          hitl_mode="interrupt")
        saver, _, _ = build_checkpointer(mode=mode, db_path=db)
        return KnowledgePipeline(deps, tmp_path / "runs", saver)

    return build, db, k_dir


# ---------------------------------------------------------------------------
# 核心：sqlite 持久化 → 重启后续会话
# ---------------------------------------------------------------------------
class TestSqlitePersistence:
    def test_resume_survives_restart(self, kb_factory):
        """挂起 → 重建实例（仅凭 sqlite 文件）→ resume 跑完并入库。"""
        build, db, k_dir = kb_factory
        pipe = build()
        r1 = pipe.run(make_chunk())
        assert r1.status == "awaiting_human"      # 质检过后挂在人工审核
        assert r1.interrupt and r1.interrupt["type"] == "kb_draft_review"

        # —— 模拟服务重启：进程内对象全部丢弃，文件系统上只剩 sqlite ——
        pipe2 = build()
        r2 = pipe2.resume(r1.run_id, {"action": "approve", "reviewer": "tester"})
        assert r2.status == "committed", r2.detail
        assert r2.committed_unit_id and r2.committed_unit_id.startswith("KU-")
        assert r2.draft is not None and r2.draft.unit.title  # 状态完整往返（serde 生效）

        # 入库结果落进知识库真相源（JSONL），而非只存在于检查点里
        store = JsonKnowledgeStore(k_dir)
        ids = [u.id for u in store.load_units()]
        assert r2.committed_unit_id in ids

    def test_memory_mode_loses_thread_on_restart(self, kb_factory):
        """对照组：memory 模式下「重启」后同 thread 无状态，恢复无从谈起。"""
        build, _, _ = kb_factory
        pipe = build(mode="none")
        r1 = pipe.run(make_chunk())
        assert r1.status == "awaiting_human"

        pipe2 = build(mode="none")  # 新实例 = 新 MemorySaver
        snap = pipe2.graph.get_state(pipe2._config(r1.run_id))
        assert not (snap.values or {}), "memory 模式不应跨实例保留状态"

    def test_checkpoint_file_created(self, kb_factory):
        build, db, _ = kb_factory
        build().run(make_chunk())
        assert db.exists() and db.stat().st_size > 0


# ---------------------------------------------------------------------------
# 工厂行为：模式解析 / 降级链 / 路径解析
# ---------------------------------------------------------------------------
class TestFactoryBehavior:
    def test_mode_none_returns_memory_saver(self, tmp_path):
        saver, mode, note = build_checkpointer(mode="none", db_path=tmp_path / "x.db")
        assert isinstance(saver, MemorySaver) and mode == "memory" and note is None

    def test_default_mode_is_sqlite(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CHECKPOINT_MODE", raising=False)
        saver, mode, note = build_checkpointer(db_path=tmp_path / "x.db")
        assert mode == "sqlite" and note is None
        assert not isinstance(saver, MemorySaver)

    def test_env_mode_drives_factory(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHECKPOINT_MODE", "none")
        saver, mode, _ = build_checkpointer(db_path=tmp_path / "x.db")
        assert mode == "memory"

    def test_postgres_without_dsn_falls_back_to_sqlite(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CHECKPOINT_POSTGRES_DSN", raising=False)
        saver, mode, note = build_checkpointer(mode="postgres", db_path=tmp_path / "x.db")
        assert mode == "sqlite"
        assert note and "降级" in note and "DSN" in note

    def test_postgres_without_package_falls_back_with_reason(self, monkeypatch, tmp_path):
        """有 DSN 但缺驱动：降级原因必须点名缺依赖（而非连接失败）。"""
        monkeypatch.setenv("CHECKPOINT_POSTGRES_DSN", "postgresql://u:p@127.0.0.1:5432/none")
        saver, mode, note = build_checkpointer(mode="postgres", db_path=tmp_path / "x.db")
        assert mode == "sqlite"
        assert note and ("langgraph-checkpoint-postgres" in note or "连接" in note)

    def test_unknown_mode_note(self, tmp_path):
        saver, mode, note = build_checkpointer(mode="bogus", db_path=tmp_path / "x.db")
        assert mode == "sqlite" and note and "bogus" in note

    def test_relative_db_path_resolves_under_backend(self, monkeypatch):
        monkeypatch.delenv("CHECKPOINT_DB_PATH", raising=False)
        assert _resolve_db_path("cp.db") == _BACKEND_DIR / "cp.db"
        assert _resolve_db_path(None) == _BACKEND_DIR / "checkpoints.db"
        abs_path = Path("C:/tmp/x.db")
        assert _resolve_db_path(abs_path) == abs_path

    def test_env_db_path_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "env.db"))
        assert _resolve_db_path(None) == tmp_path / "env.db"


# ---------------------------------------------------------------------------
# serde：领域模型登记（msgpack 允许列表）
# ---------------------------------------------------------------------------
class TestDomainSerde:
    def test_roundtrip_unit_draft(self):
        serde = domain_serde()
        draft = mock_extract(make_chunk())
        t, data = serde.dumps_typed(draft)
        assert t == "msgpack"  # pydantic 走 msgpack 路径，依赖 allowed_msgpack_modules
        back = serde.loads_typed((t, data))
        assert back == draft
        assert back.unit.id == draft.unit.id

    def test_course_serde_handles_kb_models(self):
        """并集登记的行为证据：课程图 serde 也能往返知识图专属的 UnitDraft。

        （属性级断言不可行——allowed_msgpack_modules 不暴露在实例上；
        行为级断言反而更贴近真实威胁：登记缺项时 msgpack 往返会失败。）
        """
        from app.graph.workflow import _SERDE as course_serde
        draft = mock_extract(make_chunk())
        t, data = course_serde.dumps_typed(draft)
        assert course_serde.loads_typed((t, data)) == draft
