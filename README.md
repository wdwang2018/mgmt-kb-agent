# 管理学知识库 Agent（mgmt-kb-agent）

面向管理培训的知识库 Agent。**双角色设计**：既是知识库的「建设者」（采集→加工→质检→人工审核流水线），也是管理培训的「陪跑者」。一期主打场景：**课程设计生成器**（输入培训需求 → 输出可直接开课的课程包）。

技术路线：自研 LangGraph 状态机 + FastAPI；知识组织为「能力模型 × 困境索引」三层结构；四道自动质量门禁 + 人工审核（HITL）；知识单元全部带引用定位，无据不生成。

## 架构

```
┌──────────────────────────────────────────────────────────────────┐
│  Web 工作台（web/，挂载于 FastAPI 根路径）      FastAPI（backend/app/api）│
└───────────────────────┬──────────────────────────────────────────┘
                        │ HTTP（同源，CORS 已开）
┌───────────────────────▼──────────────────────────────────────────┐
│  LangGraph 状态机（backend/app/graph）                             │
│                                                                   │
│  需求解析 → 能力差距分析 ⇈HITL → 学习目标 → 模块规划 → 内容匹配     │
│      → 活动编排 → 评估设计 → 质量门禁 ⇣(重试≤4次/转人工) → 打包输出 │
│                                                                   │
│  门禁：G1 建设性对齐 / G2 强制溯源 / G3 70-20-10 / G4 认知冲突      │
└───────────┬──────────────────────┬───────────────────────────────┘
            │ 端口（Protocol）      │
┌───────────▼──────────┐  ┌────────▼─────────┐  ┌──────────────────┐
│ LLMClient            │  │ Retriever        │  │ KnowledgeStore   │
│ · MockLLM（默认）     │  │ · MemoryRetriever│  │ · JsonKnowledge  │
│ · DeepSeekLLM / GLMLLM（在线） │  │ · Milvus（P1）   │  │   Store（P1: PG）│
└──────────────────────┘  └──────────────────┘  └──────────────────┘
```

端口-适配器架构：图编排与领域层只依赖 `app/ports.py` 的 Protocol，换模型/换检索引擎/换存储不动图。

### 两条流水线（P0 课程设计 + P1 知识生产）

```
┌─────────────────────────── P1 知识流水线（backend/app/graph/knowledge_*） ───────────────────────────┐
│  语料采集(L1 corpus/) → 规则/LLM 抽取草稿 → K1-K5 质检 → 人工审核 ⇈HITL → 分配正式编号入库(KU-xxx)   │
│  质检：K1 溯源完整 / K2 代码合法 / K3 要点化(防整段复制) / K4 误区结构 / K5 唯一性(Jaccard≥0.85)      │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                    │ 入库即生效
┌─────────────────────────── P0 课程设计生成器（backend/app/graph/） ──────────────────────────────────┐
│  需求解析 → 能力差距分析 ⇈HITL → 学习目标 → 模块规划 → 内容匹配 → 活动编排 → 评估设计                  │
│      → 质量门禁 ⇣(重试≤4次/转人工) → 打包输出                                                        │
│  门禁：G1 建设性对齐 / G2 强制溯源 / G3 70-20-10 / G4 认知冲突                                         │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

知识闭环：**采集 → 加工 → 质检 → 人工审核 → 入库 → 课程生成消费 → 学员反馈回流修正**（审核台账 `knowledge/reviews/reviews.jsonl`）。

## 目录结构

```
mgmt-kb-agent/
├── backend/
│   ├── app/
│   │   ├── domain/            # 领域内核：constants（六域×L1-L5、24 困境）、models（pydantic）
│   │   │   └── schemas/       # 导出的 JSON Schema（三个核心工件）
│   │   ├── ports.py           # 端口协议（LLM/检索/知识库/嵌入/图谱/元数据）
│   │   ├── adapters/          # Mock/DeepSeek/GLM LLM、内存/Milvus/混合检索、JSON 知识库、JSON 语料层、Kùzu 图谱、PG 元数据
│   │   ├── graph/             # 九节点(课程设计) + 五节点(知识流水线) + 四门禁/五质检 + HITL + checkpoint 工厂（sqlite 持久化）
│   │   ├── services/          # 课程设计流水线 + 知识流水线 + 渲染 + 组合根
│   │   └── api/               # FastAPI 路由（含 kb 流水线端点）+ security.py（P0 安全）+ observability.py/middleware.py（P2 可观测性）
│   ├── observability/         # P2 可观测性：结构化日志 + 进程指标 + LLM 计时包装
│   ├── scripts/               # run_demo / export_schemas / probe_langgraph
│   └── tests/                 # 144 个测试（门禁/检索/原型/流水线/API/LLM 输出形状/知识流水线/安全/检查点/可观测性）
├── knowledge/
│   ├── units/                 # 知识单元（JSONL；lead_draft/strat_draft 为流水线入库文件）
│   ├── corpus/                # L1 语料层（带页码定位，CC 编号）
│   ├── cases/                 # 5 个中国情境案例卡
│   ├── reviews/               # 人工审核台账（runs 联动）
│   └── books/                 # 38 本核心书目清单（要点化状态跟踪）
├── eval/
│   ├── golden/                # 黄金评测集（12 需求 + 12 检索查询）
│   └── run_eval.py            # 评测脚本 → report-<日期>.md
├── web/                       # Web 工作台（单页应用，FastAPI 静态挂载）
│   └── index.html             # 课程设计 / 知识库 / 运行记录 / 知识流水线 四个页签
├── .github/workflows/ci.yml   # CI：pytest + 黄金评测 +（配了 Hook 才执行）Render 自动部署
├── render.yaml                # Render 部署蓝图（Blueprint 一键部署）
├── requirements.txt
└── runs/                      # 运行产物（课程包 + kb-runs 草稿记录；已 gitignore）
```

## 快速开始（本机实测命令）

```bash
PY="C:/Users/DELL/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
# 依赖已装在该受管 venv（langgraph 1.2.15 / fastapi / pydantic / pytest / httpx）
# 向量检索：pymilvus 3.0.2 + milvus-lite 3.2.2；图查询：kuzu 0.11.3（均 Windows 实测可用，无需 Docker）

