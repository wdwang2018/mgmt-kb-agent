"""知识库存储适配器：JSONL 文件实现（P0）。

P0 阶段知识单元以 JSONL 落盘，由知识编辑人工审核签发后入库；
P1 迁移 PostgreSQL + Neo4j 时仅替换本适配器。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..domain.models import KnowledgeUnit
from ..ports import KnowledgeStore


class JsonKnowledgeStore(KnowledgeStore):
    def __init__(self, knowledge_dir: str | Path, review_log_dir: str | Path | None = None):
        self.units_dir = Path(knowledge_dir) / "units"
        self.cases_dir = Path(knowledge_dir) / "cases"
        self.review_log_dir = Path(review_log_dir) if review_log_dir else Path(knowledge_dir) / "reviews"
        self.review_log_dir.mkdir(parents=True, exist_ok=True)

    def load_units(self) -> list[KnowledgeUnit]:
        units: list[KnowledgeUnit] = []
        for path in sorted(self.units_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                units.append(KnowledgeUnit.model_validate_json(line))
        return units

    def load_cases(self) -> list[dict[str, Any]]:
        cases: list[dict[str, Any]] = []
        for path in sorted(self.cases_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    cases.append(json.loads(line))
        return cases

    def save_unit(self, unit: KnowledgeUnit) -> None:
        self.units_dir.mkdir(parents=True, exist_ok=True)  # 空知识库环境（测试/首次部署）兜底
        target = self.units_dir / f"{unit.id.split('-')[1].lower()}_draft.jsonl"
        with target.open("a", encoding="utf-8") as f:
            f.write(unit.model_dump_json(exclude_none=True) + "\n")

    def log_review(self, run_id: str, action: str, payload: dict[str, Any]) -> None:
        record = {"run_id": run_id, "action": action, "payload": payload}
        with (self.review_log_dir / "reviews.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
