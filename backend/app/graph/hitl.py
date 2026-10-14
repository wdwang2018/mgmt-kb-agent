"""HITL（人在回路）助手：统一两种人工审核挂起/恢复语义。

- interrupt 模式（LangGraph）：调用 langgraph.types.interrupt()，图在检查点处暂停，
  以 Command(resume=...) 恢复，节点重入时 interrupt() 返回人工决定；
- pause 模式（无 LangGraph 的自研 runner）：节点写 pending_interrupt 后提前返回，
  runner 停止推进，resume 时把 human_decision 注入状态重入同一节点。

节点代码对两种模式写法一致：
    decision, updates = human_gate(state, deps, "confirm_targets", payload)
    if decision is None:
        return updates            # 挂起
    # 拿到 decision，继续执行
"""

from __future__ import annotations

from typing import Any

from .state import CourseDesignState


def _interrupt_available() -> bool:
    try:
        from langgraph.types import interrupt  # noqa: F401
        return True
    except Exception:
        return False


def human_gate(state: CourseDesignState, deps: Any, kind: str, payload: dict[str, Any]):
    """返回 (decision | None, updates)。

    decision 为 None 表示需挂起等待人工；updates 为需要合并进状态的更新。
    """
    if getattr(deps, "hitl_mode", "interrupt") == "interrupt" and _interrupt_available():
        from langgraph.types import interrupt
        # 负载结构与 pause 模式保持一致：{"kind": ..., "payload": ...}
        decision = interrupt({"kind": kind, "payload": payload})
        return decision or {}, {}

    # pause 模式
    if state.get("human_decision") is not None:
        decision = state["human_decision"]
        return decision, {"human_decision": None, "pending_interrupt": None}
    return None, {"pending_interrupt": {"type": kind, "payload": payload}, "status": "awaiting_human"}