# 1. 测试（107 个）
cd backend && "$PY" -m pytest

# 2. Demo：内置真实需求 → 完整课程包（Mock 模式，无需 API Key，秒出）
"$PY" scripts/run_demo.py
# 产物：runs/<run_id>/course-package.md + package.json + state.json

# 3. HITL 交互模式（人工挂起点手动确认/打回）
"$PY" scripts/run_demo.py --interactive

# 4. API 服务
"$PY" -m uvicorn app.api.main:app --port 8000
# POST /api/v1/runs              发起课程设计（命中人工节点返回 awaiting_human）
# POST /api/v1/runs/{id}/resume  注入人工决定 {"decision": {"action": "approve"}}
# GET  /api/v1/runs/{id}         查询状态
# GET  /api/v1/runs/{id}/package.md  下载课程包
# GET  /api/v1/kb/units?domain=STRAT&q=战略共识  知识检索
# GET  /api/v1/archetypes        课程原型清单

# P1 知识流水线（审核台 API）：
# POST /api/v1/kb/ingest         提交语料 {book,author,year,locator,copyright_status,text}
# GET  /api/v1/kb/runs           草稿审核台账
# GET  /api/v1/kb/runs/{id}      草稿详情（含质检明细）
# POST /api/v1/kb/runs/{id}/review  审核决定 {"action":"approve"|"reject","note":"..."}
# GET  /api/v1/kb/corpus         L1 语料层列表
# GET  /api/v1/kb/taxonomy       受控代码表（六域/24 困境/证据等级/版权状态）
# GET  /api/v1/kb/graph/{unit_id}  知识图谱查询（前置链/提出者/学派/争议/案例/邻居）
# GET  /api/v1/kb/graph-stats    图规模统计

# 5. 黄金评测（改模型/改门禁后必跑，不达标不上线）
cd .. && "$PY" eval/run_eval.py

# 6. Web 工作台（启动 API 后浏览器访问）
"$PY" -m uvicorn app.api.main:app --port 8000
# 打开 http://127.0.0.1:8000/ 即是工作台（无需单独起前端）
```

### API 最小闭环示例

```bash
# 发起（自动批准模式下直接 completed；interrupt 模式停在 awaiting_human）
curl -X POST http://127.0.0.1:8000/api/v1/runs -H "Content-Type: application/json" -d '{
  "audience": "新任经理", "audience_level": 2, "headcount": 20, "duration_minutes": 180,
  "business_pain": "新经理埋头做事，下属得不到成长",
  "desired_behavior": "会授权、会给反馈", "industry": "互联网",
  "theme_hint": "新任管理者转型"}'
# → {"run_id": "run-...", "status": "awaiting_human", "interrupt": {"type": "confirm_targets", ...}}

# 人工确认后恢复
curl -X POST http://127.0.0.1:8000/api/v1/runs/run-xxx/resume \
  -H "Content-Type: application/json" -d '{"decision": {"action": "approve"}}'

# 取课程包
curl http://127.0.0.1:8000/api/v1/runs/run-xxx/package.md
```

### 知识流水线最小闭环示例（P1）

```bash
# 提交语料 → 质检通过则返回 awaiting_human（携草稿与 K1-K5 明细）
curl -X POST http://127.0.0.1:8000/api/v1/kb/ingest -H "Content-Type: application/json" -d '{
  "book": "成为管理者", "author": "Linda A. Hill", "year": 2003,
  "locator": "第1章", "copyright_status": "要点化",
  "text": "新经理晋升后仍然埋头做事……（要点化摘要，勿整段复制）"}'
# → {"run_id": "kb-...", "status": "awaiting_human", "draft": {...}, "quality": {...}}

# 批准入库（分配 KU-LEAD-013 正式编号，知识库检索即时生效）
curl -X POST http://127.0.0.1:8000/api/v1/kb/runs/kb-xxx/review \
  -H "Content-Type: application/json" -d '{"action": "approve", "reviewer": "knowledge-editor"}'

# 或打回（携修改意见，修正语料后可重新提交）
curl -X POST http://127.0.0.1:8000/api/v1/kb/runs/kb-xxx/review \
  -H "Content-Type: application/json" -d '{"action": "reject", "note": "要点需补充证据等级依据"}'
