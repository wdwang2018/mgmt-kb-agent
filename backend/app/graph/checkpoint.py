"""LangGraph 检查点存储工厂：嵌入式 sqlite 为默认，postgres 预留，none 退回内存。

与检索层（memory/milvus/hybrid）、图谱层（none/kuzu/neo4j）同一设计哲学：
- 默认路径按 __file__ 相对定位（backend/ 下），与 cwd 无关——评测脚本从仓库
  根目录跑与服务从 backend/ 跑，落到同一个文件，不会裂出两个库；
- 重依赖（postgres）lazy import + 工厂自动降级，当前模式与降级原因经
  AppContext.checkpoint_mode/checkpoint_note 暴露到 /healthz；
- 返回 (checkpointer, mode, note) 三元组，与 get_retriever/build_graph_store
  的工厂签名保持一致。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

try:  # 默认存储的载体；未安装时工厂自动退回内存，不阻断启动
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError:  # pragma: no cover - 依赖缺失的防御分支
    SqliteSaver = None  # type: ignore[assignment]

from ..domain.models import (
    Activity,
    AssessmentItem,
    Bucket,
    CorpusChunk,
    CoursePackage,
    DraftQualityReport,
    EvidenceLevel,
    GapAnalysis,
    GateReport,
    KnowledgeUnit,
    KUType,
    LearningObjective,
    ModulePlan,
    QualityCheck,
    RelationType,
    TheoryPoint,
    TrainingNeed,
    UnitDraft,
)

_DEFAULT_DB = "checkpoints.db"
_DEFAULT_MODE = "sqlite"
_BACKEND_DIR = Path(__file__).resolve().parents[1]


def domain_serde() -> JsonPlusSerializer:
    """课程/知识两张图状态机的统一 msgpack serde（允许列表取两张图登记的并集）。

    状态中携带 pydantic 领域模型，须在 allowed_msgpack_modules 中显式登记，
    否则检查点往返时告警（未来版本将直接阻断）。并集是安全超集：登记不改变
    序列化行为，只是放行白名单。
    """
    return JsonPlusSerializer(allowed_msgpack_modules=[
        TrainingNeed, GapAnalysis, LearningObjective, ModulePlan,
        TheoryPoint, Activity, AssessmentItem, GateReport, CoursePackage, Bucket,
        KnowledgeUnit, CorpusChunk, UnitDraft, DraftQualityReport, QualityCheck,
        EvidenceLevel, KUType, RelationType,
    ])


def _resolve_db_path(db_path: str | Path | None) -> Path:
    """相对路径一律解析到 backend/ 下（与 graph_kb.kuzu 同目录约定）。"""
    path = Path(db_path or os.environ.get("CHECKPOINT_DB_PATH") or _DEFAULT_DB)
    if not path.is_absolute():
        path = _BACKEND_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _build_postgres(serde) -> tuple[object | None, str | None]:
    """尝试构建 PostgresSaver；任一步失败返回 (None, 原因)，由调用方降级 sqlite。"""
    dsn = os.environ.get("CHECKPOINT_POSTGRES_DSN")
    if not dsn:
        return None, "未配置 CHECKPOINT_POSTGRES_DSN"
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except Exception as exc:
        return None, f"缺少 langgraph-checkpoint-postgres/psycopg（{type(exc).__name__}）"
    try:
        saver = PostgresSaver.from_conn_string(dsn)
        saver.setup()  # 首次建表（checkpoints / checkpoint_blobs / checkpoint_writes）
        return saver, None
    except Exception as exc:
        return None, f"Postgres 连接或建表失败（{type(exc).__name__}: {str(exc)[:120]}）"


def build_checkpointer(mode: str | None = None,
                       db_path: str | Path | None = None,
                       serde=None) -> tuple[object, str, str | None]:
    """按 CHECKPOINT_MODE 构建检查点存储，返回 (checkpointer, mode, note)。

    - sqlite（默认）：嵌入式文件（backend/checkpoints.db），重启不丢会话，
      支持 interrupt/Command(resume) 跨进程恢复；无需任何服务端；
    - postgres：需 langgraph-checkpoint-postgres + CHECKPOINT_POSTGRES_DSN，
      任一步缺失自动降级 sqlite 并带回原因（Render Managed PG 场景再开）；
    - none/memory：MemorySaver，进程重启即丢（仅测试与无状态演示用）。
    """
    serde = serde or domain_serde()
    mode = (mode or os.environ.get("CHECKPOINT_MODE") or _DEFAULT_MODE).lower().strip()

    if mode in ("none", "memory", "off"):
        return MemorySaver(serde=serde), "memory", None

    note: str | None = None
    if mode == "postgres":
        saver, why = _build_postgres(serde)
        if saver is not None:
            return saver, "postgres", None
        note = f"CHECKPOINT_MODE=postgres 降级为 sqlite：{why}"
    elif mode != "sqlite":
        note = f"未知 CHECKPOINT_MODE={mode}，按 sqlite 处理"

    if SqliteSaver is None:
        return MemorySaver(serde=serde), "memory", "未安装 langgraph-checkpoint-sqlite，已退回内存"
    path = _resolve_db_path(db_path)
    try:
        # check_same_thread=False：FastAPI 同步端点在线程池执行，跨线程共用连接
        conn = sqlite3.connect(path, check_same_thread=False)
        return SqliteSaver(conn, serde=serde), "sqlite", note
    except Exception as exc:
        # sqlite 也不可用的极端场景：退回内存，保证服务能起（重启丢会话）
        return (MemorySaver(serde=serde), "memory",
                f"sqlite 初始化失败（{type(exc).__name__}: {str(exc)[:120]}），已退回内存")
