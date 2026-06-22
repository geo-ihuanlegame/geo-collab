# Claude Code Loop × GEO MCP · 核心架构可视化稿（HTML）· 设计

- 状态：设计稿（v0），待 review 后出 HTML 本体
- 日期：2026-06-22
- 上游来源：[`2026-06-18-claude-code-loop-with-geo-mcp-design.md`](./2026-06-18-claude-code-loop-with-geo-mcp-design.md)（POC 正式落地稿）
- 风格对标：[`2026-06-17-loop-engineering-geo-integration.html`](./2026-06-17-loop-engineering-geo-integration.html)（同套调性、同套配色变量）
- 受众：老板演示 + 技术 leader 对齐（**5 分钟讲完**）
- 不是什么：不是讨论稿，不是替代上游 06-18 设计，不增删 POC 范围

---

## 0. 一句话

把 06-18 POC 的架构落成**一张可投屏 / 可截图 / 可塞进飞书**的单页 HTML，老板 5 分钟看懂「**飞书自然语言** → **Claude Code 跑 Loop** → **GEO MCP 提供能力** → **飞书回报**」这条闭环。

---

## 1. 锁定决定（brainstorming 已答）

| # | 决策 | 选项 |
|---|------|------|
| 1 | 范围 | **单页核心架构图（聚焦）**——500–800 行 HTML，TL;DR + 大架构图 + 数据流脚注 |
| 2 | IM 边方向 | **POC 实线 + v3 虚线叠加**——飞书 ← 推送实线；飞书 → @bot 反向触发用虚线 + `v3` chip |
| 3 | 视觉布局 | **左右三柱**——使用者层 / Loop 大脑 / GEO 能力底座 |
| 4 | 样式调性 | **复用 06-17 同套调性**——浅色起底 + panel + 四层色（plugin 绿 / flow 蓝 / skill 紫 / cloud 青）+ Loop indigo |

---

## 2. 页面结构（5 节，从上到下）

### 节 1：doc header + TL;DR 卡（页首一屏内）

- header `<div class="doc-header">`：tagline `ARCHITECTURE SNAPSHOT v0` + 主标题「Claude Code Loop × GEO MCP · 核心架构」+ 副标题一行
- TL;DR 卡（`.tldr` 风格沿用 06-17）：
  - 「这是什么」：Loop 不在 GEO 里跑，跑在 Claude Code CLI；GEO 通过 MCP server 暴露 17 个 atomic tool
  - 「关键 3 个数」：**17 MCP tools / 3 Loop 配方 / 7 天 POC**（三个大数字横排，类似 KPI 卡）
  - 「跟上游的关系」：是 06-18 设计稿的可视化分发版；架构边界与那份完全一致
  - 「演示路径」：飞书目标 → `/loop` 启动 → Claude 调 MCP tools → GEO 落库 → 飞书报完工

### 节 2：核心架构图（**主角**，占首屏 60%+）

水平三柱布局，纯 HTML/CSS 实现（不引外部图库），SVG 只用来画箭头和虚线 v3 边。

**左柱：使用者层（plugin 绿系）**
```
┌────────────────────────────┐
│ 👤 运营人                  │
│ ────────────────────────── │
│ 启动 Loop：                │
│   [POC] 终端 /loop ...     │ ← 实线指向中柱
│   [v3 ]飞书 @bot 自然语言   │ ← 虚线指向中柱
│ ────────────────────────── │
│ 💬 飞书群（接收回报）       │
│   - 心跳 / 阶段 / 告警 / 完工│ ← 来自右柱的实线
└────────────────────────────┘
```
- 主调：plugin 绿 `--plugin: #10b981`
- 左柱内显式分三块：**运营人头像** + **启动 Loop 的两种机制**（POC 终端实线 / v3 @bot 虚线 + `v3` chip） + **飞书群接收回报**（webhook 实线进站）
- 这样老板一眼看清：现在是"运营人在终端起 Loop / 飞书只收报"，v3 才"飞书内一句话直接起 Loop"