```

### 输入怎么写（需求诊断单七要素）

| 字段 | 说明 | 示例 |
|---|---|---|
| `audience` | 培训对象 | 新任经理（晋升 6 个月内） |
| `audience_level` | 1-5 当前职级/能力层 | 2 |
| `headcount` | 人数 | 24 |
| `duration_minutes` | 总时长（分钟，15 的倍数最佳） | 360 |
| `business_pain` | 业务痛点（具体场景，越具体检索越准） | 跨部门协作全部卡在经理这里 |
| `desired_behavior` | 期望的行为改变 | 会授权、会给反馈、坚持 1on1 |
| `industry` | 行业情境 | 互联网 |
| `theme_hint` | 主题倾向（可选） | 新任管理者转型 / 战略共识与解码 |

切换在线大模型（内容深度由在线模型承担，支持 DeepSeek / GLM 两种）：

```bash
# 方式一：DeepSeek
export LLM_MODE=deepseek
export DEEPSEEK_API_KEY=sk-xxx
# 可选：DEEPSEEK_BASE_URL / DEEPSEEK_MODEL

# 方式二：GLM（智谱，OpenAI 兼容协议）——本机已实测跑通
export LLM_MODE=glm
export GLM_API_KEY=<your-glm-api-key>
export GLM_MODEL=glm-5          # 默认 glm-4-plus；glm-5 质量高，glm-4-air 更快更省
# 可选：GLM_BASE_URL（默认 https://open.bigmodel.cn/api/paas/v4）
```

> ⚠️ **在线模式必须后台跑**：glm-5 单次调用约 6 秒，一轮流水线约 30-35 次调用 ≈ 12-14 分钟，前台跑会被工具超时杀掉。黄金评测 12 个需求全量在线跑约 3 小时，建议 `GLM_MODEL=glm-4-air` 或择时后台跑。

## 九节点状态机与四道门禁

| 节点 | 职责 | 关键机制 |
|---|---|---|
| 需求解析 | 结构化需求诊断单 | 自由文本解析（在线 LLM）/ 规则补全（Mock） |
| 能力差距分析 | 痛点 → 能力目标 + 困境标签 | 原型标签 + 关键词增强；**后挂人工确认** |
| 学习目标生成 | 布鲁姆层级可测量目标 | L1-L5 行为锚定 |
| 模块规划 | 15 分钟粒度时间轴 | 原型缩放守恒；**目标全覆盖保证**；小桶折叠 |
| 内容匹配检索 | 理论要点 + 引用 | 能力域 × 困境双路召回；跨模块去重 |
| 教学活动编排 | 70-20-10 分桶 | 桶时长即活动时长；>90 分钟自动分段 |
| 评估设计 | SJT + 案例讨论 + 量规 | 每题挂误区对应的理论纠正 |
| 质量门禁 | G1-G4 自动质检 | 不过重试 ≤4 次，耗尽转人工签发 |
| 打包输出 | 课程包 + 讲义 Markdown | 落盘 runs/，审核台账 |

门禁规则：**G1** 每个学习目标必须同时挂教学活动与评估题；**G2** 每个理论要点/评估题必须对应已入库单元 + 引用定位；**G3** 实践 ≥30%、人际 ≥20%、正式 ≤50%；**G4** 每个模块至少一处「常见误区 vs 理论证据」。

## Web 工作台（P2 已落地，P1 扩充第四页签）

单页应用（`web/index.html`），由 FastAPI 静态挂载于根路径，同源零 CORS 问题；独立打开时回落 `http://127.0.0.1:8000`（页面横幅可改基址）。

| 页签 | 功能 |
|---|---|
| **课程设计** | 需求诊断单表单（七要素 + 时长快捷档）→ 九节点流水线进度条 → 人工审核卡片（能力目标确认/门禁升级，批准或带反馈打回）→ 课程包 Markdown 渲染 + 一键下载 |
| **知识库** | 知识单元检索：关键词 + 领域 + 证据等级（A/B/C）筛选；卡片含引用定位、学员常见误区、学派争议标记；**图谱关系面板**（GraphRAG） |
| **运行记录** | 历史 run 列表（状态/对象/时长/模块数），点击回看课程包 |
| **知识流水线**（P1） | 语料采集表单 → 五节点进度 → 草稿审核卡片（K1-K5 明细、批准/打回）→ 草稿台账 + L1 语料层；待审数量实时徽标 |

配套 API：`GET /api/v1/runs`、`GET /api/v1/runs/{id}`（补全计数字段，刷新后统计不丢）、`GET /api/v1/kb/units`（含误区）、kb 流水线六端点（见上文）、CORS 全开、静态挂载（API 路由优先，不被遮蔽）。

**知识库页的图谱关系面板**（2026-10-17 接入）：搜索栏右侧实时显示图规模（`图谱（kuzu）：26 单元 · 13 学者 · 9 观点 · 103 边`）；每张单元卡片底部挂「🕸 图谱关系」折叠区，**前 5 条结果自动展开**、其余点击懒加载（结果按 unit_id 缓存，不重复请求）。面板按行展示：前置概念链（`A ← B ← 本单元`；入口概念显示"被 N 个单元引用"）、提出者、学派、⚔ 争议观点（CONTESTS/COMPARES）、应用案例、相关单元（RELATED，自动剔除与前置重复项）。前置/相关 KU 编号 chip 可点击：命中当前结果集则平滑滚动定位 + 高亮闪烁，未命中则回填搜索框触发检索。`GRAPH_MODE=none` 时面板整体隐藏，检索功能不受影响。

