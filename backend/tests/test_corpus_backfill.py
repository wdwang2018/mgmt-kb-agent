"""L1 语料层回填（要点级粒度）测试。

验证重点：
- 要点级覆盖：每个单元的每条要点都有对应语料，且 locator / 版权标记继承单元；
- 单文件约束：corpus 目录只能有一个 jsonl——save_chunks() 全量重写到单一文件、
  load_chunks() 却读取目录下所有 *.jsonl，多出一个文件就会导致重复加载与编号冲突；
- 幂等：重复回填不翻倍；
- 回链消歧：书名重名时优先采信「书名+作者」双命中者；无法定夺则宁可不链、不可错链。

注：部分用例读取仓库真实 knowledge/ 作为数据完整性护栏——
    改动单元要点后必须重跑 scripts/backfill_corpus.py，否则此处会红。
    合成夹具的 id 必须落在 KnowledgeUnit 允许的前缀内（无 TEAM 等域代码前缀）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.adapters.corpus_json import JsonCorpusStore
from app.adapters.kb_json import JsonKnowledgeStore
from app.domain.models import CorpusChunk, EvidenceLevel, KnowledgeUnit, KUType, SourceRef

REPO_ROOT = Path(__file__).resolve().parents[2]
K_DIR = REPO_ROOT / "knowledge"

_SPEC = importlib.util.spec_from_file_location(
    "backfill_corpus", REPO_ROOT / "backend" / "scripts" / "backfill_corpus.py"
)
bf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bf)


def _unit(uid, title, book, author, kps, locator="第3章", year=2000, cs="要点化"):
    return KnowledgeUnit(
        id=uid,
        title=title,
        type=KUType.MODEL,
        domains=["TEAM"],
        dilemmas=["D01"],
        evidence_level=EvidenceLevel.B,
        source=SourceRef(book=book, author=author, year=year, locator=locator, copyright_status=cs),
        key_points=kps,
    )


# ---------------------------------------------------------------------------
# 真实数据护栏
# ---------------------------------------------------------------------------
def test_corpus_api_exposes_unit_id_and_supports_reverse_lookup():
    """端点头必须透出 unit_id，且支持 ?unit_id= 反查某单元的全部出处语料。

    否则回链只存在于文件里、对消费方不可见，溯源形同虚设。
    """
    import tempfile

    from fastapi.testclient import TestClient

    import app.api.main as main
    from app.api.main import app
    from app.services.context import build_context

    main.ctx = build_context(knowledge_dir=K_DIR, output_dir=Path(tempfile.mkdtemp()))
    client = TestClient(app)

    allc = client.get("/api/v1/kb/corpus").json()
    assert len(allc) == len(JsonCorpusStore(str(K_DIR)).load_chunks())
    assert all("unit_id" in x for x in allc), "unit_id 未透出，回链对外不可见"
    assert all("ingested_by" in x for x in allc), "ingested_by 未透出，无法分辨人工采集与回填"

    units = {u.id: u for u in JsonKnowledgeStore(str(K_DIR)).load_units()}
    target = "KU-CONT-001"
    assert target in units, "样例单元缺失"
    mine = client.get(f"/api/v1/kb/corpus?unit_id={target}").json()
    assert len(mine) == len(units[target].key_points), "反查条数应等于该单元要点数"
    assert all(x["unit_id"] == target for x in mine)
    assert all(x["locator"] == units[target].source.locator for x in mine)

    assert client.get("/api/v1/kb/corpus?unit_id=KU-NOPE-000").json() == []


def test_repo_corpus_single_jsonl_and_unique_ids():
    """corpus 目录必须只有一个 jsonl，且编号全局唯一。"""
    jsonls = sorted((K_DIR / "corpus").glob("*.jsonl"))
    assert [p.name for p in jsonls] == ["corpus-v1.jsonl"], (
        f"多出的 jsonl 会被 load_chunks 重复读取：{[p.name for p in jsonls]}"
    )

    chunks = JsonCorpusStore(str(K_DIR)).load_chunks()
    ids = [c.id for c in chunks]
    assert ids, "语料层为空，回填未生效"
    assert len(ids) == len(set(ids)), f"语料编号重复：{[i for i in ids if ids.count(i) > 1]}"


def test_repo_every_keypoint_has_corpus_chunk():
    """每个单元的每条要点都有一条语料，locator 与版权标记继承自单元。"""
    units = JsonKnowledgeStore(str(K_DIR)).load_units()
    chunks = JsonCorpusStore(str(K_DIR)).load_chunks()
    backfilled = [c for c in chunks if c.ingested_by == "backfill"]
    by_unit: dict[str, list[CorpusChunk]] = {}
    for c in backfilled:
        by_unit.setdefault(c.unit_id, []).append(c)

    assert len(backfilled) == sum(len(u.key_points) for u in units), (
        "回填条数 != 要点总数，说明单元改动后未重跑回填脚本"
    )
    for u in units:
        mine = by_unit.get(u.id, [])
        assert len(mine) == len(u.key_points), f"{u.id} 语料数 {len(mine)} != 要点数 {len(u.key_points)}"
        # 每条语料的正文必须是该单元某条要点本身
        assert all(c.text in u.key_points for c in mine), f"{u.id} 语料正文不来自其要点"
        # 定位与版权必须跟随单元，不允许回填时自作主张
        assert all(c.locator == u.source.locator for c in mine), f"{u.id} locator 未继承"
        assert all(c.copyright_status == u.source.copyright_status for c in mine), f"{u.id} 版权标记未继承"


# ---------------------------------------------------------------------------
# 合成数据：幂等与消歧
# ---------------------------------------------------------------------------
def test_build_chunks_inherits_locator_and_copyright():
    """行为测试（非数据测试）：直接驱动 build_chunks，覆盖 locator/版权/正文继承。

    夹具刻意让两个单元的 locator 与 copyright_status **互不相同**——
    否则「硬编码某个值」的坏实现仍能通过，测试就成了空转。
    """
    units = [
        _unit("KU-LEAD-001", "A", "书甲", "甲", ["要点一", "要点二"], locator="第7章", cs="原创"),
        _unit("KU-LEAD-002", "B", "书乙", "乙", ["要点三", "要点四"], locator="第2章", cs="要点化"),
    ]
    gen = bf.build_chunks(units, [])

    assert len(gen) == 4
    for u in units:
        mine = [c for c in gen if c.unit_id == u.id]
        assert [c.text for c in mine] == u.key_points, f"{u.id} 正文应逐条等于要点本身"
        assert all(c.locator == u.source.locator for c in mine), f"{u.id} locator 必须继承单元"
        assert all(c.copyright_status == u.source.copyright_status for c in mine), f"{u.id} 版权必须继承单元"
    # 跨单元必须出现不同取值，杜绝硬编码蒙混过关
    assert {c.locator for c in gen} == {"第7章", "第2章"}
    assert {c.copyright_status for c in gen} == {"原创", "要点化"}


def test_build_chunks_is_idempotent():
    """重复回填不翻倍：剔除旧 backfill 后重建，条目数与编号应完全一致。"""
    units = [
        _unit("KU-LEAD-001", "A", "团队之书", "甲", ["要点一", "要点二"]),
        _unit("KU-LEAD-002", "B", "另一本", "乙", ["要点三", "要点四"]),
    ]
    manual = [CorpusChunk(id="CC-0001", book="手工书", author="丙", locator="第1章", text="旧语料")]

    first = bf.build_chunks(units, manual)
    second = bf.build_chunks(units, manual)          # 幂等重跑
    assert [c.id for c in first] == [c.id for c in second]
    assert len(first) == 1 + 2 + 2                   # 1 人工 + 每单元 2 条要点
    ids = [c.id for c in first]
    assert ids == ["CC-0001", "CC-0002", "CC-0003", "CC-0004", "CC-0005"]
    assert all(c.unit_id in {"KU-LEAD-001", "KU-LEAD-002"} for c in first if c.unit_id)


def test_guess_unit_id_prefers_book_and_author():
    """书名重名时，优先采信作者也命中的那条。"""
    units = [
        _unit("KU-LEAD-013", "新经理转身", "领导梯队", "甲", ["a", "b"]),
        _unit("KU-LDS-005", "查兰领导梯队", "The Leadership Pipeline 领导梯队", "乙", ["c", "d"]),
    ]
    chunk = CorpusChunk(id="CC-0009", book="领导梯队", author="甲",
                        locator="第3章", text="旧语料")
    assert bf.guess_unit_id(chunk, units) == "KU-LEAD-013"


def test_guess_unit_id_refuses_ambiguous():
    """两条都强命中时宁可不链，避免错配制造假溯源。"""
    units = [
        _unit("KU-LEAD-013", "X", "领导梯队", "同一作者", ["a", "b"]),
        _unit("KU-LDS-005", "Y", "领导梯队", "同一作者", ["c", "d"]),
    ]
    chunk = CorpusChunk(id="CC-0009", book="领导梯队", author="同一作者", locator="第3章", text="旧语料")
    assert bf.guess_unit_id(chunk, units) is None
