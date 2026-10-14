"""导出三个核心工件的 JSON Schema（能力模型 / 困境索引 / 知识单元）。

用法：python scripts/export_schemas.py
产出：app/domain/schemas/{competency_model,dilemma_index,knowledge_unit}.schema.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.constants import COMPETENCY_DOMAINS, DILEMMA_INDEX, EVIDENCE_LEVELS
from app.domain.models import KnowledgeUnit

OUT = Path(__file__).resolve().parents[1] / "app" / "domain" / "schemas"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    competency = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "管理能力模型（六域 × L1-L5 行为锚定）",
        "type": "object",
        "properties": {
            code: {
                "type": "object",
                "properties": {
                    "name": {"const": d["name"]},
                    "anchors": {
                        "type": "object",
                        "properties": {str(lv): {"const": txt} for lv, txt in d["anchors"].items()},
                    },
                },
            }
            for code, d in COMPETENCY_DOMAINS.items()
        },
        "additionalProperties": False,
    }
    (OUT / "competency_model.schema.json").write_text(
        json.dumps(competency, ensure_ascii=False, indent=2), encoding="utf-8")

    dilemma = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "管理困境索引 v1",
        "type": "object",
        "properties": {code: {"const": desc} for code, desc in DILEMMA_INDEX.items()},
        "additionalProperties": False,
    }
    (OUT / "dilemma_index.schema.json").write_text(
        json.dumps(dilemma, ensure_ascii=False, indent=2), encoding="utf-8")

    ku = KnowledgeUnit.model_json_schema()
    ku["title"] = "知识单元（L2 层核心资产）"
    ku["properties"]["evidence_level"]["description"] = "；".join(
        f"{k}: {v}" for k, v in EVIDENCE_LEVELS.items())
    (OUT / "knowledge_unit.schema.json").write_text(
        json.dumps(ku, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"schemas exported to {OUT}")
    for f in sorted(OUT.glob("*.json")):
        print(" -", f.name)


if __name__ == "__main__":
    main()