**浏览器实测（2026-10-15，Playwright 真实驱动）**：
- 课程设计链路：表单 → 生成 → 人工确认卡片（能力目标表格正确渲染）→ 批准 → 完成面板（7 模块/19 活动/10 评估题/9 引用、四门禁 ✓），0 JS 错误；
- 知识流水线链路：语料提交 → DR-0001 草稿 → K1-K5 五项全过 → 批准入库 KU-LEAD-013 → 知识库 24→25 即时生效 → 台账/语料 extracted → 徽标清零，10/10 PASS。

**浏览器实测（2026-10-17，图谱面板接入）**：26 张卡片全部挂载图谱面板、前 5 自动展开（KU-LEAD-002 正确显示前置 KU-LEAD-001 + 争议「先规划后行动的理性决策观」+ 提出者 Herminia Ibarra）；懒加载路径（第 7 张点击展开）正常；KU 编号 chip 跳转从列表底部（scrollTop 3270）滚回目标卡（1442）并闪烁高亮；相关单元去重生效；图规模徽标正确；0 JS 错误。

## 知识组织（三个核心工件）

1. **能力模型**：六域（战略思维/团队领导/变革管理/沟通影响/教练辅导/执行落地）× L1-L5 行为锚定 → `domain/schemas/competency_model.schema.json`
2. **困境索引 v1**：24 个管理困境标签（D01-D24），知识按困境挂接 → `domain/schemas/dilemma_index.schema.json`
3. **知识单元**：概念/理论/模型/工具/研究结论，含证据等级（A 实证强支持/B 情境依赖/C 经验争议）、误区、关系（提出者/学派/争议/前置/应用）→ `domain/schemas/knowledge_unit.schema.json`

设计取舍：按「管理困境」而非「名词」索引——学员带着困境来，不是带着「我想学波特五力」来。

## P0 验收映射（实测结果，2026-10-14）

| PRD P0 验收标准 | 实测 | 结论 |
|---|---|---|
| 30-50 本核心书目 + 两主题要点化 | 38 本书目清单；24 个知识单元要点化（12+12） | ✅ |
| 图谱 Schema | 三个 JSON Schema 导出 + 关系字段落地 | ✅ |
| 黄金评测集 | 24 条（12 需求 + 12 查询） | ✅ |
| 问答/生成 Demo 可跑通 | 端到端 Demo + API + 29 测试全过 | ✅ |
| 引用准确率 ≥90% | 溯源覆盖率 100%，recall@5 100% | ✅ |
| 幻觉抽检 <5% | 无据不生成（G2 强制溯源，引用单元存在性校验） | ✅ |
| 完成率 / 门禁通过率 | 12/12 需求 100% 完成，G1-G4 100% 通过 | ✅ |
| 域/困境识别率 ≥80% | 均 100% | ✅ |

## 已验证的真实运行记录

- `pytest`：**107 passed**（课程设计全链路 + 知识流水线全链路 + 向量/混合检索层 + 图查询层 + .env 加载）
- `eval/run_eval.py`：完成率 100%、门禁通过率 100%、recall@5 100%、溯源覆盖率 100%、建设性对齐率 100%、域识别 100%、困境识别 100%
- 知识流水线浏览器实测（Playwright 真实驱动）：语料提交 → DR-0001 草稿 → K1-K5 五项全过 → 批准入库 KU-LEAD-013 → 知识库 24→25 即时生效 → 台账/语料 extracted → JS 零错误，10/10 PASS
- 修复并回归的真实缺陷：LangGraph interrupt 语义（实测后实现）、检查点 msgpack 白名单（含 KnowledgeUnit 内部三个枚举）、缩放舍入漂移、活动阈值丢弃导致 G3 失真、`normalize_mix` 死循环（过期快照值 + 零桶乒乓）、pydantic 列表拷贝导致域增强丢失、增强域目标无模块承载导致 G1 失败、Mock 抽取极短语料击穿 pydantic min_length、在线抽取脏 shape 构造前回退、`save_unit` 空目录崩溃、知识流水线 reject 路径缺终态节点

## Mock LLM 的诚实边界

默认 `LLM_MODE=mock` 是**确定性模板桩**：保证无 API Key 下端到端可运行、可测试、可回归；它只做结构化装配与模板措辞，**事实性内容全部来自检索到的真实知识单元**（引用、误区、证据等级均为真）。内容的语言质量、情境贴合度由在线模型（`LLM_MODE=deepseek` 或 `LLM_MODE=glm`）承担，切换后需重跑 `eval/run_eval.py` 复测。知识流水线的「加工」节点同理：Mock 走关键词+句子切分规则抽取，在线走 LLM 萃取，两条路都过同一套 K1-K5 质检。

## 检索与向量库（Milvus 混合检索）

检索层按「端口-适配器」设计，通过环境变量切换，**图编排层与 Web 前端零改动**。

