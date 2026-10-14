"""课程设计流水线服务：运行 / 恢复 / 查询。

- 运行：LangGraph 状态机推进到完成或人工挂起点；
- 恢复：Command(resume=decision) 注入人工决定（能力目标确认 / 门禁人工签发）；
- 自动批准模式：Demo 与评测用，模拟「默认同意」的评审人，保证无人值守可跑通。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from ..domain.models import CoursePackage, TrainingNeed
from ..graph.nodes import NodeDeps
from ..graph.state import initial_state
from ..graph.workflow import build_workflow

_TZ = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


class RunResult:
    def __init__(self, run_id: str, status: str, interrupt: Optional[dict] = None,
                 package: Optional[CoursePackage] = None, detail: Optional[dict] = None):
        self.run_id = run_id
        self.status = status  # completed | awaiting_human | failed
        self.interrupt = interrupt
        self.package = package
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "interrupt": self.interrupt,
            "package_markdown": self.package and None or None,
            "detail": self.detail,
        }


class CourseDesignPipeline:
    def __init__(self, deps: NodeDeps, checkpointer=None):
        self.deps = deps
        self.graph = build_workflow(deps, checkpointer)
        self.runs_dir = Path(deps.output_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 内部
    def _config(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def _extract_interrupt(self, snapshot) -> Optional[dict]:
        """从检查点快照提取待处理的人工挂起点（快照 values 不含 __interrupt__，需读 next/tasks）。"""
        if not snapshot or not snapshot.next:
            return None
        for task in getattr(snapshot, "tasks", []) or []:
            for it in getattr(task, "interrupts", []) or []:
                value = getattr(it, "value", None)
                if isinstance(value, dict):
                    return {"type": value.get("kind", "human_review"),
                            "payload": value.get("payload", {})}
        return {"type": "human_review", "payload": {}}

    def _persist(self, run_id: str, result: RunResult, snapshot_values: dict) -> None:
        gates = snapshot_values.get("gate_reports") or []
        record = {
            "run_id": run_id,
            "status": result.status,
            "updated_at": _now(),
            "interrupt": result.interrupt,
            "gates": [g.model_dump() if hasattr(g, "model_dump") else g for g in gates],
            "modules": [m.title for m in snapshot_values.get("modules", [])] if snapshot_values.get("modules") else [],
        }
        (self.runs_dir / run_id).mkdir(parents=True, exist_ok=True)
        (self.runs_dir / run_id / "state.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _to_result(self, run_id: str) -> RunResult:
        snap = self.graph.get_state(self._config(run_id))
        values = dict(snap.values or {})
        interrupt = self._extract_interrupt(snap)
        package = values.get("package")
        status = "completed" if package is not None else ("awaiting_human" if interrupt else "running")
        detail = {
            "status_field": values.get("status"),
            "retries": values.get("retries"),
            "gates": [g.model_dump() if hasattr(g, "model_dump") else g for g in values.get("gate_reports", [])],
            "modules_count": len(values.get("modules", [])),
            "activities_count": len(package.activities) if package else 0,
            "assessments_count": len(package.assessments) if package else 0,
            "citations_count": len(package.citations) if package else 0,
            "human_reviewed": bool(package.human_reviewed) if package else False,
        }
        return RunResult(run_id, status, interrupt, package, detail)

    # ------------------------------------------------------------------ 公共
    def _invoke(self, payload: Any, run_id: str) -> RunResult:
        """统一执行入口：任何节点异常都收敛为 failed 状态并落盘，不向上抛。"""
        try:
            self.graph.invoke(payload, self._config(run_id))
            result = self._to_result(run_id)
            self._persist(run_id, result, dict(self.graph.get_state(self._config(run_id)).values or {}))
            return result
        except Exception as exc:  # 在线模型 shape 抖动/网络异常的最终兜底
            result = RunResult(run_id, "failed",
                               detail={"error": f"{type(exc).__name__}: {exc}"[:500]})
            try:
                (self.runs_dir / run_id).mkdir(parents=True, exist_ok=True)
                (self.runs_dir / run_id / "state.json").write_text(json.dumps({
                    "run_id": run_id, "status": "failed", "updated_at": _now(),
                    "gates": [], "modules": [],
                    "error": result.detail["error"],
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass  # 落盘失败不影响调用方拿到 failed 结果
            return result

    def run(self, need: TrainingNeed) -> RunResult:
        run_id = f"run-{datetime.now(_TZ).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        return self._invoke(initial_state(run_id, need), run_id)

    def resume(self, run_id: str, decision: dict) -> RunResult:
        from langgraph.types import Command
        result = self._invoke(Command(resume=decision), run_id)
        self.deps.store.log_review(run_id, "human_decision", decision)
        return result

    def run_auto(self, need: TrainingNeed, decision: Optional[dict] = None,
                 max_rounds: int = 6) -> RunResult:
        """无人值守跑通：遇到人工挂起点自动按 decision（默认同意）恢复。"""
        decision = decision or {"action": "approve"}
        result = self.run(need)
        rounds = 0
        while result.status == "awaiting_human" and rounds < max_rounds:
            result = self.resume(result.run_id, decision)
            rounds += 1
        return result

    def get(self, run_id: str) -> Optional[RunResult]:
        state_file = self.runs_dir / run_id / "state.json"
        if not state_file.exists():
            return None
        record = json.loads(state_file.read_text(encoding="utf-8"))
        package = None
        pkg_file = self.runs_dir / run_id / "package.json"
        if pkg_file.exists():
            package = CoursePackage.model_validate_json(pkg_file.read_text(encoding="utf-8"))
        # detail 与 _to_result 保持同构：Web 工作台刷新后统计卡片仍有计数可取
        detail = {
            "status_field": None,
            "retries": None,
            "gates": record.get("gates") or [],
            "modules_count": len(record.get("modules") or []),
            "activities_count": len(package.activities) if package else 0,
            "assessments_count": len(package.assessments) if package else 0,
            "citations_count": len(package.citations) if package else 0,
            "human_reviewed": bool(package.human_reviewed) if package else False,
        }
        return RunResult(record["run_id"], record["status"], record.get("interrupt"), package, detail)
