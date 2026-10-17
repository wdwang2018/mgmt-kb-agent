"""知识图谱适配器：Neo4j 服务端实现（GRAPH_MODE=neo4j）。

与 KuzuGraphStore 同接口、Cypher 语句同构——Kùzu 兼容 Cypher 子集，
故两适配器的查询可近乎平移（差异仅在连接方式与结果读取）。

环境要求（当前环境不具备，装好后用环境变量切换即可）：
- Neo4j 服务端（Docker/Neo4j Desktop/Java 直装），bolt 端口 7687；
- `pip install neo4j`（driver，lazy import，未装时构造即抛清晰错误）。

连接参数：GRAPH_URI（默认 bolt://localhost:7687）/ GRAPH_USER（默认 neo4j）/
GRAPH_PASSWORD（必填，无默认值——避免误连本地空口令实例）。

唯一约束：启动时对 KU/Person/School/View/KCase 的主键建 CONSTRAINT，
保证 MERGE 幂等（对应 Kùzu 的 PRIMARY KEY 语义）。
"""

from __future__ import annotations

import os

from ..domain.models import KnowledgeUnit
from ..ports import GraphStore

_DEFAULT_URI = "bolt://localhost:7687"

_CONSTRAINTS = [
    ("KU", "id"), ("Person", "name"), ("School", "name"),
    ("View", "name"), ("KCase", "id"),
]