| 环境变量 | 取值 | 说明 |
|---|---|---|
| `VECTOR_MODE` | `memory`（默认）/ `milvus` / `hybrid` | 检索实现：纯 BM25 / 纯稠密向量 / **双路召回 + RRF 融合（推荐）** |
| `EMBED_MODE` | `mock`（默认）/ `glm` | Embedding 实现：确定性伪向量 / 智谱 `embedding-3` |
| `EMBED_DIM` | `256`（mock）/ `2048`（glm） | 向量维度；**Milvus 集合按 dim 建，变更需重建集合** |
| `MILVUS_URI` | 默认 `milvus_kb.db` | Milvus Lite 本地文件（**无需 Docker**） |
| `MILVUS_COLLECTION` | 默认 `kb_units` | 集合名 |
| `RRF_K` | 默认 `60` | RRF 融合平滑系数 |
| `RRF_DENSE_WEIGHT` | 默认 `2.0` | 稠密侧权重（**实测必须 >1，见下**） |

```bash
# 默认 memory 模式（离线可跑，零向量依赖）
"$PY" -m uvicorn app.api.main:app --port 8932

# 切 Milvus 向量模式（先用无 Key 的确定性向量验证链路）
VECTOR_MODE=milvus "$PY" -m uvicorn app.api.main:app --port 8932

# 混合检索（推荐）：BM25 + 稠密向量双路召回，RRF 融合
VECTOR_MODE=hybrid EMBED_MODE=glm "$PY" -m uvicorn app.api.main:app --port 8932
```

**为什么默认不是 Milvus**：26 个单元规模下 BM25 召回够用，向量库的真实收益只有两点——① **语义召回**（「授权/赋能/放权」词面不同但语义近，BM25 召回不到）；② 规模到千级单元后的检索效率。默认保留 memory 是为了「无 API Key、无向量依赖」下端到端仍可跑、可测。

### 实测结论：混合检索 + 稠密侧加权（2026-10-17）

三模式在两组查询上的 MRR（完整数据见 `eval/retrieval-bench-2026-10-17.md`）：

| 查询组 | 纯 BM25 | 纯向量 | **混合（wd=2）** |
|---|---|---|---|
| A 黄金集 12 条（含 GROW/SBI/VRIO 等专有名词） | 1.000 | 1.000 | **1.000** |
| B 语义改写 6 条（用户真实口吻） | 0.650 | 0.875 | **0.917** |

三条关键结论：

1. **等权融合是错的**。`wb:wd = 1:1` 时 B 组 MRR 只有 0.833，**反而低于纯向量的 0.875**——BM25 会把语义相近但非目标的单元顶到前列，等权融合被这种「伪关键词命中」拖累。
2. **稠密侧权重 ≥1.5 后混合才真正同时优于两条单路线**（B 组 MRR 0.917 > 0.875 > 0.650，且 R@3 达 1.000），A 组维持 1.000 不受影响。1.5~3+ 是效果平台期，故默认取 **2.0** 居中。
3. **代价是延迟**：纯 BM25 约 1ms，向量/混合约 300ms（每查询一次在线 embedding 往返）。

> ⚠️ 诚实边界：B 组仅 6 条人工构造查询，0.833 与 0.917 的差距约等于 1~2 条查询的翻转，**样本量小，不宜过度解读**；权重取值依据的是「平台期内结论稳定」而非最优点拟合。

**三条工程约定（都是踩坑后的设计）**：

1. **md5 而非内置 `hash()`** 生成确定性伪向量（`embedding_mock.py`）——Python 内置 hash 受 `PYTHONHASHSEED` 随机化影响，跨进程不稳定，会导致同一文本两次 upsert 得到不同向量。
2. **Embedding 异常不抛出，退回伪向量**（`embedding_glm.py`，沿用项目 `llm_glm` 的既有约定）——网络/鉴权/维度不一致都不阻断「入库即索引」链路；真实错误记在 `last_error` 供排查。
3. **向量侧故障自动降级并记原因**（`retrieval_hybrid.py`）——构造失败降级为纯 memory，运行期 search/reload 失败当次退回 BM25 并记 `degrade_note`；降级状态经 `/healthz` 的 `vector_mode` / `vector_note` 暴露，避免「以为在用向量检索、其实已静默退化」的假象。

**索引维护机制**（三种模式共用同一条链路，调用方零感知）：

```
commit_draft 入库成功 → deps.on_commit(unit.id) → ctx.reindex()
                      → retriever.reload(store.load_units())
                      → memory: 全量重建 BM25 统计量
                        milvus/hybrid: 差值 upsert + 删除陈旧 ID（幂等，重复 reload 不写脏）
```

即 `knowledge/units/*.jsonl` 是**唯一真相源**，向量库只是可重建的派生索引（进程重启由 `_ensure_collection` 兜底；集合已存在且 dim 变更时自动 drop 重建）。

> ⚠️ `milvus_kb.db` 是二进制产物，已在 `.gitignore` 中排除，**切勿提交到仓库**。

## 知识图谱与持久化（GraphRAG / PostgreSQL）

数据来源是知识单元自带的 `relations`（提出者/学派/争议/对比/前置/应用）、`prerequisites`、`related` 字段，**入库时同步建边，无需改知识模型**。

| 环境变量 | 取值 | 说明 |
|---|---|---|
| `GRAPH_MODE` | `none`（默认）/ `kuzu` / `neo4j` | 图查询开关：关闭 / 嵌入式图库 / 服务端图库 |
| `GRAPH_DB_PATH` | 默认 `graph_kb.kuzu` | Kùzu 本地文件（**无需 Docker/Java**） |
| `GRAPH_URI` / `GRAPH_USER` / `GRAPH_PASSWORD` | `bolt://localhost:7687` / `neo4j` / **必填** | Neo4j 连接（无默认口令，防误连本地空实例） |
| `STORE_MODE` | `json`（默认）/ `pg` | 元数据存储：JSONL / PostgreSQL |
| `PG_DSN` | 默认 `postgresql://postgres:postgres@localhost:5432/mgmtkb` | PG 连接串 |

