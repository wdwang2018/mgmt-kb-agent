"""知识流水线服务（P1）：采集加工一次运行的编排入口。

与课程设计流水线平行：
- run(chunk)：一条语料进，五节点推进到「等待审核」或终态；
- resume(run_id, decision)：审核员批准/打回（Command(resume=...)）；
- run_auto(chunk)：无人值守跑通（Demo/评测），自动批准入库；
- 草稿与语料均落盘 runs/kb-runs/，审核台账进 knowledge/reviews/reviews.jsonl。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from ..domain.models import CorpusChunk, DraftQualityReport, UnitDraft
from ..graph.knowledge_nodes import KBNodeDeps, kb_initial_state
from ..graph.knowledge_workflow import build_kb_workflow

_TZ = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


class KBResult:
    def __init__(self, run_id: str, status: str, draft: Optional[UnitDraft] = None,
                 quality: Optional[DraftQualityReport] = None,
                 committed_unit_id: Optional[str] = None,
                 interrupt: Optional[dict] = None, detail: Optional[dict] = None):
        self.run_id = run_id
        self.status = status  # awaiting_human | committed | rejected | failed
        self.draft = draft
        self.quality = quality
        self.committed_unit_id = committed_unit_id
        self.interrupt = interrupt
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "draft": self.draft.model_dump() if self.draft else None,
            "quality": self.quality.model_dump() if self.quality else None,
            "committed_unit_id": self.committed_unit_id,
            "interrupt": self.interrupt,
            "detail": self.detail,
        }


class KnowledgePipeline:
    def __init__(self, deps: KBNodeDeps, runs_dir: str | Path, checkpointer=None):
        self.deps = deps
        self.graph = build_kb_workflow(deps, checkpointer)
        self.runs_dir = Path(runs_dir) / "kb-runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 内部
    def _config(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def _extract_interrupt(self, snapshot) -> Optional[dict]:
        if not snapshot or not snapshot.next:
            return None
        for task in getattr(snapshot, "tasks", []) or []:
            for it in getattr(task, "interrupts", []) or []:
                value = getattr(it, "value", None)
                if isinstance(value, dict):
                    return {"type": value.get("kind", "kb_draft_review"),
                            "payload": value.get("payload", {})}
        return {"type": "kb_draft_review", "payload": {}}

    def _persist(self, run_id: str, result: KBResult, values: dict) -> None:
        record = {
            "run_id": run_id,
            "status": result.status,
            "updated_at": _now(),
            "corpus": values["corpus"].model_dump() if values.get("corpus") else None,
            "draft": values["draft"].model_dump() if values.get("draft") else None,
            "quality": values["quality"].model_dump() if values.get("quality") else None,
            "committed_unit_id": values.get("committed_unit_id"),
            "decision": values.get("decision"),
        }
        (self.runs_dir / run_id).mkdir(parents=True, exist_ok=True)
        (self.runs_dir / run_id / "state.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _to_result(self, run_id: str) -> KBResult:
        snap = self.graph.get_state(self._config(run_id))
        values = dict(snap.values or {})
        interrupt = self._extract_interrupt(snap)
        draft = values.get("draft")
        quality = values.get("quality")
        status_field = values.get("status")
        committed = values.get("committed_unit_id")
        if committed:
            status = "committed"
        elif status_field == "rejected":
            status = "rejected"
        elif interrupt:
            status = "awaiting_human"
        else:
            status = status_field or "running"
        detail = {
            "status_field": status_field,
            "corpus_id": values["corpus"].id if values.get("corpus") else None,
            "quality_passed": quality.passed if quality else None,
            "failed_checks": quality.failed_codes() if quality else [],
        }
        return KBResult(run_id, status, draft, quality, committed, interrupt, detail)

    def _invoke(self, payload: Any, run_id: str) -> KBResult:
        """统一执行入口：异常收敛为 failed 落盘，不向上抛（在线模式兜底）。"""
        try:
            self.graph.invoke(payload, self._config(run_id))
            result = self._to_result(run_id)
            self._persist(run_id, result, dict(self.graph.get_state(self._config(run_id)).values or {}))
            return result
        except Exception as exc:
            result = KBResult(run_id, "failed", detail={"error": f"{type(exc).__name__}: {exc}"[:500]})
            try:
                (self.runs_dir / run_id).mkdir(parents=True, exist_ok=True)
                (self.runs_dir / run_id / "state.json").write_text(json.dumps({
                    "run_id": run_id, "status": "failed", "updated_at": _now(),
                    "error": result.detail["error"],
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            return result

    # ------------------------------------------------------------------ 公共
    def run(self, chunk: CorpusChunk) -> KBResult:
        run_id = f"kb-{datetime.now(_TZ).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        return self._invoke(kb_initial_state(run_id, chunk), run_id)

    def resume(self, run_id: str, decision: dict) -> KBResult:
        from langgraph.types import Command
        result = self._invoke(Command(resume=decision), run_id)
        self.deps.store.log_review(run_id, "kb_draft_review", decision)
        return result

    def run_auto(self, chunk: CorpusChunk, decision: Optional[dict] = None) -> KBResult:
        """无人值守跑通：质检过后自动按 decision（默认批准）恢复。"""
        decision = decision or {"action": "approve", "reviewer": "auto", "note": "自动批准"}
        result = self.run(chunk)
        if result.status == "awaiting_human":
            result = self.resume(result.run_id, decision)
        return result

    def get(self, run_id: str) -> Optional[KBResult]:
        state_file = self.runs_dir / run_id / "state.json"
        if not state_file.exists():
            return None
        record = json.loads(state_file.read_text(encoding="utf-8"))
        draft = UnitDraft.model_validate(record["draft"]) if record.get("draft") else None
        quality = DraftQualityReport.model_validate(record["quality"]) if record.get("quality") else None
        return KBResult(record["run_id"], record["status"], draft, quality,
                        record.get("committed_unit_id"),
                        detail={"updated_at": record.get("updated_at"),
                                "failed_checks": quality.failed_codes() if quality else []})

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """审核台草稿列表：扫描 kb-runs 目录，按时间倒序。"""
        rows: list[dict[str, Any]] = []
        for d in sorted(self.runs_dir.glob("kb-*"), reverse=True):
            st_file = d / "state.json"
            if not st_file.exists():
                continue
            try:
                st = json.loads(st_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            draft = st.get("draft") or {}
            unit = draft.get("unit") or {}
            rows.append({
                "run_id": st.get("run_id", d.name),
                "status": st.get("status"),
                "updated_at": st.get("updated_at"),
                "draft_id": draft.get("id"),
                "title": unit.get("title"),
                "domains": unit.get("domains") or [],
                "dilemmas": unit.get("dilemmas") or [],
                "source": (f"{unit.get('source', {}).get('author', '')}《{unit.get('source', {}).get('book', '')}》"
                           f"{unit.get('source', {}).get('locator', '')}"),
                "quality_passed": (st.get("quality") or {}).get("passed"),
                "committed_unit_id": st.get("committed_unit_id"),
            })
            if len(rows) >= limit:
                break
        return rows
