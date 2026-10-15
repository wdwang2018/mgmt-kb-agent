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
│   │   ├── ports.py           # 端口协议（LLM/检索/知识库）
│   │   ├── adapters/          # Mock/DeepSeek/GLM LLM、内存检索、JSON 知识库、JSON 语料层
│   │   ├── graph/             # 九节点(课程设计) + 五节点(知识流水线) + 四门禁/五质检 + HITL
│   │   ├── services/          # 课程设计流水线 + 知识流水线 + 渲染 + 组合根
│   │   └── api/               # FastAPI 路由（含 kb 流水线端点）
│   ├── scripts/               # run_demo / export_schemas / probe_langgraph
│   └── tests/                 # 58 个测试（门禁/检索/原型/流水线/API/LLM 输出形状/知识流水线）
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
├── requirements.txt
└── runs/                      # 运行产物（课程包 + kb-runs 草稿记录；已 gitignore）
```

## 快速开始（本机实测命令）

```bash
PY="C:/Users/DELL/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
# 依赖已装在该受管 venv（langgraph 1.2.15 / fastapi / pydantic / pytest / httpx）

# 1. 测试（58 个）
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
| **知识库** | 知识单元检索：关键词 + 领域 + 证据等级（A/B/C）筛选；卡片含引用定位、学员常见误区、学派争议标记 |
| **运行记录** | 历史 run 列表（状态/对象/时长/模块数），点击回看课程包 |
| **知识流水线**（P1） | 语料采集表单 → 五节点进度 → 草稿审核卡片（K1-K5 明细、批准/打回）→ 草稿台账 + L1 语料层；待审数量实时徽标 |

配套 API：`GET /api/v1/runs`、`GET /api/v1/runs/{id}`（补全计数字段，刷新后统计不丢）、`GET /api/v1/kb/units`（含误区）、kb 流水线六端点（见上文）、CORS 全开、静态挂载（API 路由优先，不被遮蔽）。

**浏览器实测（2026-10-15，Playwright 真实驱动）**：
- 课程设计链路：表单 → 生成 → 人工确认卡片（能力目标表格正确渲染）→ 批准 → 完成面板（7 模块/19 活动/10 评估题/9 引用、四门禁 ✓），0 JS 错误；
- 知识流水线链路：语料提交 → DR-0001 草稿 → K1-K5 五项全过 → 批准入库 KU-LEAD-013 → 知识库 24→25 即时生效 → 台账/语料 extracted → 徽标清零，10/10 PASS。

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

- `pytest`：**58 passed**（课程设计全链路 + 知识流水线全链路：抽取/质检打回/唯一性/编号分配/HITL 挂起恢复/API 冒烟/坏 shape 回退/静态挂载与 runs 列表）
- `eval/run_eval.py`：完成率 100%、门禁通过率 100%、recall@5 100%、溯源覆盖率 100%、建设性对齐率 100%、域识别 100%、困境识别 100%
- 知识流水线浏览器实测（Playwright 真实驱动）：语料提交 → DR-0001 草稿 → K1-K5 五项全过 → 批准入库 KU-LEAD-013 → 知识库 24→25 即时生效 → 台账/语料 extracted → JS 零错误，10/10 PASS
- 修复并回归的真实缺陷：LangGraph interrupt 语义（实测后实现）、检查点 msgpack 白名单（含 KnowledgeUnit 内部三个枚举）、缩放舍入漂移、活动阈值丢弃导致 G3 失真、`normalize_mix` 死循环（过期快照值 + 零桶乒乓）、pydantic 列表拷贝导致域增强丢失、增强域目标无模块承载导致 G1 失败、Mock 抽取极短语料击穿 pydantic min_length、在线抽取脏 shape 构造前回退、`save_unit` 空目录崩溃、知识流水线 reject 路径缺终态节点

## Mock LLM 的诚实边界

默认 `LLM_MODE=mock` 是**确定性模板桩**：保证无 API Key 下端到端可运行、可测试、可回归；它只做结构化装配与模板措辞，**事实性内容全部来自检索到的真实知识单元**（引用、误区、证据等级均为真）。内容的语言质量、情境贴合度由在线模型（`LLM_MODE=deepseek` 或 `LLM_MODE=glm`）承担，切换后需重跑 `eval/run_eval.py` 复测。知识流水线的「加工」节点同理：Mock 走关键词+句子切分规则抽取，在线走 LLM 萃取，两条路都过同一套 K1-K5 质检。

## 路线

- **P1 知识流水线**：✅ 已落地——采集（L1 语料带页码定位）→ 加工（规则/LLM 双模抽取）→ K1-K5 质检 → 人工审核台（Web 第四页签 + API）→ 编号入库即时生效；待做：Milvus 混合检索 / Neo4j GraphRAG / PostgreSQL 存储适配器（端口已留，按需切换）
- **P2 培训支撑扩展**：✅ Web 工作台已落地（课程设计/知识库/运行记录/知识流水线四页签）；待做：备课助手、学员导师、案例导演（多角色模拟）；Kirkpatrick L1-L4 回流
- **P3 闭环运营**：学员画像、学习路径、课程包自动迭代

## 约定

- 版权：只存「摘要 + 结构化要点 + 引用定位」，不整文复制；教辅书走授权或要点化
- 租户：公共知识 / 企业私有案例 / 个人学习数据三级隔离（复用白起SSE RBAC 模式，P1 落地）
- 知识编辑（领域专家）是唯一握有「入库签发权」的角色；Agent 是放大器，不是替代者
