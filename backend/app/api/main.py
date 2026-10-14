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
            "knowledge_units": len(c.retriever.units)}


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


# Web 工作台静态挂载：API 路由均注册在前，优先匹配；其余路径回落 web/index.html
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
