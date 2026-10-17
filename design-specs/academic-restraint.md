# 设计规范｜管理学知识库工作台 · 学术克制

> 生成依据：`ui-ux-pro-max-skill` SOP（Step 1–5），基线来自对 `web/index.html` 的实测扫描。
> 日期：2026-10-18　适用对象：`mgmt-kb-agent/web/index.html`（单文件 1549 行）

---

## 一、需求解析（Step 1）

| 字段 | 取值 |
|---|---|
| `project_type` | 专业工具 · 课程设计工作台（B 端生产力工具，非展示型） |
| `target_platform` | Web 桌面端优先（讲师在工位备课），需容忍窄屏但不做移动端专项 |
| `user_persona` | 管理培训讲师 / 培训负责人，30–50 岁，在交付压力下备课；**对内容的学术可信度敏感** |
| `core_tasks` | ① 填七要素需求诊断单 → ② 生成课程包并人工审核/打回 → ③ 检索知识单元做备课溯源 |
| `brand_tone` | **学术克制**（Academic Restraint）：可信 > 亲切，权威 > 活泼，克制 > 精致 |
| `constraints` | 单文件 HTML（内联 `<style>`+`<script>`）、零外部依赖、**不可引用外部字体 CDN**（离线可用）、22 个表单控件属**录入密集**、须保留全部现有功能 |

### 由 Persona 推导出的两条硬约束

1. **讲师要向学员引用「这是 Simon 1960 的观点」** → 溯源信息（书/作者/年/章节）必须是**一等公民**，视觉权重不能低于装饰元素。
2. **讲师在赶交付** → 不能为「好看」牺牲信息密度。学术克制 ≠ 留白大片，而是**密度内的秩序**。

---

## 二、设计令牌表（Step 2）

### 2.1 推导逻辑：为什么现有色板不合格

现有 `--primary:#4f46e5`（靛紫）+ `.logo` 的 `linear-gradient(135deg,#4f46e5,#7c3aed)`（紫蓝渐变）—— 这恰好是 `impeccable` 与 `web-app-aesthetics-hardening` 双双点名的「AI 味配色」特征。学术克制要求：**低饱和、无渐变、有色相偏移的中性**。

### 2.2 颜色系统

色相推导：取墨蓝色相 **208°**，饱和度压到 38%（远低于 AI 感的 60–70%），亮度 30%。

| 令牌名 | 值 | 用途 | 推导依据 |
|---|---|---|---|
| `--bg` | `#f6f5f2` | 页面底色 | 宣纸暖灰，避开纯白与冷灰 AI 感 |
| `--paper` | `#fcfcfb` | 面板/卡片底 | 微暖白，与底色拉开 3% 明度即够 |
| `--paper-2` | `#f2f0ea` | 次级底纹（表头/引用） | 同色相再降一档 |
| `--rule` | `#e3e0d8` | 主分隔线 | 暖灰描边，替代大面积阴影 |
| `--rule-2` | `#cbc7bb` | 强调边框/输入框 | 表单需要更高对比（WCAG 非文本 3:1） |
| `--text` | `#22201d` | 正文 | 近黑但偏暖，纯黑在屏幕上过锐 |
| `--text-2` | `#6b675f` | 次要文本 | 对比度 ≈ 5.6:1 ✅ AA |
| `--text-3` | `#948f85` | 弱文本/占位 | 对比度 ≈ 3.1:1，仅用于非关键辅助 |
| `--ink` | `#2b4c6f` | **主色·墨蓝** | Hs 208° S38% L30%，大学出版社经典色 |
| `--ink-hover` | `#1f3b58` | 主色悬浮 | 亮度 −8% |
| `--ink-soft` | `#eaeef3` | 主色浅底 | 用于选中态背景 |
| `--seal` | `#9a3324` | **强调·朱砂** | 学术印章感，仅用于「需人工处理」语义 |
| `--seal-soft` | `#fbf0ee` | 强调浅底 | |
| `--verde` | `#3f6f52` | 成功·松绿 | 低饱和,避开 emerald 荧光感 |
| `--verde-soft` | `#edf3ef` | 成功浅底 | |
| `--ochre` | `#8a6d1f` | 警告·土黄 | 比 amber 更沉稳 |
| `--ochre-soft` | `#fbf6e8` | 警告浅底 | |
| `--brick` | `#8f2f2f` | 错误·砖红 | |
| `--brick-soft` | `#fbf0f0` | 错误浅底 | |
| `--azure` | `#37618c` | 信息·靛青（保留原 blue 语义位） | |

