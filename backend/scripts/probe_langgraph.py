"""探针：验证 langgraph 1.x 的 interrupt / Command(resume) 行为与 MemorySaver 检查点语义。"""
import sys
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command
from langgraph.errors import GraphInterrupt


class S(TypedDict, total=False):
    x: int
    log: list


def node_a(s: S):
    return {"x": 1, "log": s.get("log", []) + ["a"]}


def node_gate(s: S):
    decision = interrupt({"kind": "confirm", "payload": {"x": s["x"]}})
    return {"log": s["log"] + [f"gate:{decision}"]}


def node_b(s: S):
    return {"log": s.get("log", []) + ["b"]}


def build():
    g = StateGraph(S)
    g.add_node("a", node_a)
    g.add_node("gate", node_gate)
    g.add_node("b", node_b)
    g.add_edge(START, "a")
    g.add_edge("a", "gate")
    g.add_edge("gate", "b")
    g.add_edge("b", END)
    return g.compile(checkpointer=MemorySaver())


graph = build()
cfg = {"configurable": {"thread_id": "probe-1"}}

# 第一次调用：应在 gate 处暂停
try:
    result = graph.invoke({"x": 0, "log": []}, cfg)
    print("INVOKE_RETURNED:", result)
except GraphInterrupt as e:
    print("GRAPH_INTERRUPT_RAISED:", type(e).__name__)

snap = graph.get_state(cfg)
print("NEXT:", snap.next)
print("TASKS:", [(t.name, getattr(t, "interrupts", None)) for t in snap.tasks])
print("VALUES_LOG:", snap.values.get("log"))

# 恢复
try:
    result2 = graph.invoke(Command(resume={"action": "approve"}), cfg)
    print("RESUME_RETURNED:", result2)
except GraphInterrupt as e:
    print("RESUME_INTERRUPT_RAISED:", type(e).__name__)

snap2 = graph.get_state(cfg)
print("FINAL_NEXT:", snap2.next)
print("FINAL_LOG:", snap2.values.get("log"))
print("PROBE_DONE")
