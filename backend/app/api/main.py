"""FastAPI 服务：课程设计生成器 API + Web 工作台（P2）。

端点：
- GET  /healthz
- POST /api/v1/runs                     发起一次课程设计（命中人工节点时返回 awaiting_human + interrupt）
- POST /api/v1/runs/{run_id}/resume     注入人工决定（Command(resume=...)）
- GET  /api/v1/runs                     运行记录列表（扫描 runs 目录）
- GET  /api/v1/runs/{run_id}            查询运行状态
- GET  /api/v1/runs/{run_id}/package.md 下载课程包 Markdown
- GET  /api/v1/kb/units                 知识单元检索（域/困境/关键词）
- GET  /api/v1/archetypes               课程原型列表
- POST /api/v1/kb/ingest                提交语料，跑知识流水线（P1）
- GET  /api/v1/kb/runs                  知识流水线运行记录（草稿审核台）
- GET  /api/v1/kb/runs/{run_id}         草稿详情
- POST /api/v1/kb/runs/{run_id}/review  审核决定（批准入库/打回）
- GET  /api/v1/kb/corpus                L1 语料层列表
- GET  /api/v1/kb/taxonomy              受控代码表（表单下拉数据源）
- GET  /                               Web 工作台（mgmt-kb-agent/web/ 静态目录）
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..domain.models import TrainingNeed
from ..services.context import AppContext, build_context

# Web 工作台静态目录：mgmt-kb-agent/web（相对本文件 backend/app/api/main.py 的上三级）
WEB_DIR = Path(__file__).resolve().parents[3] / "web"

ctx: AppContext | None = None


def _ctx() -> AppContext:
    """懒加载兜底：lifespan 未触发（如部分测试客户端）时按需构建。"""
    global ctx
    if ctx is None:
        ctx = build_context()
    return ctx


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ctx
    ctx = build_context()
    yield


app = FastAPI(title="管理学知识库 Agent · 课程设计生成器", version="0.2.0", lifespan=lifespan)

# CORS：Web 工作台既可同源（挂载于本服务）也可独立打开（预览面板/其他端口）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ResumeBody(BaseModel):
    decision: dict


@app.get("/healthz")
def healthz():
    c = _ctx()
    return {"status": "ok", "llm": c.pipeline.deps.llm.name,
            "knowledge_units": len(c.retriever.units),
            "vector_mode": c.vector_mode, "vector_note": c.vector_note}


@app.post("/api/v1/runs")
def create_run(need: TrainingNeed):
    c = _ctx()
    result = c.pipeline.run(need)
    return result.to_dict() | {"detail": result.detail}


@app.post("/api/v1/runs/{run_id}/resume")
def resume_run(run_id: str, body: ResumeBody):
    c = _ctx()
    result = c.pipeline.resume(run_id, body.decision)
    return result.to_dict() | {"detail": result.detail}


@app.get("/api/v1/runs")
def list_runs(limit: int = 50):
    """运行记录：扫描 runs 目录，按时间倒序。"""
    c = _ctx()
    rows = []
    for d in sorted(c.output_dir.glob("run-*"), reverse=True):
        st_file = d / "state.json"
        if not st_file.exists():
            continue
        try:
            st = json.loads(st_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        pkg_file = d / "package.json"
        pkg = None
        if pkg_file.exists():
            try:
                pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
            except Exception:
                pkg = None
        need = (pkg or {}).get("need") or {}
        rows.append({
            "run_id": st.get("run_id", d.name),
            "status": st.get("status"),
            "updated_at": st.get("updated_at"),
            "audience": need.get("audience"),
            "duration_minutes": need.get("duration_minutes"),
            "industry": need.get("industry"),
            "modules": len(st.get("modules") or []),
            "has_package": pkg is not None,
        })
        if len(rows) >= limit:
            break
    return rows


@app.get("/api/v1/runs/{run_id}")
def get_run(run_id: str):
    c = _ctx()
    result = c.pipeline.get(run_id)
    if result is None:
        raise HTTPException(404, f"run {run_id} 不存在")
    return result.to_dict() | {"detail": result.detail}


@app.get("/api/v1/runs/{run_id}/package.md")
def get_package_md(run_id: str):
    c = _ctx()
    md_file = c.output_dir / run_id / "course-package.md"
    if not md_file.exists():
        raise HTTPException(404, "课程包尚未生成（流水线未完成）")
    return PlainTextResponse(md_file.read_text(encoding="utf-8"))


@app.get("/api/v1/kb/units")
def kb_units(domain: str | None = None, dilemma: str | None = None, q: str | None = None):
    c = _ctx()
    units = c.store.load_units()
    if domain:
        units = [u for u in units if domain in u.domains]
    if dilemma:
        units = [u for u in units if dilemma in u.dilemmas]
    if q:
        units = c.retriever.search(q, domains=[domain] if domain else None,
                                   dilemmas=[dilemma] if dilemma else None, top_k=10)
    return [{"id": u.id, "title": u.title, "type": u.type.value, "domains": u.domains,
             "dilemmas": u.dilemmas, "evidence_level": u.evidence_level.value,
             "contested": u.contested, "misconceptions": [x.claim for x in u.misconceptions],
             "source": f"{u.source.author}《{u.source.book}》{u.source.locator}",
             "key_points": u.key_points} for u in units]


@app.get("/api/v1/archetypes")
def archetypes():
    from ..graph.archetypes import ARCHETYPES
    return [{"key": a.key, "title": a.title, "dilemmas": a.dilemma_tags,
             "modules": [{"title": m["title"], "minutes": m["minutes"],
                          "bucket_mix": m["bucket_mix"]} for m in a.modules]}
            for a in ARCHETYPES.values()]


# ---------------------------------------------------------------------------
# 知识流水线（P1）：语料采集 → 加工草稿 → 质检 → 人工审核 → 入库
# ---------------------------------------------------------------------------
class CorpusBody(BaseModel):
    book: str
    author: str
    year: int | None = None
    locator: str
    copyright_status: str = "要点化"
    text: str


class ReviewBody(BaseModel):
    action: str  # approve | reject
    note: str = ""
    reviewer: str = "reviewer"


@app.get("/api/v1/kb/taxonomy")
def kb_taxonomy():
    """受控代码表：域/困境/证据等级/版权状态（审核台表单下拉数据源）。"""
    from ..domain.constants import COMPETENCY_DOMAINS, DILEMMA_INDEX, EVIDENCE_LEVELS
    return {
        "domains": [{"code": k, "name": v["name"]} for k, v in COMPETENCY_DOMAINS.items()],
        "dilemmas": [{"code": k, "name": v} for k, v in DILEMMA_INDEX.items()],
        "evidence_levels": [{"code": k, "name": v} for k, v in EVIDENCE_LEVELS.items()],
        "copyright_statuses": ["公版", "授权", "要点化", "原创"],
    }


@app.post("/api/v1/kb/ingest")
def kb_ingest(body: CorpusBody):
    """提交一条语料，跑采集→加工→质检；质检通过则挂起等待人工审核。"""
    from ..domain.models import CorpusChunk
    c = _ctx()
    chunk = CorpusChunk(id="CC-0000", **body.model_dump())
    result = c.kb_pipeline.run(chunk)
    return result.to_dict()


@app.get("/api/v1/kb/runs")
def kb_runs(limit: int = 50):
    """知识流水线运行记录（草稿审核台列表）。"""
    return _ctx().kb_pipeline.list_runs(limit)


@app.get("/api/v1/kb/runs/{run_id}")
def kb_run(run_id: str):
    result = _ctx().kb_pipeline.get(run_id)
    if result is None:
        raise HTTPException(404, f"知识流水线 run {run_id} 不存在")
    return result.to_dict()


@app.post("/api/v1/kb/runs/{run_id}/review")
def kb_review(run_id: str, body: ReviewBody):
    """审核决定：批准（分配正式编号入库）/ 打回（携修改意见）。"""
    if body.action not in ("approve", "reject"):
        raise HTTPException(422, "action 必须为 approve 或 reject")
    result = _ctx().kb_pipeline.resume(run_id, body.model_dump())
    return result.to_dict()


@app.get("/api/v1/kb/corpus")
def kb_corpus():
    """L1 语料层列表。"""
    c = _ctx()
    return [{"id": ch.id, "book": ch.book, "author": ch.author, "year": ch.year,
             "locator": ch.locator, "copyright_status": ch.copyright_status,
             "status": ch.status, "text_preview": ch.text[:120]}
            for ch in c.corpus_store.load_chunks()]


# Web 工作台静态挂载：API 路由均注册在前，优先匹配；其余路径回落 web/index.html
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