**对比度自检**（正文 13.5px，`#22201d` on `#fcfcfb`）：≈ 14.2:1 ✅ AAA
（`--text-2` 5.6:1 ✅ AA · `--ink` on white ≈ 7.4:1 ✅ AAA）

### 2.3 字体系统

学术感的第一来源不是颜色，是**衬线体**。

| 令牌名 | 值 | 用途 |
|---|---|---|
| `--font-serif` | `Georgia,"Times New Roman","Songti SC","STSong","SimSun",serif` | **标题 / 页码主标识 / 课程包正文** |
| `--font-sans` | `-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif` | UI 正文 / 表单 / 标签 |
| `--font-mono` | `ui-monospace,SFMono-Regular,Consolas,monospace` | KU 编号 / run-id（等宽对齐） |

- 禁用于标题：Inter / Roboto / Arial / Open Sans（三个 skill 的共同 DON'T）
- 标题字重：`--fw-title: 600`（不是 700，克制）
- 正文行高：`--lh-body: 1.65`（中文需比西文更松）
- 课程包 `.md-paper` 正文改用**衬线 + 1.75 行高**——这直接把「生成的课程包」观感从「网页」拉到「讲义」

### 2.4 间距系统（4px 基准，保持不变）

`--s1:4px --s2:8px --s3:12px --s4:16px --s6:24px --s8:32px --s12:48px`

### 2.5 圆角与阴影（克制的核心）

| 令牌名 | 值 | 说明 |
|---|---|---|
| `--r-xs` | `2px` | 标签 / 徽标（**取代 99px 胶囊**） |
| `--r-sm` | `3px` | 输入框 / 小按钮 |
| `--r-md` | `4px` | 卡片 / 大按钮 |
| `--r-lg` | `6px` | 浮层 |
| `--shadow-card` | `0 1px 2px rgba(34,32,29,.04)` | **仅一级**，或干脆不用 |
| `--shadow-pop` | `0 4px 14px rgba(34,32,29,.10)` | 仅浮层 |

> **圆角决策说明**：现有 99px 胶囊遍布 badge / chip / pill / graph-toggle 四类元素。学术克制要求「方而有度」，统一降为 `2px`。这不是审美偏好——胶囊 UI 是移动端审美迁移到桌面端的产物，而本产品是桌面讲师工具。

---

## 三、组件规格卡（Step 3）

### Button-Primary

| 属性 | 默认 | 悬浮 | 激活 | 禁用 |
|---|---|---|---|---|
| 背景 | `--ink` | `--ink-hover` | `--ink-hover` | `#cfcbc2` |
| 文字 | `#fff` | `#fff` | `#fff` | `#8d887e` |
| 边框 | 1px `--ink` | 1px `--ink-hover` | — | none |
| 圆角 | `--r-sm` | | | |
| 游标 | pointer / pointer / pointer / **not-allowed** | | | |

高 34px，横向 padding 14px，字号 13.5px，字重 500。**去掉 transition:all**（动画化所有属性是性能反模式）。

### StatusDot（替代 ✓ / ✗）

```css
.sdot{display:inline-block;width:7px;height:7px;border-radius:50%;flex:none;background:var(--text-3)}
.sdot.ok{background:var(--verde)} .sdot.warn{background:var(--ochre)}
.sdot.err{background:var(--brick)} .sdot.run{background:var(--ink)}
```
> `display:inline-block` 是必写项——`<span>` 是行内元素，漏写会导致圆点**完全不显示**，且 `node --check` 与任何静态检查都发现不了。

**凡用圆点处必须同时给出文字**（D 项「状态不唯色」），例：`● 通过` / `● 未通过`。

