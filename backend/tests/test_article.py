"""文章摄入与解析（P6）的测试。

夹具设计铁律（本项目已踩过两次，勿忘）：
合成数据的关键字段取值必须**互不相同**，否则「硬编码某个值」的坏实现仍能蒙混过关。
例：两个单元给不同的 locator / 版权标记；这里的 LLM 假响应里
title / summary / 各代码 / 证据等级 / 课型 全部取不同值。

网络纪律：本文件**不发起任何真实网络请求**。正文抽取用本地 HTML 字符串，
URL 只测 SSRF 拦截（在发起请求前就被挡住），从而测试不依赖外网可用性。
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pytest

from app.adapters.llm_mock import MockLLM
from app.domain.constants import COMPETENCY_DOMAINS, COURSE_TYPES, DILEMMA_INDEX, THEORY_LINEAGE
from app.domain.models import (ArticleAnalysis, ArticleSource, KnowledgeUnit,
                               TrainingNeed)
from app.services.article_analyzer import (_norm_course_type, _norm_evidence,
                                           _norm_sentences, _norm_str_list,
                                           _resolve_codes, analyze_article,
                                           _code_table)
from app.services.article_intake import (IntakeError, _extract_main_text,
                                         _is_forbidden_ip, intake_bytes,
                                         intake_paste, intake_url)

REPO_ROOT = Path(__file__).resolve().parents[2]
K_DIR = REPO_ROOT / "knowledge"

# 三个取值刻意不同的 IP：私有 / 公网 v4 / 会误杀的协议分配段 v6
_PRIVATE_IP = "192.168.1.1"
_PUBLIC_V4 = "8.8.8.8"
_TEREDO_V6 = "2001::1"          # IETF 协议分配段，是公网可达的，绝不能拦


# ---------------------------------------------------------------------------
# A. 摄入：粘贴 / 文件 / 正文抽取 / SSRF
# ---------------------------------------------------------------------------
def test_paste_rejects_empty_and_tracks_length():
    with pytest.raises(IntakeError) as e:
        intake_paste("   ")
    assert e.value.code == "empty_text"

    text, warns = intake_paste("短")
    assert text == "短"
    assert any("过短" in w for w in warns), "过短必须给出提示，不能静默"


def test_paste_truncates_overlong_text():
    from app.services.article_intake import MAX_ARTICLE_CHARS

    text, warns = intake_paste("字" * (MAX_ARTICLE_CHARS + 500))
    assert len(text) == MAX_ARTICLE_CHARS, "超长必须截断，否则会打爆 token 预算"
    assert any("截断" in w for w in warns)


def test_file_accepts_text_rejects_unsupported_and_empty():
    text, _ = intake_bytes("a.md", "# 标题\n正文在这里".encode("utf-8"))
    assert "正文在这里" in text

    with pytest.raises(IntakeError) as e:
        intake_bytes("x.pptx", b"fake")
    assert e.value.code == "unsupported_format"

    with pytest.raises(IntakeError) as e:
        intake_bytes("e.txt", b"")
    assert e.value.code == "empty_file"


def test_docx_extracts_paragraphs_and_tables():
    docx = pytest.importorskip("docx")

    d = docx.Document()
    d.add_paragraph("段落一：领导风格需与情境匹配。")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "领导成员关系"
    t.cell(1, 0).text = "任务结构"
    buf = io.BytesIO()
    d.save(buf)

    text, _ = intake_bytes("a.docx", buf.getvalue())
    assert "领导风格需与情境匹配" in text
    assert "任务结构" in text, "表格内容也是正文，不能被丢掉"


def test_html_extraction_prefers_article_and_drops_noise():
    """广告与页脚在 article 之外，绝不能混进正文。"""
    html = (
        "<html><body>"
        "<nav><p>首页</p><p>关于我们</p></nav>"
        "<div class='ad'><p>广告内容不要我</p></div>"
        "<article><p>情境领导由 Hersey 提出。</p>"
        "<p>准备度由能力与意愿构成。</p></article>"
        "<footer><p>版权信息</p></footer>"
        "</body></html>"
    )
    text = _extract_main_text(html)
    assert "Hersey" in text and "准备度" in text
    assert "广告内容" not in text, "article 存在时必须信任它，不能落到拼接所有 p 的兜底"
    assert "版权信息" not in text


def test_html_extraction_falls_back_to_densest_container():
    html = (
        "<html><body><div class='menu'><p>菜单</p></div>"
        "<div class='content'><p>路径-目标理论由 House 提出。</p>"
        "<p>领导者职责是澄清路径。</p><p>四种行为：指导支持参与成就。</p></div>"
        "<div class='side'><p>侧边栏</p></div></body></html>"
    )
    text = _extract_main_text(html)
    assert "House" in text and "成就" in text
    assert "菜单" not in text and "侧边栏" not in text


def test_ssrf_blocks_private_but_not_public():
    import ipaddress

    assert _is_forbidden_ip(ipaddress.ip_address(_PRIVATE_IP)) is True
    assert _is_forbidden_ip(ipaddress.ip_address("127.0.0.1")) is True
    assert _is_forbidden_ip(ipaddress.ip_address("169.254.169.254")) is True, "云元数据必拦"
    assert _is_forbidden_ip(ipaddress.ip_address("::ffff:127.0.0.1")) is True, "IPv4-mapped 绕过必拦"
    assert _is_forbidden_ip(ipaddress.ip_address(_PUBLIC_V4)) is False


def test_ssrf_must_not_block_protocol_assigned_ipv6():
    """2001::/23 曾被整体判为私有，导致 en.wikipedia.org 被误杀。

    这是本条测试存在的唯一理由：宁可放过，也不能误伤公网可解析的正常站点。
    """
    import ipaddress

    assert _is_forbidden_ip(ipaddress.ip_address(_TEREDO_V6)) is False
    assert _is_forbidden_ip(ipaddress.ip_address("2001:4860:4860::8888")) is False
    # 同时确认真正的私有 v6 仍然被拦
    assert _is_forbidden_ip(ipaddress.ip_address("fd00::1")) is True
    assert _is_forbidden_ip(ipaddress.ip_address("fe80::1")) is True


def test_url_intake_blocks_localhost_before_any_request():
    for url in ("http://127.0.0.1:9/x", "http://localhost/x"):
        with pytest.raises(IntakeError) as e:
            intake_url(url)
        assert e.value.code == "forbidden_host", "必须在发起请求前拦截"


# ---------------------------------------------------------------------------
# B. LLM 输出规范化
# ---------------------------------------------------------------------------
def test_norm_str_list_handles_scalars_lists_and_dicts():
    assert _norm_str_list(None) == []
    assert _norm_str_list("TEAM") == ["TEAM"]
    assert _norm_str_list("TEAM、STRAT") == ["TEAM", "STRAT"]
    assert _norm_str_list([{"code": "TEAM"}]) == ["TEAM"]
    assert _norm_str_list(["a", "b\nTEAM"]) == ["a", "b", "TEAM"], "list 元素内嵌分隔符也必须切"


def test_resolve_codes_accepts_code_or_name_and_drops_invalid():
    table = _code_table(COMPETENCY_DOMAINS)
    assert _resolve_codes(["TEAM"], table) == ["TEAM"]
    assert _resolve_codes(["团队领导"], table) == ["TEAM"], "模型常给中文名而非代码"
    assert _resolve_codes(["TEAM", "团队领导"], table) == ["TEAM"], "同一目标须去重"
    assert _resolve_codes(["不存在的域", "TEAM"], table) == ["TEAM"], "非法代码必须丢弃"


def test_evidence_level_is_conservative():
    assert _norm_evidence("A") == "A"
    assert _norm_evidence("实证强支持") == "A"
    assert _norm_evidence("实证支持") == "B"
    assert _norm_evidence("经验流派") == "C"
    for bad in ("", None, "Z", "123", "随便说说"):
        assert _norm_evidence(bad) == "C", f"{bad!r} 认不出时必须降到 C，绝不抬升"


def test_course_type_matches_both_code_and_short_name():
    assert _norm_course_type("FULL_DAY") == "FULL_DAY"
    assert _norm_course_type("全天公开课") == "FULL_DAY"
    assert _norm_course_type("全天") == "FULL_DAY", "简称是被包含在全名里的一侧，须双向匹配"
    assert _norm_course_type("微课") == "MICRO"
    assert _norm_course_type("乱说") == "HALF_DAY"


def test_sentences_are_not_split_by_comma():
    """逗号是句子内部标点。按逗号切会把一句碎成三句。"""
    whole = "所谓准备度，由能力与意愿两个维度构成，组合出四个层级"
    got = _norm_sentences([whole])
    assert got == [whole], f"摘要被切碎：{got}"
    assert _norm_sentences("第一句，含逗号\n第二句，也含逗号") == ["第一句，含逗号", "第二句，也含逗号"]


# ---------------------------------------------------------------------------
# C. 解析服务（用假 LLM，断言的是规范化行为而非模型能力）
# ---------------------------------------------------------------------------
class _FakeLLM:
    """按给定 payload 返回，并记录收到的完整 user（用于验证哨兵）。"""

    name = "fake-article-llm"

    def __init__(self, payload: dict):
        self.payload = payload
        self.user = None
        self.purpose = None

    def complete_json(self, purpose, system, user, fallback):
        self.purpose = purpose
        self.user = user
        return self.payload

    def complete(self, purpose, system, user) -> str:
        return ""


_GOOD_PAYLOAD = {
    "title": "权变领导：风格要配情境",
    "summary": ["不存在普遍最优的领导方式，风格须与情境匹配。",
                "情境控制力由三个因素构成，缺一不可。"],
    "core_concepts": ["权变领导", "情境控制力"],
    "domains": ["TEAM"],
    "dilemmas": ["D02"],
    "lineages": ["L-CONTINGENCY"],
    "evidence_level": "B",
    "feasible": True,
    "feasibility_reason": "概念清晰且有可迁移行为",
    "course_type": "半天",           # 刻意给中文简称，验证规范化
    "audience_reco": "新任部门经理",
    "suggested_theme": "权变领导",
    "suggested_pain": "一刀切授权导致失控",
    "suggested_behavior": "先判断情境再决定放权程度",
    "suggested_duration": 180,
    "suggested_headcount": 30,
    "suggested_level": 2,
}


def test_analyze_normalizes_llm_output():
    llm = _FakeLLM(_GOOD_PAYLOAD)
    a = analyze_article("权变领导理论正文。" * 40, ArticleSource.PASTE, llm, retriever=None)

    assert a.source == ArticleSource.PASTE
    assert a.title == "权变领导：风格要配情境"
    assert a.summary == _GOOD_PAYLOAD["summary"], "摘要必须原样保留，不能被标点切碎"
    assert a.domains == ["TEAM"]
    assert a.dilemmas == ["D02"]
    assert a.lineages == ["L-CONTINGENCY"]
    assert a.evidence_level == "B"
    assert a.course_type == "HALF_DAY", "中文简称必须归一化成代码"
    assert a.feasible is True
    assert a.raw_chars > 0


def test_analyze_drops_invalid_codes_and_downgrades_unknown_evidence():
    payload = dict(_GOOD_PAYLOAD)
    payload["domains"] = ["TEAM", "编造的域"]
    payload["lineages"] = ["L-CONTINGENCY", "L-编造"]
    payload["evidence_level"] = "说不清"
    a = analyze_article("正文。" * 40, ArticleSource.PASTE, _FakeLLM(payload), retriever=None)

    assert a.domains == ["TEAM"], "非法域必须丢弃"
    assert a.lineages == ["L-CONTINGENCY"], "非法学派必须丢弃"
    assert a.evidence_level == "C", "说不清的证据等级必须降到 C"


def test_summary_absent_forces_infeasible():
    payload = dict(_GOOD_PAYLOAD)
    payload["summary"] = []
    a = analyze_article("正文。" * 40, ArticleSource.PASTE, _FakeLLM(payload), retriever=None)

    assert a.feasible is False, "连要点都没有，绝不能报可行"
    assert any("要点" in w for w in a.warnings)


def test_evidence_c_warns_when_feasible():
    payload = dict(_GOOD_PAYLOAD)
    payload["evidence_level"] = "C"
    a = analyze_article("正文。" * 40, ArticleSource.PASTE, _FakeLLM(payload), retriever=None)

    assert a.feasible is True
    assert any("C" in w and "实证" in w for w in a.warnings), "C 级成课必须提示讲师标明来源"


def test_mock_llm_reads_body_not_prompt():
    """哨兵的守卫测试：Mock 若读到整个 prompt，会把提示清单当正文。"""
    art = (
        "路径-目标理论：让下属看清路\n"
        "House 提出该理论，主张领导者澄清路径、清除障碍。\n"
        "研究显示其效果受任务结构调节，存在边界条件。\n"
    ) * 12
    a = analyze_article(art, ArticleSource.PASTE, MockLLM(), retriever=None)

    assert a.title == "路径-目标理论：让下属看清路", "标题必须取首行"
    for s in a.summary:
        assert "允许" not in s and "【" not in s and "代码" not in s, f"摘要被提示污染：{s}"
    assert a.domains, "正文命中了关键词，应有域命中"


class _BoomRetriever:
    """检索必炸，用于验证检索失败不会牵连解析主流程。"""

    def search(self, *a, **k):
        raise RuntimeError("boom")

    def dual_recall(self, *a, **k):
        raise RuntimeError("boom")

    def by_ids(self, *a, **k):
        raise RuntimeError("boom")

    def reload(self, *a, **k):
        raise RuntimeError("boom")


def test_retriever_failure_degrades_instead_of_breaking_analysis():
    """检索只是增益，挂了不该让整次解析失败。"""
    a = analyze_article("正文。" * 40, ArticleSource.PASTE,
                        _FakeLLM(_GOOD_PAYLOAD), retriever=_BoomRetriever())

    assert a.feasible is True, "检索挂了也要给出成课判断"
    assert a.related_units == [], "检索不可用时应为空，而不是抛异常"
    assert a.title == _GOOD_PAYLOAD["title"], "主流程产物必须完好"
    assert a.summary == _GOOD_PAYLOAD["summary"]


def test_prompt_uses_sentinel_around_body():
    llm = _FakeLLM(_GOOD_PAYLOAD)
    analyze_article("正文。" * 40, ArticleSource.PASTE, llm, retriever=None)
    assert "【正文】" in llm.user and "【正文结束】" in llm.user, "缺少哨兵则 Mock 无法切出正文"
    assert llm.purpose == "analyze_article"


# ---------------------------------------------------------------------------
# D. 模型：注入需求单的空值保护
# ---------------------------------------------------------------------------
def _need() -> TrainingNeed:
    return TrainingNeed(audience="手填受众", audience_level=5, headcount=99,
                        duration_minutes=480, business_pain="手填痛点",
                        desired_behavior="手填行为")


def test_fill_overrides_only_provided_fields():
    a = ArticleAnalysis(source=ArticleSource.PASTE, title="t",
                        summary=["s"], feasible=True, feasibility_reason="r",
                        suggested_duration=120)  # 只给了时长
    need = a.fill_training_need(_need())

    assert need.duration_minutes == 120, "给了建议就必须覆盖"
    assert need.headcount == 99, "没给建议就必须保留手填值"
    assert need.audience_level == 5
    assert need.audience == "手填受众"
    assert need.business_pain == "手填痛点"


def test_fill_with_nothing_keeps_everything():
    a = ArticleAnalysis(source=ArticleSource.URL, title="t", summary=["s"],
                        feasible=False, feasibility_reason="r")
    need = a.fill_training_need(_need())

    assert (need.audience, need.headcount, need.duration_minutes) == ("手填受众", 99, 480)
    assert need.audience_level == 5
    assert need.business_pain == "手填痛点" and need.desired_behavior == "手填行为"


def test_fill_overrides_all_when_all_given():
    a = ArticleAnalysis(source=ArticleSource.FILE, title="t", summary=["s"],
                        feasible=True, feasibility_reason="r",
                        audience_reco="新任经理", suggested_theme="权变领导",
                        suggested_pain="p", suggested_behavior="b",
                        suggested_duration=60, suggested_headcount=20,
                        suggested_level=1)
    need = a.fill_training_need(_need())

    assert need.audience == "新任经理"
    assert need.theme_hint == "权变领导"
    assert (need.duration_minutes, need.headcount, need.audience_level) == (60, 20, 1)


# ---------------------------------------------------------------------------
# E. 端点
# ---------------------------------------------------------------------------
def test_api_analyze_paste_and_error_contract():
    from fastapi.testclient import TestClient

    import app.api.main as main
    from app.api.main import app
    from app.services.context import build_context

    ctx = build_context(knowledge_dir=K_DIR,
                        output_dir=Path(tempfile.mkdtemp()))
    ctx.llm = MockLLM()          # 强制离线，不依赖 API Key，也不改环境变量
    main.ctx = ctx
    client = TestClient(app)

    body = "情境领导：风格需随准备度调整。\n" + "有效的领导取决于风格与准备度的匹配。\n" * 40
    r = client.post("/api/v1/analyze", json={"text": body})
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "paste"
    assert d["course_type_name"] == COURSE_TYPES[d["course_type"]]["name"], "课型中文名须由后端给出"
    assert d["raw_chars"] > 0

    # 都给 / 都不给 → 400
    assert client.post("/api/v1/analyze",
                       json={"text": "a", "url": "https://x.com"}).status_code == 400
    assert client.post("/api/v1/analyze", json={}).status_code == 400
    # 空文本 → 400 且带明确错误码
    r = client.post("/api/v1/analyze", json={"text": "  "})
    assert r.status_code == 400 and "empty_text" in r.json()["detail"]
    # 内网 URL → 403（在发起请求前拦截）
    r = client.post("/api/v1/analyze", json={"url": "http://127.0.0.1:9/x"})
    assert r.status_code == 403 and "forbidden_host" in r.json()["detail"]


def test_api_analyze_file_upload_and_formats():
    from fastapi.testclient import TestClient

    import app.api.main as main
    from app.api.main import app
    from app.services.context import build_context

    ctx = build_context(knowledge_dir=K_DIR, output_dir=Path(tempfile.mkdtemp()))
    ctx.llm = MockLLM()
    main.ctx = ctx
    client = TestClient(app)

    body = ("目标管理与 SMART 原则。\n"
            "目标要具体可衡量可实现相关有时限。\n" * 30).encode("utf-8")
    r = client.post("/api/v1/analyze/file",
                    files={"file": ("a.md", body, "text/markdown")})
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "file"
    assert d["raw_chars"] > 0

    r = client.post("/api/v1/analyze/file",
                    files={"file": ("x.pptx", b"fake", "application/octet-stream")})
    assert r.status_code == 415 and "unsupported_format" in r.json()["detail"]

    r = client.post("/api/v1/analyze/file", files={"file": ("e.txt", b"", "text/plain")})
    assert r.status_code == 400 and "empty_file" in r.json()["detail"]