class Neo4jGraphStore(GraphStore):
    """Neo4j 图存储。lazy import：未安装 neo4j driver 或连不上时抛带指引的异常。"""

    name = "neo4j"

    def __init__(self, uri: str | None = None, user: str | None = None,
                 password: str | None = None):
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover - 环境分支
            raise RuntimeError(
                "GRAPH_MODE=neo4j 需要 neo4j driver（pip install neo4j）与 Neo4j 服务端"
                "（Docker / Neo4j Desktop / Java 直装，bolt 7687）；"
                "无服务端环境请用 GRAPH_MODE=kuzu（嵌入式）或 none。"
            ) from exc

        self._uri = uri or os.environ.get("GRAPH_URI") or _DEFAULT_URI
        self._user = user or os.environ.get("GRAPH_USER") or "neo4j"
        self._password = password or os.environ.get("GRAPH_PASSWORD") or ""
        if not self._password:
            raise RuntimeError(
                "GRAPH_MODE=neo4j 需要 GRAPH_PASSWORD（Neo4j 连接口令）；"
                "请在环境变量或仓库根 .env 中配置。"
            )
        self._driver = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
        self._driver.verify_connectivity()
        with self._driver.session() as s:
            for label, key in _CONSTRAINTS:
                s.run(f"CREATE CONSTRAINT {label.lower()}_{key}_uniq IF NOT EXISTS "
                      f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def _run(self, cypher: str, params: dict | None = None) -> list[tuple]:
        with self._driver.session() as s:
            return [tuple(r.values()) for r in s.run(cypher, params or {})]

    # ------------------------------------------------------------ 写入
    @staticmethod
    def _first_domain(unit: KnowledgeUnit) -> str:
        return unit.domains[0] if unit.domains else ""

    def _merge_node(self, table: str, key: str, value: str, extra: dict | None = None) -> None:
        sets = ", ".join(f"n.{k}=${k}" for k in (extra or {}))
        stmt = f"MERGE (n:{table} {{{key}:${key}}})" + (f" SET {sets}" if sets else "")
        self._run(stmt, {key: value, **(extra or {})})

    def _merge_edge(self, rel: str, frm: str, to_table: str, to_key: str, to: str) -> None:
        self._run(f"MATCH (a:KU {{id:$f}}), (b:{to_table} {{{to_key}:$t}}) "
                  f"MERGE (a)-[:{rel}]->(b)", {"f": frm, "t": to})

    def sync_unit(self, unit: KnowledgeUnit, known_ids: set[str] | None = None) -> None:
        """MERGE 幂等写入（语义与 KuzuGraphStore.sync_unit 一致）。"""
        self._merge_node("KU", "id", unit.id,
                         {"title": unit.title, "domain": self._first_domain(unit),
                          "evidence": str(getattr(unit.evidence_level, "value", unit.evidence_level))})

        def link_ku(rel: str, target: str) -> None:
            if known_ids is not None and target not in known_ids:
                return
            self._merge_node("KU", "id", target)
            self._merge_edge(rel, unit.id, "KU", "id", target)

        for rel in unit.relations or []:
            rtype = getattr(rel.type, "value", rel.type)
            target = (rel.target or "").strip()
            if not target:
                continue
            if rtype == "提出者":
                self._merge_node("Person", "name", target)
                self._merge_edge("PROPOSED_BY", unit.id, "Person", "name", target)
            elif rtype == "学派":
                self._merge_node("School", "name", target)
                self._merge_edge("BELONGS_TO", unit.id, "School", "name", target)
            elif rtype == "争议":
                self._merge_node("View", "name", target)
                self._merge_edge("CONTESTS", unit.id, "View", "name", target)
            elif rtype == "对比":
                self._merge_node("View", "name", target)
                self._merge_edge("COMPARES", unit.id, "View", "name", target)
            elif rtype == "前置":
                link_ku("PREREQUISITE", target)
            elif rtype == "应用":
                if target.upper().startswith("CASE-"):
                    self._merge_node("KCase", "id", target, {"title": target})
                    self._merge_edge("APPLIES", unit.id, "KCase", "id", target)
                else:
                    link_ku("RELATED", target)
        for pre in unit.prerequisites or []:
            link_ku("PREREQUISITE", pre)
        for rel_id in unit.related or []:
            if known_ids is not None and rel_id not in known_ids:
                continue
            self._merge_node("KU", "id", rel_id)
            self._merge_edge("RELATED", unit.id, "KU", "id", rel_id)
            self._merge_edge("RELATED", rel_id, "KU", "id", unit.id)

    def rebuild(self, units: list[KnowledgeUnit]) -> None:
        """全量重建：删陈旧 KU 节点后逐个 MERGE（差值同步，语义同 Kùzu 版）。"""
        live = {u.id for u in units}
        stale = [r[0] for r in self._run("MATCH (a:KU) RETURN a.id") if r[0] not in live]
        for sid in stale:
            self._run("MATCH (a:KU {id:$id}) DETACH DELETE a", {"id": sid})
        for unit in units:
            self.sync_unit(unit, known_ids=live)

    # ------------------------------------------------------------ 查询
    def prerequisites(self, unit_id: str, depth: int = 3) -> list[str]:
        """前置概念链（多跳遍历），返回祖先单元 id，近者在前。DISTINCT 去重（多路径）。"""
        rows = self._run(
            f"MATCH (a:KU {{id:$id}})-[:PREREQUISITE*1..{max(1, depth)}]->(b:KU) "
            "RETURN DISTINCT b.id ORDER BY b.id", {"id": unit_id})
        return [r[0] for r in rows]

    def proposers(self, unit_id: str) -> list[str]:
        rows = self._run(
            "MATCH (a:KU {id:$id})-[:PROPOSED_BY]->(p:Person) RETURN p.name ORDER BY p.name",
            {"id": unit_id})
        return [r[0] for r in rows]

    def contested_views(self, unit_id: str) -> list[str]:
        rows = self._run(
            "MATCH (a:KU {id:$id})-[:CONTESTS|COMPARES]->(v:View) "
            "RETURN DISTINCT v.name ORDER BY v.name", {"id": unit_id})
        return [r[0] for r in rows]

    def schools(self, unit_id: str) -> list[str]:
        rows = self._run(
            "MATCH (a:KU {id:$id})-[:BELONGS_TO]->(s:School) RETURN s.name ORDER BY s.name",
            {"id": unit_id})
        return [r[0] for r in rows]

    def applied_cases(self, unit_id: str) -> list[str]:
        rows = self._run(
            "MATCH (a:KU {id:$id})-[:APPLIES]->(c:KCase) RETURN c.id ORDER BY c.id",
            {"id": unit_id})
        return [r[0] for r in rows]

    def neighbors(self, unit_id: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for rel in ("PREREQUISITE", "PROPOSED_BY", "BELONGS_TO", "CONTESTS",
                    "COMPARES", "APPLIES", "RELATED"):
            rows = self._run(
                f"MATCH (a:KU {{id:$id}})-[:{rel}]-(b) RETURN DISTINCT b.id, b.name ORDER BY 1",
                {"id": unit_id})
            ids = [(r[0] or r[1]) for r in rows if (r[0] or r[1])]
            if ids:
                out[rel] = ids
        return out

    def stats(self) -> dict[str, int]:
        def count(node: str, key: str) -> int:
            return self._run(f"MATCH (n:{node}) RETURN count(n.{key})")[0][0]

        def rel_count(rel: str) -> int:
            return self._run(f"MATCH ()-[r:{rel}]->() RETURN count(r)")[0][0]

        return {
            "ku_nodes": count("KU", "id"),
            "person_nodes": count("Person", "name"),
            "school_nodes": count("School", "name"),
            "view_nodes": count("View", "name"),
            "case_nodes": count("KCase", "id"),
            "relations": sum(rel_count(r) for r in
                             ("PREREQUISITE", "PROPOSED_BY", "BELONGS_TO",
                              "CONTESTS", "COMPARES", "APPLIES", "RELATED")),
        }