### GateBadge（质量门禁）

现有缺陷：`${g.passed ? "✓" : "✗"} ${g.gate}` —— **仅靠符号区分成败，无文字**，违反 D 项。

改为：`<span class="gate-badge (fail)"><i class="sdot (ok|err)"></i>通过 · ${gate}</span>`

### SourceRef（溯源条）

升为一等公民：`.u-source` 用**左侧朱砂细条 + 衬线书名**，让「引自哪本书」成为卡片上第二显眼的元素。

---

## 四、页面布局蓝图（Step 4）

```
┌─ header 52px ─ 纸白 · 1px 下边线 · Serif 标题 ─────────────┐
├─ sidebar 184px ─────┬─ main ────────────────────────────────┤
│ nav-item ×5         │ [banner 区：仅异常时出现]              │
│ 墨蓝左色条=选中      │                                        │
│ （不用背景块）       │ Tab 内容区                             │
│                     │  · 课程设计：design-grid 400px / 1fr   │
│ sidebar-foot        │  · 知识库：kb-bar + 卡片网格 或 学派图谱 │
└─────────────────────┴────────────────────────────────────────┘
```

- **选中态改由 `--ink` 左色条承担**，取消 `--ink-soft` 背景块（色块是重 UI，色条是轻 UI）
- 栅格：`--content-max: 1440px`，**不居中留白**——讲师需要横向信息密度
- 响应式断点：`1080px`（设计页单列）/ `960px`（流水线单列）/ `720px`（侧栏收窄至 56px 图标态）
- 关键路径：需求单 → 生成 → **人工审核** → 课程包。**审核节点用朱砂左色条**标出（全文唯一高饱和色）

---

## 五、前端实现指引（Step 5）

### 5.1 CSS 变量落地位置

`:root{...}` 整体替换。**保留所有变量名前缀习惯**，仅改值域，避免连带改动 200+ 处引用。
新增 `--font-serif / --font-mono / --r-xs / --seal / --verde / --ochre / --brick`。

### 5.2 必须执行的替换清单

| 类别 | 旧 | 新 |
|---|---|---|
| 紫蓝渐变 logo | `linear-gradient(135deg,#4f46e5,#7c3aed)` | 纯色 `--ink` + serif 汉字「管」 |
| 胶囊圆角 | `border-radius:99px`（约 14 处） | `var(--r-xs)` |
| focus 光环 | `box-shadow:0 0 0 3px var(--primary-soft)` | `outline:2px solid var(--ink); outline-offset:1px` |
| 标题字体 | 继承 sans | `font-family:var(--font-serif)` |
| 卡片 | border+shadow | border 1px `--rule`，shadow 仅一级 |

### 5.3 无障碍

- 全局 `*:focus-visible{outline:2px solid var(--ink);outline-offset:1px}`（**现状为 0，必须补**）
- `Escape` 关闭浮层/回到卡片视图（现状为 0）
- `prefers-reduced-motion` 下禁用 spin / flash 动画（现状为 0）
- 所有 `.sdot` 状态必须配文字

### 5.4 N1 告警（来自 `web-app-aesthetics-hardening`）

本项目 22 个表单控件 → **禁止加裸 `←/→` 切页快捷键**。输入框聚焦时按方向键必须仍是光标移动。

---

## 六、校验清单

- [ ] `:root` 令牌全部替换，无残留 `#4f46e5`/`#7c3aed`/`99px`
- [ ] Serif 字体生效于 h1/h2/brand/md-paper 正文
- [ ] 48 个符号按判定表处理完毕（功能箭头保留 / 状态符号转圆点+文字 / 装饰清除）
- [ ] `focus-visible` / `Escape` / `prefers-reduced-motion` 均注入
- [ ] GateBadge 等状态均有文字冗余
- [ ] 无新建外部依赖（离线可用）
- [ ] 内联 JS 通过 `node --check`
- [ ] pytest 全过 + 黄金评测七项 100%
- [ ] 起 uvicorn 做 HTTP 冒烟：四个 tab 均正常渲染
