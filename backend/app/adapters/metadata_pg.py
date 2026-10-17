"""元数据存储适配器：PostgreSQL 实现（STORE_MODE=pg）。

当前环境无 PostgreSQL 服务端（无 Docker、pgserver 无 Windows 发行版），
故本适配器的真实路径尚未实测；连不上时由工厂自动降级 JSONL 并记原因。
psycopg2 已装（2.9.12），lazy import 防未装环境导入失败。

表结构（PG 模式）：
- units：知识单元主表，id 主键 + payload JSONB（upsert 语义，比 JSONL 追加更干净）；
- reviews：HITL 审核台账（log_review 写入）；
- runs：流水线运行记录（state JSONB，upsert）。

连接：PG_DSN（如 postgresql://user:pass@localhost:5432/mgmtkb）。
schema 由连接后首次 _ensure_schema 建表（IF NOT EXISTS，幂等）。
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..domain.models import KnowledgeUnit
from ..ports import KnowledgeStore

_DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/mgmtkb"

_DDL = [
    """CREATE TABLE IF NOT EXISTS units (
        id VARCHAR(64) PRIMARY KEY,
        payload JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS reviews (
        id BIGSERIAL PRIMARY KEY,
        run_id VARCHAR(64) NOT NULL,
        action VARCHAR(32) NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id VARCHAR(64) PRIMARY KEY,
        kind VARCHAR(32) NOT NULL,
        state JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
]


class PgMetadataStore(KnowledgeStore):
    """PostgreSQL 元数据存储。构造时即验证连接（连不上抛异常，由工厂降级）。"""

    def __init__(self, dsn: str | None = None):
        try:
            import psycopg2
        except ImportError as exc:  # pragma: no cover - 环境分支
            raise RuntimeError(
                "STORE_MODE=pg 需要 psycopg2（pip install psycopg2-binary）与 "
                "PostgreSQL 服务端；无服务端环境请用 STORE_MODE=json（默认）。"
            ) from exc
        self._dsn = dsn or os.environ.get("PG_DSN") or _DEFAULT_DSN
        self._conn = psycopg2.connect(self._dsn)
        self._conn.autocommit = True
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)

    # ------------------------------------------------------------ 读取
    def load_units(self) -> list[KnowledgeUnit]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT payload FROM units ORDER BY id")
            return [KnowledgeUnit.model_validate(dict(row[0])) for row in cur.fetchall()]

    def load_cases(self) -> list[dict[str, Any]]:
        # 案例卡仍走 JSONL 资产目录（L3 场景资产，PG 模式只迁元数据）
        return []

    # ------------------------------------------------------------ 写入
    def save_unit(self, unit: KnowledgeUnit) -> None:
        payload = json.loads(unit.model_dump_json(exclude_none=True))
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO units (id, payload, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, "
                "updated_at = now()",
                (unit.id, json.dumps(payload, ensure_ascii=False)))

    def log_review(self, run_id: str, action: str, payload: dict[str, Any]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reviews (run_id, action, payload) VALUES (%s, %s, %s)",
                (run_id, action, json.dumps(payload, ensure_ascii=False)))

    # ------------------------------------------------------------ 运行记录
    def save_run(self, run_id: str, kind: str, state: dict[str, Any]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, kind, state, updated_at) VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (run_id) DO UPDATE SET state = EXCLUDED.state, "
                "updated_at = now()",
                (run_id, kind, json.dumps(state, ensure_ascii=False)))