```bash
# 开图查询（Kùzu 嵌入式，真跑无服务端）
GRAPH_MODE=kuzu "$PY" -m uvicorn app.api.main:app --port 8932

# 图查询 API
curl localhost:8932/api/v1/kb/graph/KU-LEAD-001      # 前置链/提出者/学派/争议/案例/邻居
curl localhost:8932/api/v1/kb/graph-stats            # 节点与关系边规模
```

**选型理由与实测（2026-10-17）**：Neo4j 服务端在当前环境不可行（无 Docker、无 Java），但 **Kùzu 提供 win_amd64 wheel、嵌入式免服务端、兼容 Cypher 子集**，实测 9 项能力全部通过（多跳前置链、争议观点、提出者、域过滤、聚合、参数化查询、**MERGE 幂等**、DETACH DELETE、持久化重开）。`Neo4jGraphStore` 与 `KuzuGraphStore` 同接口、Cypher 语句同构，装好 Neo4j 服务端后改一个环境变量即切换。

**为什么默认 GRAPH_MODE=none**：图查询是增强能力而非基础设施，关闭时全链路照常可跑；26 单元规模下图查询价值主要在「争议观点呈现」与「前置链追溯」两个场景。

**降级与维护约定**：

1. `GRAPH_MODE=kuzu/neo4j` 构造失败 → 自动关闭图查询并记原因（经 `/healthz` 的 `graph_mode` / `graph_note` 暴露），不阻断启动；
2. `STORE_MODE=pg` 连接失败 → 自动降级 JSONL 并记原因（`store_mode` / `store_note`）；
3. 图与向量一样是**派生索引**：`reindex()` 里 `graph.rebuild(units)` 做差值同步（删陈旧节点 + MERGE 幂等重建），`knowledge/units/*.jsonl` 仍是唯一真相源；
4. `rebuild` 传 `known_ids` 过滤**悬空引用**（related 指向已删单元时不建节点不建边），避免「先删后建」的白做功与边抖动；
5. `*.kuzu` 是二进制产物，已排除出仓库，**切勿提交**。

**诚实边界**：Neo4j 与 PostgreSQL 的真实服务端路径**尚未实测**（环境缺 Java/PG 服务端），当前验证的是 Kùzu 全链路 + Neo4j/PG 的降级路径；`PgMetadataStore` 的建表/upsert/台账写入逻辑待有服务端后验证。

## 部署到 Render（2026-10-17，Blueprint 一键部署）

仓库根 `render.yaml`：Render Dashboard → **New Blueprint** 选 `wdwang2018/mgmt-kb-agent` 即按蓝图建服务，无需手填构建/启动命令。

| 配置项 | 值 | 说明 |
|---|---|---|
| 形态 | 单 Web Service（runtime: python） | FastAPI 同时伺服 API + `web/` 静态工作台，零前后端分离 |
| 构建 | `pip install -r requirements.txt` | 仓库根同一份依赖（含 milvus-lite / kuzu 可选增强） |
| 启动 | `cd backend && uvicorn app.api.main:app --host 0.0.0.0 --port $PORT` | `$PORT` 由 Render 注入；路径全按 `__file__` 相对定位，与 cwd 无关 |
| 健康检查 | `/healthz` | 同时暴露 llm / vector_mode / graph_mode / store_mode，退化一目了然 |
| 计划 / 区域 | free / Singapore | 免费 750 小时/月；冷启动约 50s；国内访问延迟最低 |

环境变量（render.yaml 预设）：`LLM_MODE=glm`、`VECTOR_MODE=hybrid`、`EMBED_MODE=glm`、`GRAPH_MODE=kuzu`、`PYTHON_VERSION=3.13.0`；**`GLM_API_KEY` 用 `sync: false`，只在你 Render 后台填、不进仓库**（沿用白起SSE 敏感 env 约定）。未填 Key 时 LLM 自动回落 mock，不阻断启动。

**持久化边界（诚实说明）**：Render free 计划无持久磁盘，重启/重新部署后文件系统重置——

| 数据 | 命运 | 说明 |
|---|---|---|
| `knowledge/*.jsonl`（26 单元 + 案例） | ✅ 保留 | 在 git 仓库，随镜像部署 |
| `milvus_kb.db` / `*.kuzu` | ⚠️ 重置 | 派生索引，启动时由 JSONL 自动重建（MERGE 幂等） |
| `checkpoints.db`（进行中的会话） | ⚠️ 重置 | 重启丢会话；但免费计划本身就会周期性重启，跨重启续会话需持久磁盘或 PG（`CHECKPOINT_MODE=postgres` 预留） |
| `runs/`（课程包）、流水线审核入库的新单元 | ⚠️ 重置 | **线上定位为只读演示**；生产知识编辑以本地仓库为真相源；后续可接 Render Managed PG（`STORE_MODE=pg` 适配器已就位、未实测） |

