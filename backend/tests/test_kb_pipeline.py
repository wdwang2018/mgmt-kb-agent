"""P1 知识流水线测试：Mock 全链路、质检打回、唯一性、编号分配、API 闭环。

全部使用 tmp_path 隔离知识库与运行产物，不触碰仓库真实 knowledge/ 与 runs/。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.corpus_json import JsonCorpusStore
from app.adapters.kb_json import JsonKnowledgeStore
from app.adapters.llm_mock import MockLLM
from app.domain.models import CorpusChunk
from app.graph.knowledge_nodes import (
    KBNodeDeps,
    mock_extract,
    next_unit_id,
    online_extract,
    validate_draft,
)
from app.graph.knowledge_workflow import build_kb_workflow
from app.services.kb_pipeline import KnowledgePipeline

TEXT_STRAT = (
    "战略共识缺失的团队各唱各调，根源在于战略没有完成解码。"
    "战略解码的核心动作是把公司级战略翻译为部门的关键举措与衡量指标。"
    "共识不是一次宣讲就能达成，而是在目标对齐会议中通过质询与修正逐步形成。"
    "缺少解码环节的组织，战略执行必然断裂，策略停在 PPT 层面无法落地。"
)

GOOD_TEXT = (
    "新经理晋升后仍然埋头做事，以个人贡献者的标准要求自己，导致下属得不到成长。"
    "管理效能包含三个转变：从做事到通过他人拿结果、从被管理到管理、从专业深度转向关系广度。"
    "晋升后前 90 天最关键的动作是重新定义什么算我的工作成果，管理者价值体现在团队产能与人才成长。"
)


def make_chunk(**kw) -> CorpusChunk:
    base = dict(id="CC-0000", book="成为管理者", author="Linda A. Hill", year=2003,
                locator="第1章", copyright_status="要点化", text=GOOD_TEXT)
    base.update(kw)
    return CorpusChunk(**base)


@pytest.fixture()
def kb(tmp_path: Path):
    """全隔离环境：空知识库（编号从 KU-LEAD-001 起算，不受真实仓库状态影响）。"""
    k_dir = tmp_path / "knowledge"
    store = JsonKnowledgeStore(k_dir, review_log_dir=tmp_path / "runs" / "reviews")
    corpus_store = JsonCorpusStore(k_dir)
    deps = KBNodeDeps(llm=MockLLM(), store=store, corpus_store=corpus_store, hitl_mode="interrupt")
    return KnowledgePipeline(deps, tmp_path / "runs"), store, corpus_store


# ---------------------------------------------------------------------------
# 规则抽取与质检（纯函数）
# ---------------------------------------------------------------------------
class TestExtractAndValidate:
    def test_mock_extract_hits_domain_and_dilemma(self):
        d = mock_extract(make_chunk())
        assert "TEAM" in d.unit.domains
        assert "D01" in d.unit.dilemmas
        assert len(d.unit.key_points) >= 2
        assert d.unit.source.locator == "第1章"
        assert d.unit.id == "KU-LEAD-000"  # 占位，入库才分配正式编号

    def test_short_text_fails_k3(self):
        d = mock_extract(make_chunk(text="太短了。不够。"))
        report = validate_draft(d, [], d.corpus_ids and "")
        codes = report.failed_codes()
        assert "K3" in codes  # 要点不足 2 条
        assert not report.passed

    def test_duplicate_title_fails_k5(self):
        d = mock_extract(make_chunk())
        # 用与现有单元完全相同的标题触发 K5
        existing = store_units()
        d.unit.title = existing[0].title
        report = validate_draft(d, existing, GOOD_TEXT)
        assert "K5" in report.failed_codes()

    def test_illegal_codes_fail_k2(self):
        d = mock_extract(make_chunk())
        d.unit.domains = ["NOPE"]
        report = validate_draft(d, [], GOOD_TEXT)
        assert "K2" in report.failed_codes()

    def test_empty_source_fails_k1(self):
        d = mock_extract(make_chunk())
        d.unit.source.locator = "  "
        report = validate_draft(d, [], GOOD_TEXT)
        assert "K1" in report.failed_codes()

    def test_next_unit_id_allocates_and_skips(self):
        existing = store_units()
        lead_max = max(int(u.id.split("-")[2]) for u in existing if u.id.startswith("KU-LEAD-"))
        strat_max = max(int(u.id.split("-")[2]) for u in existing if u.id.startswith("KU-STRAT-"))
        assert next_unit_id(existing, ["TEAM"]) == f"KU-LEAD-{lead_max + 1:03d}"
        assert next_unit_id(existing, ["STRAT", "TEAM"]) == f"KU-STRAT-{strat_max + 1:03d}"
        bumped = existing + [type("U", (), {"id": f"KU-LEAD-{lead_max + 1:03d}"})()]
        assert next_unit_id(bumped, ["TEAM"]) == f"KU-LEAD-{lead_max + 2:03d}"


def store_units():
    from app.domain.models import KnowledgeUnit
    repo_units = Path(__file__).resolve().parents[2] / "knowledge" / "units"
    units = []
    for f in repo_units.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                units.append(KnowledgeUnit.model_validate_json(line))
    return units


class TestOnlineExtractFallback:
    def test_dirty_shape_falls_back_to_mock(self):
        class DirtyLLM:
            name = "glm-5"
            def complete_json(self, purpose, system, user, fallback):
                return {"title": {"id": 1, "text": "对象标题"}, "type": "理论",
                        "domains": "TEAM", "dilemmas": None, "key_points": [None, 42]}
        d = online_extract(DirtyLLM(), make_chunk())
        # 脏 shape（域是字符串/要点含非字符串）→ 回退规则抽取，产出合规草稿
        assert isinstance(d.unit.domains, list) and d.unit.domains
        assert len(d.unit.key_points) >= 2

    def test_good_shape_used_directly(self):
        class GoodLLM:
            name = "glm-5"
            def complete_json(self, purpose, system, user, fallback):
                return {"title": " Outsight 原则", "type": "理论", "domains": ["TEAM", "COMM"],
                        "dilemmas": ["D01", "D02"], "evidence_level": "A",
                        "key_points": ["先行动后思考，外在行动重塑身份", "转型需要工作内容关系网络自我认知三变"],
                        "misconceptions": [{"claim": "先想清楚再行动", "correction": "过度内省强固化旧身份"}],
                        "applicable_context": ["转型期管理者"]}
        d = online_extract(GoodLLM(), make_chunk())
        assert d.unit.title == "Outsight 原则"  # strip 生效
        assert d.unit.evidence_level.value == "A"
        assert len(d.unit.misconceptions) == 1


# ---------------------------------------------------------------------------
# 流水线全链路（LangGraph）
# ---------------------------------------------------------------------------
class TestPipelineFlow:
    def test_full_flow_auto_commit(self, kb):
        pipe, store, corpus_store = kb
        result = pipe.run_auto(make_chunk())
        assert result.status == "committed", result.detail
        assert result.committed_unit_id == "KU-LEAD-001"
        # 语料落 L1 且标记 extracted
        chunks = corpus_store.load_chunks()
        assert len(chunks) == 1 and chunks[0].status == "extracted"
        assert chunks[0].id == "CC-0001"
        # 正式单元入库：JSONL 追加
        units = store.load_units()
        assert any(u.id == "KU-LEAD-001" for u in units)
        # 台账
        reviews = (store.review_log_dir / "reviews.jsonl").read_text(encoding="utf-8")
        assert "kb_commit" in reviews and "kb_draft_review" in reviews

    def test_interrupt_then_approve(self, kb):
        pipe, store, _ = kb
        result = pipe.run(make_chunk())
        assert result.status == "awaiting_human"
        assert result.interrupt and result.interrupt["type"] == "kb_draft_review"
        assert result.interrupt["payload"]["title"]
        assert len(result.interrupt["payload"]["quality"]) == 5
        # 批准 → 入库
        result2 = pipe.resume(result.run_id, {"action": "approve", "reviewer": "tester"})
        assert result2.status == "committed"
        assert result2.committed_unit_id == "KU-LEAD-001"

    def test_interrupt_then_reject(self, kb):
        pipe, store, _ = kb
        result = pipe.run(make_chunk())
        assert result.status == "awaiting_human"
        result2 = pipe.resume(result.run_id, {"action": "reject", "note": "要点需补充证据", "reviewer": "tester"})
        assert result2.status == "rejected"
        assert result2.committed_unit_id is None
        assert not any(u.id == "KU-LEAD-013" for u in store.load_units())

    def test_quality_reject_short_text(self, kb):
        pipe, store, _ = kb
        result = pipe.run(make_chunk(text="太短了。不够。"))
        assert result.status == "rejected"
        assert "K3" in (result.detail.get("failed_checks") or [])

    def test_duplicate_corpus_id_not_reused(self, kb):
        pipe, _, corpus_store = kb
        pipe.run_auto(make_chunk())
        pipe.run_auto(make_chunk(text=TEXT_STRAT, book="战略解码", author="吕守升", locator="第2章"))
        ids = [c.id for c in corpus_store.load_chunks()]
        assert ids == ["CC-0001", "CC-0002"]  # 编号顺延，不重复

    def test_get_and_list_runs(self, kb):
        pipe, _, _ = kb
        r1 = pipe.run_auto(make_chunk())
        r2 = pipe.run_auto(make_chunk(text=TEXT_STRAT, book="战略解码", author="吕守升", locator="第2章"))
        got = pipe.get(r1.run_id)
        assert got is not None and got.status == "committed"
        rows = pipe.list_runs()
        assert {r["run_id"] for r in rows} == {r1.run_id, r2.run_id}
        assert all(r["committed_unit_id"] for r in rows)
        # 按时间倒序（同秒创建时以 updated_at+run_id 稳定排序，不断言绝对顺序）
        keys = [(r["updated_at"], r["run_id"]) for r in rows]
        assert keys == sorted(keys, reverse=True)

    def test_failed_status_persisted(self, kb, monkeypatch):
        pipe, _, _ = kb
        # 人为制造节点异常：validate 抛错 → 全流程收敛 failed 落盘
        import app.graph.knowledge_nodes as kn
        def boom(state, deps):
            raise RuntimeError("模拟节点异常")
        monkeypatch.setitem(kn.KB_NODES, "validate_draft", boom)
        result = pipe.run(make_chunk())
        assert result.status == "failed"
        assert "模拟节点异常" in (result.detail.get("error") or "")
        assert pipe.get(result.run_id).status == "failed"

    def test_reindex_after_commit(self, kb):
        """入库后 on_commit 触发重索引：检索器即时能搜到新单元。"""
        pipe, store, _ = kb
        seen: list[str] = []
        pipe.deps.on_commit = lambda unit_id: seen.append(unit_id)
        result = pipe.run_auto(make_chunk())
        assert result.status == "committed"
        assert seen == [result.committed_unit_id]


# ---------------------------------------------------------------------------
# API 闭环（TestClient）
# ---------------------------------------------------------------------------
class TestKbApi:
    def test_ingest_review_commit_flow(self, tmp_path: Path):
        from fastapi.testclient import TestClient
        from app.api.main import app
        from app.services.context import build_context

        # 隔离环境注入
        import app.api.main as main
        main.ctx = build_context(knowledge_dir=tmp_path / "knowledge", output_dir=tmp_path / "runs")
        c = TestClient(app)
        try:
            r = c.post("/api/v1/kb/ingest", json={
                "book": "成为管理者", "author": "Linda A. Hill", "year": 2003,
                "locator": "第1章", "copyright_status": "要点化", "text": GOOD_TEXT})
            assert r.status_code == 200
            j = r.json()
            assert j["status"] == "awaiting_human", j
            run_id = j["run_id"]

            # 待审核列表出现该草稿
            runs = c.get("/api/v1/kb/runs").json()
            assert any(x["run_id"] == run_id for x in runs)

            # 语料层
            corpus = c.get("/api/v1/kb/corpus").json()
            assert len(corpus) == 1 and corpus[0]["status"] == "raw"

            # 批准入库
            r2 = c.post(f"/api/v1/kb/runs/{run_id}/review",
                        json={"action": "approve", "note": "ok", "reviewer": "tester"})
            assert r2.status_code == 200
            j2 = r2.json()
            assert j2["status"] == "committed" and j2["committed_unit_id"].startswith("KU-")

            # 入库后语料标记 extracted
            corpus2 = c.get("/api/v1/kb/corpus").json()
            assert corpus2[0]["status"] == "extracted"

            # 非法 action 拒绝
            r3 = c.post(f"/api/v1/kb/runs/{run_id}/review", json={"action": "maybe"})
            assert r3.status_code == 422

            # taxonomy
            tax = c.get("/api/v1/kb/taxonomy").json()
            assert len(tax["domains"]) == 6 and len(tax["dilemmas"]) == 24

            # 不存在的 run
            assert c.get("/api/v1/kb/runs/nope").status_code == 404
        finally:
            main.ctx = None
