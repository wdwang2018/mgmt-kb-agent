"""知识图谱适配器：Kùzu 嵌入式图库实现（GRAPH_MODE=kuzu）。

选型理由（2026-10-17 实测）：
- Kùzu 提供 win_amd64 wheel，**无需 Docker / Java / 服务端**，与 Milvus Lite 同路线；
- 兼容 Cypher 子集，与 Neo4j 适配器查询语句可近乎平移（换 driver 与参数占位符）；
- MERGE 幂等实测通过：重复 sync_unit / rebuild 不产生重复节点与边（「入库即生效」不写脏）；
- 本地文件存储，进程重启后图数据仍在。

节点表：KU（知识单元）/ Person（理论提出者）/ School（学派）/ View（争议或对立观点）/ KCase（案例卡）。
关系表：PREREQUISITE（前置依赖）/ PROPOSED_BY（提出者）/ BELONGS_TO（学派归属）/
        CONTESTS（争议观点）/ COMPARES（对比观点）/ APPLIES（应用于案例）/ RELATED（相关单元）。

覆盖 RelationType 全部 7 种取值：提出者/学派/争议/前置/应用/对比（relations 字段）
与单元级 prerequisites / related 两个数组字段。

避坑：`Case` 是 Kùzu 保留字（撞 CASE），节点表名用 `KCase`。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..domain.models import KnowledgeUnit
from ..ports import GraphStore

_DEFAULT_DB = "graph_kb.kuzu"

_DDL = [
    "CREATE NODE TABLE KU(id STRING, title STRING, domain STRING, evidence STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Person(name STRING, PRIMARY KEY(name))",
    "CREATE NODE TABLE School(name STRING, PRIMARY KEY(name))",
    "CREATE NODE TABLE View(name STRING, PRIMARY KEY(name))",
    "CREATE NODE TABLE KCase(id STRING, title STRING, PRIMARY KEY(id))",
    "CREATE REL TABLE PREREQUISITE(FROM KU TO KU)",
    "CREATE REL TABLE PROPOSED_BY(FROM KU TO Person)",
    "CREATE REL TABLE BELONGS_TO(FROM KU TO School)",
    "CREATE REL TABLE CONTESTS(FROM KU TO View)",
    "CREATE REL TABLE COMPARES(FROM KU TO View)",
    "CREATE REL TABLE APPLIES(FROM KU TO KCase)",
    # RELATED 为无向语义；Kùzu 不支持双向 FROM-TO 声明，故写入时双向各建一条边
    "CREATE REL TABLE RELATED(FROM KU TO KU)",
]

_NODE_TABLES = {"KU": "id", "Person": "name", "School": "name", "View": "name", "KCase": "id"}
_REL_TABLES = ["PREREQUISITE", "PROPOSED_BY", "BELONGS_TO", "CONTESTS",
               "COMPARES", "APPLIES", "RELATED"]


class KuzuGraphStore(GraphStore):
    """Kùzu 图存储。lazy import：未安装 kuzu 时构造即抛清晰错误（由工厂降级）。"""

    name = "kuzu"

    def __init__(self, db_path: str | Path | None = None):
        try:
            import kuzu  # noqa: F401
        except ImportError as exc:  # pragma: no cover - 环境分支
            raise RuntimeError(
                "GRAPH_MODE=kuzu 需要 kuzu 包（pip install kuzu，win_amd64 wheel 可用，"
                "无需 Docker/Java）；未安装时请用 GRAPH_MODE=none 关闭图查询。"
            ) from exc
        import kuzu

        self._db_path = str(db_path or os.environ.get("GRAPH_DB_PATH") or _DEFAULT_DB)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(self._db_path)
        self._conn = kuzu.Connection(self._db)
        self._ensure_schema()

    # ------------------------------------------------------------ 内部
    def _ensure_schema(self) -> None:
        """建缺失的表（幂等）：先列已有表，只建不存在的，避免 DDL 重复执行报错。"""
        existing = {row[0] for row in self._q("CALL SHOW_TABLES() RETURN name")}
        for stmt in _DDL:
            kind, rest = stmt.split(" TABLE ", 1)
            name = rest.split("(", 1)[0].strip()
            if name not in existing:
                self._conn.execute(stmt)

    def _q(self, cypher: str, params: dict | None = None):
        return self._conn.execute(cypher, params or {}).get_all()

    @staticmethod
    def _first_domain(unit: KnowledgeUnit) -> str:
        return unit.domains[0] if unit.domains else ""

    # ------------------------------------------------------------ 写入
    def _merge_node(self, table: str, key: str, value: str, extra: dict | None = None) -> None:
        """MERGE 节点（幂等）；extra 为可选的 SET 属性。"""
        if extra:
            sets = ", ".join(f"n.{k}=${k}" for k in extra)
            params = {key: value, **extra}
            self._conn.execute(f"MERGE (n:{table} {{{key}:${key}}}) SET {sets}", params)
        else:
            self._conn.execute(f"MERGE (n:{table} {{{key}:${key}}})", {key: value})

    def _merge_edge(self, rel: str, frm: str, to_table: str, to_key: str, to: str) -> None:
        self._conn.execute(
            f"MATCH (a:KU {{id:$f}}), (b:{to_table} {{{to_key}:$t}}) MERGE (a)-[:{rel}]->(b)",
            {"f": frm, "t": to})

    def sync_unit(self, unit: KnowledgeUnit, known_ids: set[str] | None = None) -> None:
        """MERGE 幂等写入：节点 + 全部关系边（覆盖 RelationType 全部 7 种）。

        known_ids：存活单元 id 集合。rebuild 时传入，用于过滤悬空引用
        （related/prerequisites 指向已删除单元时不建节点不建边）；
        单独入库调用时传 None，不过滤（引用的单元可能尚未加载）。
        """
        self._merge_node("KU", "id", unit.id,
                         {"title": unit.title, "domain": self._first_domain(unit),
                          "evidence": str(getattr(unit.evidence_level, "value", unit.evidence_level))})

        def link_ku(rel: str, target: str) -> None:
            if known_ids is not None and target not in known_ids:
                return  # 悬空引用：不建节点不建边
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
            # 未知 rtype 静默跳过（前向兼容 RelationType 扩容）

        for pre in unit.prerequisites or []:
            link_ku("PREREQUISITE", pre)
        for rel_id in unit.related or []:
            if known_ids is not None and rel_id not in known_ids:
                continue  # 悬空引用：不建节点不建边
            # RELATED 无向语义：双向各建一条边，查询侧用 -[:RELATED]- 匹配
            self._merge_node("KU", "id", rel_id)
            self._merge_edge("RELATED", unit.id, "KU", "id", rel_id)
            self._merge_edge("RELATED", rel_id, "KU", "id", unit.id)

    def rebuild(self, units: list[KnowledgeUnit]) -> None:
        """全量重建：删除陈旧 KU 节点后逐个 MERGE（差值同步）。

        stale 判定用「存活单元 id 集合」；sync_unit 传入同一集合过滤悬空引用，
        避免 related/prerequisites 指向的已删单元被 MERGE 建回（先删后建的白做功）。
        """
        live = {u.id for u in units}
        stale = [row[0] for row in self._q("MATCH (a:KU) RETURN a.id") if row[0] not in live]
        for sid in stale:
            self._conn.execute("MATCH (a:KU {id:$id}) DETACH DELETE a", {"id": sid})
        for unit in units:
            self.sync_unit(unit, known_ids=live)

    # ------------------------------------------------------------ 查询
    def prerequisites(self, unit_id: str, depth: int = 3) -> list[str]:
        """前置概念链（多跳遍历），返回祖先单元 id，近者在前。

        DISTINCT 去重：同一祖先可能经多条路径到达（如 008→001 与 008→002→001），
        不去重会返回重复 id。
        """
        rows = self._q(
            f"MATCH (a:KU {{id:$id}})-[:PREREQUISITE*1..{max(1, depth)}]->(b:KU) "
            "RETURN DISTINCT b.id ORDER BY b.id",
            {"id": unit_id})
        return [r[0] for r in rows]

    def proposers(self, unit_id: str) -> list[str]:
        rows = self._q(
            "MATCH (a:KU {id:$id})-[:PROPOSED_BY]->(p:Person) RETURN p.name ORDER BY p.name",
            {"id": unit_id})
        return [r[0] for r in rows]

    def contested_views(self, unit_id: str) -> list[str]:
        """争议观点（CONTESTS）+ 对比观点（COMPARES）——证据分级与学派争议的图查询入口。"""
        rows = self._q(
            "MATCH (a:KU {id:$id})-[:CONTESTS|COMPARES]->(v:View) RETURN DISTINCT v.name ORDER BY v.name",
            {"id": unit_id})
        return [r[0] for r in rows]

    def schools(self, unit_id: str) -> list[str]:
        """学派归属（relations 中 type=学派）。"""
        rows = self._q(
            "MATCH (a:KU {id:$id})-[:BELONGS_TO]->(s:School) RETURN s.name ORDER BY s.name",
            {"id": unit_id})
        return [r[0] for r in rows]

    def applied_cases(self, unit_id: str) -> list[str]:
        rows = self._q(
            "MATCH (a:KU {id:$id})-[:APPLIES]->(c:KCase) RETURN c.id ORDER BY c.id",
            {"id": unit_id})
        return [r[0] for r in rows]

    def neighbors(self, unit_id: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for rel in _REL_TABLES:
            rows = self._q(
                f"MATCH (a:KU {{id:$id}})-[:{rel}]-(b) RETURN DISTINCT b.id, b.name ORDER BY 1",
                {"id": unit_id})
            ids = [(r[0] or r[1]) for r in rows if (r[0] or r[1])]
            if ids:
                out[rel] = ids
        return out

    def stats(self) -> dict[str, int]:
        def count(node: str, key: str) -> int:
            return self._q(f"MATCH (n:{node}) RETURN count(n.{key})")[0][0]

        def rel_count(rel: str) -> int:
            return self._q(f"MATCH ()-[r:{rel}]->() RETURN count(r)")[0][0]

        return {
            "ku_nodes": count("KU", "id"),
            "person_nodes": count("Person", "name"),
            "school_nodes": count("School", "name"),
            "view_nodes": count("View", "name"),
            "case_nodes": count("KCase", "id"),
            "relations": sum(rel_count(r) for r in _REL_TABLES),
        }