**上线步骤**：① Dashboard → New Blueprint 选本仓库 → ② 部署时填 `GLM_API_KEY`（建议同时填 `API_KEY`，见下节）→ ③ 拿到 URL 后验 `/healthz` 应返回 `llm=glm-*`、`vector_mode=hybrid`、`graph_mode=kuzu`（若为 `memory`/`none` 说明可选依赖没装上，已自动降级但功能仍完整）。

**已本地实测（2026-10-17）**：按 render.yaml 的启动命令与环境变量在 8934 端口起服务——20s 内完成启动，`/healthz` 三模式全开无降级、图统计 26 单元/103 边、Web 工作台与检索/图端点全部 200。

**线上实测（2026-10-17，https://mgmt-kb-agent.onrender.com）**：
- `/healthz`：`llm=glm-4-plus`、`vector_mode=hybrid`、`graph_mode=kuzu`——**milvus-lite 与 kuzu 的 Linux wheel 均安装成功，三模式零降级**；图统计 26 单元/13 学者/9 观点/2 案例/103 边与本地一致；
- 课程设计全链路：POST `/api/v1/runs`（新任经理/180 分钟）**2.8s** 到达 confirm_targets 挂起点（GLM 经 Render 新加坡区访问国内 API 可达且快，能力目标诊断含 TEAM/EXEC/COACH 三域差距分析）→ approve 后 **93s** 完成：G1-G4 四门禁全过、7 模块、10 条布鲁姆可测量目标、21KB 课程包，引用溯源完整（《领导梯队》第3章、《成为管理者》第1-2章、《能力陷阱》第1、4章、《教练式管理》第2章，均带证据等级）；
- 已知小瑕疵：LLM 偶将个别学习目标的所属域标注错位（如项目群目标标"沟通影响"），G1 建设性对齐门禁不校验域标注，待后续在门禁或后处理中收紧。

### P0 安全层（API Key 鉴权 + CORS 白名单 + 限流）

`backend/app/api/security.py`，三个中间件全部环境变量驱动、**未配置时整体退化为开放模式**（本地开发/测试零门槛）：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `API_KEY` | 未设 = 不校验 | 设置后所有 API 请求需带 `X-API-Key` 头；`/healthz`、`/docs`、静态首页豁免（健康检查与页面打开不依赖 Key） |
| `CORS_ORIGINS` | 空 | 逗号分隔的额外允许 origin（填线上域名）；本地 `127.0.0.1`/`localhost` 任意端口已内置放行（预览面板场景） |
| `RATE_LIMIT_IP` | 120 | 每 IP 每窗口请求数，0 = 不限 |
| `RATE_LIMIT_KEY` | 300 | 每 API Key 每窗口请求数，0 = 不限 |
| `RATE_LIMIT_WINDOW` | 60 | 限流窗口秒数；超限返回 429 + `Retry-After` |

要点：① 限流位于 Key 校验外层——**401 请求也计 IP 额度**（防暴力探测），有意为之；② 限流为进程内滑动窗口，单实例够用，多实例共享需 Redis（后续项）；③ Web 工作台收到 401 时弹出琥珀色横幅引导输入 Key，存 `localStorage` 持久化，填一次即记住；④ 测试 15 例（`tests/test_security.py`）含四轮变异验证。

### P1 可靠性层（检查点持久化 + CI）