**中柱：Loop 大脑（loop indigo）**
```
┌────────────────────────────────┐
│ 🧠 Claude Code（本机 CLI）     │
│ ────────────────────────────── │
│ /loop generation-loop.md       │
│ /loop distribute-loop.md       │
│ /loop weekly-report-loop.md    │
│                                │
│ 五件套：                       │
│  目标 / 上下文 / 工具 /        │
│  评估 / 停止条件               │
│                                │
│ 主 LLM: Anthropic Opus         │
└────────────────────────────────┘
```
- 主调：Loop indigo `--loop: #6366f1`
- 突出「五件套」（Loop Engineering 概念，跟 06-17 第 1 节呼应）
- 三个 Loop 配方做成 3 个并排小 chip（generation / distribute / weekly-report），每个 chip 加 emoji（✍️ / 📤 / 📊）

**右柱：GEO 能力底座**

纵向三层叠：

- **顶层 MCP Server**（cloud 青）：FastMCP / stdio / `GEO_MCP_TOKEN` 鉴权 + 「17 tools」标记
- **中层 新增 API**（黄底高亮表示「这次新做的」）：
  - `auto_review`（自动审核 decision 持久化）
  - `performance`（模板/账号 metrics 聚合）
  - `feishu-notify`（MCP 通知出口封装）
- **底层 现有 GEO 能力**（多色 chip 横排，2×4 网格）：
  - pipelines（flow 蓝）
  - ai_generation（flow 蓝）
  - prompt_templates（skill 紫）
  - image_library（skill 紫）
  - question_bank（skill 紫）
  - articles / accounts（plugin 绿）
  - hot_lists（plugin 绿）
  - audit_logs（cloud 青）

**主箭头（SVG，画在三柱之间）**

| 起 | 止 | 标签 | 线型 | 说明 |
|----|----|------|------|------|
| 左·POC 终端 | 中 | 终端 `/loop ...` 启动 | **实线**，indigo | POC 期运营人手工敲 |
| 左·v3 @bot | 中 | 飞书 @bot 自然语言下令 | **虚线**，浅 indigo + `v3` chip | 愿景边 |
| 中 | 右 | MCP tool 调用（stdio） | **实线粗**，indigo | 17 个 atomic tools |
| 右 | 左·飞书群 | 飞书 webhook（心跳/告警/完工） | **实线**，plugin 绿 | 单向通知 |

> POC 现状是「运营人在本机终端敲 `/loop`」，左柱内显式画两个启动入口（POC 实线 / v3 虚线），主箭头表跟左柱内的标识对齐；不靠主标题旁的小字额外解释。

### 节 3：「一次 Loop 跑了什么」时序条带（横向 6 步）

横向 6 个 step 卡片串成条带（参考 06-17 已有横向步骤组件），每个 step 一行：

1. **启动** — 终端 `/loop generation-loop.md`（POC）/ 飞书 @bot（v3）
2. **拉上下文** — `list_question_pools` → `list_question_items` → `list_prompt_templates`
3. **生产** — `compose_article` × N + `illustrate_article`
4. **自评** — `score_recent_articles`（ai_format_model 批量打分）
5. **决策** — `submit_review_decision` → 入未审核库（人审兜底）
6. **报工** — `notify_feishu(level="done", ...)`

每个 step 底部小灰字标注：哪个 tool 在叫、对应 GEO 哪个模块。

### 节 4：17 个 MCP tool 速查（分 3 列）

按 06-18 设计稿的 3 组（catalog / action / meta）做 3 列卡片：

- **Catalog（只读，7 个）**：灰 chip
  - `list_articles` / `list_question_pools` / `list_question_items` / `list_prompt_templates` / `list_pipelines` / `list_accounts` / `get_article`
- **Action（写，6 个）**：橙 chip
  - `compose_article` / `illustrate_article` / `submit_review_decision` / `set_review_status` / `create_distribute_task` / `notify_feishu`
- **Meta（评估/回流，4 个）**：紫 chip
  - `score_recent_articles` / `get_template_performance` / `get_account_performance` / `record_publish_metrics`

每个 tool 后面跟一行 ≤30 字超短说明。

### 节 5：v2 / v3 路标小卡（页尾收束）

横向 3 卡：