**检查点持久化**（`backend/app/graph/checkpoint.py`）：LangGraph 检查点从 `MemorySaver`（进程内存，重启即丢进行中的会话）切换为嵌入式 SQLite（默认），与 Milvus Lite / Kùzu 同一嵌入式路线——**无服务端、无 Docker、一个文件**：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CHECKPOINT_MODE` | `sqlite` | `sqlite` = 嵌入式文件（`backend/checkpoints.db`，路径按 `__file__` 相对定位、与 cwd 无关）；`postgres` = 服务端（需配 DSN）；`none` = MemorySaver（仅测试） |
| `CHECKPOINT_DB_PATH` | `checkpoints.db` | 相对路径一律解析到 `backend/` 下（与 `graph_kb.kuzu` 同目录约定） |
| `CHECKPOINT_POSTGRES_DSN` | 未设 | `postgres` 模式的连接串；缺失或驱动缺失自动**降级 sqlite** 并在 `/healthz` 的 `checkpoint_note` 暴露原因 |

`/healthz` 新增 `checkpoint_mode` / `checkpoint_note` 两个字段，与既有 vector/graph/store 模式同一风格。**实测（2026-10-17，服务级重启 smoke）**：课程设计跑到 `confirm_targets` 挂起点 → kill 服务 → 重启 → GET 状态仍是 `awaiting_human` → approve 后 `completed`（7 模块/10 活动/10 评估/11 引用）完整产出——MemorySaver 时代重启后 resume 面对空状态，此场景不可能成立。`postgres` 分支为 Render Managed PG 预留（lazy 导入 + 降级链，本轮未实测服务端路径）。

**CI**（`.github/workflows/ci.yml`）：push/PR 触发，两步守质量——① `pytest` 全量（Mock 模式离线确定性，无需 Key）；② 黄金评测七项（完成率或门禁通过率 <100% 时脚本 exit 1，CI 直接红）。`deploy` 任务仅 main 分支 push 且测试通过后执行：读取 `secrets.RENDER_DEPLOY_HOOK` 调 Render Deploy Hook（**未配置该 secret 时自动跳过**，不影响绿灯）——配一次即拥有「测试过才部署」的自动流水线，无需再手动 Deploy。

配置方法：GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret，名 `RENDER_DEPLOY_HOOK`，值取 Render Dashboard → 服务 → Settings → Deploy Hook 生成的 URL。

### P2 可观测性层（结构化日志 + 进程指标 + LLM 计时）

免费 Render 实例**无日志留存**，一旦线上出问题只能靠结构化日志与指标端点自查——这是线上部署后最该补的一层（2026-10-17 落地）。全部环境变量驱动、未配置时默认 INFO / text 格式 / 仅控制台，零门槛。

**结构**：
- `backend/app/observability/logging_setup.py`：`configure_logging`（控制台 + 可选文件落盘 `RotatingFileHandler`，5MB×5 滚动）、`JSONFormatter`（单行 JSON，业务 extra 字段一并保留，便于 Render/ELK 采集）、`observability_config_from_env`
- `backend/app/observability/metrics.py`：进程内指标累加器（线程安全）——请求数、错误数、错误率、P50/P95 延迟（滚动 500 样本）、**`last_llm_ms` / `llm_calls_total`**、知识单元数
- `backend/app/observability/instrument.py`：`InstrumentedLLM` 透明包裹任意 LLMClient，记录每次调用耗时（`finally` 保证计时一定发生，异常不吞、不影响主链路）
- `backend/app/api/middleware.py`：`RequestLoggingMiddleware`——透传/生成 `X-Request-ID`（跨服务追踪）、记录 method/path/status/毫秒延迟/IP/是否带 Key；未捕获异常记堆栈并计错误；`/healthz` 心跳只计数不打 INFO（防探针刷屏）
- `backend/app/api/main.py`：`install_observability` 接线；`/healthz` 与新增 `/api/v1/metrics`（受 Key 鉴权）均返回指标快照

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `LOG_FORMAT` | `text` | `text` = 本地可读；`json` = 单行 JSON（Render 日志面板/集中采集推荐） |
| `LOG_DIR` | 未设 = 仅控制台 | 日志落盘目录（`working/logs/`，`.gitignore` 已排除）；不可写时自动退回控制台 |

要点：① LLM 计时在组合根 `build_context` 用 `wrap_llm` 包裹，**import 失败兜底退回原对象**，绝不因可观测性阻断启动；② 指标为进程内累加器，单实例够用，多实例聚合需 Prometheus/Redis（后续项，见路线）；③ 测试 8 例（`tests/test_observability.py`）含 JSON 序列化、关联 ID 回传、计数器递增、LLM 计时与四轮变异验证。

## 路线

- **P1 知识流水线**：✅ 已落地——采集（L1 语料带页码定位）→ 加工（规则/LLM 双模抽取）→ K1-K5 质检 → 人工审核台（Web 第四页签 + API）→ 编号入库即时生效
- **P1 基础设施·向量检索**：✅ 已落地——`EmbeddingClient` 端口 + Mock/GLM 双 Embedding 适配器 + `MilvusRetriever`（Milvus Lite 本地文件、**无需 Docker**，COSINE 语义召回 + `ARRAY_CONTAINS` 标量过滤 + upsert 幂等）+ `HybridRetriever`（BM25 + 稠密双路召回、RRF 融合、稠密侧加权 2.0）+ `VECTOR_MODE` 三模式切换与自动降级
- **P1 基础设施·图与持久化**：✅ 已落地——`GraphStore` 端口 + `KuzuGraphStore`（嵌入式、**实测 26 单元建出 103 条关系边**：前置链/提出者/学派/争议/案例查询全通）+ `Neo4jGraphStore`（lazy 占位，Cypher 同构可平移）+ `MetadataStore` 双适配器（JSON 默认 / PG lazy）+ `GRAPH_MODE` / `STORE_MODE` 切换与自动降级；**待实测**：Neo4j 与 PG 真实服务端路径（环境缺 Java/PG）
- **P1 基础设施·可靠性与 CI**：✅ 已落地——检查点持久化（sqlite 默认 + postgres 预留 + 降级链，重启续会话实测通过）+ GitHub Actions（pytest + 黄金评测 + 可选 Render 自动部署）
- **P2 可观测性**：✅ 已落地——结构化日志（text/json 双格式 + 文件落盘）/ 进程指标（QPS·错误率·P95 延迟·LLM 耗时）/ 请求关联 ID 与 `/api/v1/metrics` 端点
- **P2 培训支撑扩展**：✅ Web 工作台已落地（课程设计/知识库/运行记录/知识流水线四页签）；待做：备课助手、学员导师、案例导演（多角色模拟）；Kirkpatrick L1-L4 回流
- **P3 闭环运营**：学员画像、学习路径、课程包自动迭代；剩余工业级项：多实例限流（Redis）、LLM 故障降级链（GLM→DeepSeek→Mock）、token 计量与指标导出（Prometheus/Redis 多实例聚合）

## 约定

- 版权：只存「摘要 + 结构化要点 + 引用定位」，不整文复制；教辅书走授权或要点化
- 租户：公共知识 / 企业私有案例 / 个人学习数据三级隔离（复用白起SSE RBAC 模式，P1 落地）
- 知识编辑（领域专家）是唯一握有「入库签发权」的角色；Agent 是放大器，不是替代者