- **POC（v0，本周）** — 终端 `/loop` + 单向飞书通知 + 17 tools + auto_review + performance
- **v2（1–2 个月）** — Skill 包装、长跑服务器（tmux）、真 metrics 接入（头条/微信 API）、MCP token 吊销 UI、多 Loop 并行
- **v3（3–6 个月）** — 飞书内 @bot 自然语言双向、Skill 市场、多游戏多矩阵

每卡顶部一个 emoji 标识阶段（🌱 / 🌿 / 🌳）。

---

## 3. 风格细节（沿用 06-17）

### CSS 变量（直接复制）

```css
--bg: #f8fafc; --card: #ffffff; --border: #e2e8f0; --text: #0f172a;
--muted: #64748b; --brand: #10b981; --link: #1e40af; --warn: #ef4444;
--plugin: #10b981; --flow: #1e40af; --skill: #8b5cf6; --cloud: #06b6d4;
--loop: #6366f1; --loop-soft: rgba(99,102,241,0.08);
```

### 字体 / 字号

- font-family 与 06-17 完全一致（系统字体 stack）
- h1 28 / h2 22 / h3 16 / 正文 14–15
- 中柱「大脑」卡和左柱「飞书」卡可以视觉上做大一档（h2 字号居中）

### 组件复用清单

- `.tldr` 顶部摘要卡 → 节 1
- `.panel` 通用卡 → 三柱主体
- `.tag` chip → tool 列表、v3 标识、阶段标识
- 06-17 节 1 的「步骤条带」DOM 风格 → 节 3 端到端时序条
- 06-17 节 4-5 用过的 `.grid-3` / `.grid-2` → 节 4-5

### SVG 箭头

- 实线 stroke `var(--loop)` 2.5px，箭头三角形端
- 虚线 `stroke-dasharray="6 4"`，浅 indigo 透明 0.5
- v3 chip：indigo bg + 白字 + `font-size:10px` + 字母「v3」

---

## 4. 文件 / 命名

| 类型 | 路径 |
|------|------|
| 设计 spec | `docs/superpowers/specs/2026-06-22-claude-code-loop-architecture-visual-design.md`（本稿） |
| HTML 本体 | `docs/superpowers/specs/2026-06-22-claude-code-loop-architecture-visual.html` |
| 备用纵向引用 | 上游 06-18 spec 和 06-17 HTML，本稿在 doc-header 里通过 `<a>` 互链 |

---

## 5. 验收

- [ ] 单文件 HTML，不依赖外部 JS / CSS / 字体 / 图片，离线双击能打开
- [ ] 在 1280 宽屏下首屏（无滚动）能看到完整核心架构图 + TL;DR
- [ ] 三柱箭头方向、虚线位置与节 2 描述完全一致
- [ ] 17 个 MCP tool 名字与 06-18 设计稿一字不差
- [ ] 「POC 现状 vs v3 愿景」边界有视觉区分（实线/虚线 + chip）
- [ ] 印刷模式下颜色不丢（关键边界 +1px 边框，非纯靠颜色区分）
- [ ] 字数控制：HTML 单文件 ≤ 800 行

---

## 6. 不做（YAGNI）

- 不做交互（无 click 展开 / 无切换 tab）——演示场景就是投屏 + 飞书截图
- 不嵌 logo / 不引外部 CDN
- 不做暗色模式
- 不做移动端响应式（投屏专用，1024–1920 宽稳定就行）
- 不重新设计颜色 / 字体 / 图标体系
- 不复刻 06-17 的 Section 1-9 整套——只复用调性，内容是新写的（这份是聚焦版，不是 deck）

---

## 7. 风险

| 风险 | 缓解 |
|------|------|
| 老板把 POC 和 v3 搞混（以为飞书已经能自然语言下令） | 标题下小字 + v3 chip + 虚线/实线区分；演示口径配套 |
| 17 个 tool 列出来太密 | 分 3 列 + 短说明 + 颜色区分组别；不在主图里展开，单独一节 |
| 跟 06-17 那份重复 | 06-17 是 Loop Engineering 概念讨论稿（9 节深度叙事），这份是 06-18 POC 的架构 snapshot；定位不同，引言里讲清楚 |
| 单文件 HTML 行数失控 | 目标 ≤ 800 行；超出就把节 4 tool 表用更紧凑布局压缩 |
