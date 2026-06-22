# Claude Code Loop + GEO MCP · 设计稿

- 状态：设计稿（v0），待 pair coding 实施前 review
- 日期：2026-06-18
- 上游讨论稿：[`2026-06-17-loop-engineering-geo-integration-design.md`](./2026-06-17-loop-engineering-geo-integration-design.md) + 同名 `.html`（保留有效；本稿是其方案 C「Agent Town」的 **OpenClaw 范式**具体落地）
- 取代：~~`2026-06-17-topic-loop-demo-design.md`~~（已删除，方向已转）
- 受众：pair coding 实施前对齐 + 团队架构对齐

---

## 0. 一句话

**Loop 不在 GEO 内部跑，而是在 Claude Code 本机 CLI 进程里跑**。GEO 通过 **MCP server** 把现有能力（pipelines 节点、提示词模板、图片库、问题池、文章 CRUD、分发链路、新增评估器/反馈回流 API）暴露给 Claude Code 调用。Loop 关键节点推飞书 webhook。POC 跑两个 Loop：**生文 Loop** + **发文 Loop**，含**自动审核 / 评估器 / 反馈回流**三块新能力。

---

## 1. 决策快照（已锁，不再讨论）

| 决策点 | 锁定选项 | 含义 |
|--------|----------|------|
| 暴露协议 | **MCP（先做）** | atomic tools 设计，未来不锁 Claude 生态；Skill 包装留 v2 |
| 运行环境 | **本机 Claude Code** | POC 期最快、零部署；长跑服务器留 v2 |
| 通知机制 | **飞书 webhook（单向）** | 复用现有 `shared/feishu.py`；飞书内互动留 v3 |
| 旧 spec | **删 topic_loop spec、留 Loop Engineering 讨论稿** | 战略层论述仍有效，模块级旧方案已过时 |

---

## 2. 架构总览

### 2.1 新旧对比

```
旧方向（topic_loop 模块）                  新方向（Claude Code Loop + GEO MCP）
────────────────────────                  ──────────────────────────────────────
GEO 系统                                  [Claude Code 本机 CLI 进程]
├ topic_loop 模块                            ├ /loop / /goal / Task 工具
│  ├ scheduler 线程                          └ Loop 配方（prompt + 工具调用编排）
│  ├ evaluator (litellm)                              ↓ MCP 协议（stdio）
│  └ 3 张 DB 表                            [GEO MCP Server]（新增、薄）
└ 前端 tab                                    └ atomic tools × ~15 个
                                                      ↓ HTTP
                                            [GEO 后台]（能力底座）
                                            ├ 现有：pipelines / prompt_templates /
                                            │       image_library / question_bank /
                                            │       articles / accounts / 分发链路
                                            └ 新增 API：
                                                 - 自动审核（auto_review）
                                                 - 评估器（performance metrics）
                                                 - 反馈回流（publish metrics 写回）
                                                      ↓ 通知
                                            [飞书 Webhook]
                                            ├ Loop 心跳
                                            ├ 阶段汇报
                                            └ 异常告警
```

### 2.2 角色边界

| 谁 | 干什么 | 不干什么 |
|----|--------|---------|
| **Claude Code** | Loop 编排 / 节奏控制 / LLM 推理 / 决策（"这篇要不要重写"） | 不直连 DB / 不直接调 LiteLLM / 不读飞书 |
| **GEO MCP Server** | 协议层薄壳：tool schema 校验 + HTTP 转发 + 鉴权 | 不含业务逻辑（业务在 GEO API 后台） |
| **GEO 后台** | CRUD / 数据持久化 / 现有 pipelines 节点 / 新增评估 API / 推飞书 | 不再背 Loop 调度 / 不再背 prompt 设计 |
| **飞书 Webhook** | Loop 关键节点的通知出口 | POC 期不收消息（v3 才做反向触发） |

---

## 3. MCP Server 设计

### 3.1 部署形态（POC）

- **目录**：`server/mcp/`（跟 GEO 同 repo，便于复用 schemas / config）
- **SDK**：FastMCP（Anthropic 官方 Python SDK，`pip install mcp`）
- **传输**：stdio（POC 期最简单；Claude Code 直接 spawn server 进程）
- **启动方式**：Claude Code 配置 `~/.claude.json` 的 `mcpServers`：
  ```json
  {
    "mcpServers": {
      "geo": {
        "command": "python",
        "args": ["-m", "server.mcp.server"],
        "env": {
          "GEO_MCP_TOKEN": "...",
          "GEO_API_BASE_URL": "http://127.0.0.1:8000"
        }
      }
    }
  }
  ```
- **运行模型**：Claude Code 启动时自动 spawn；session 结束自动 kill；token 配置在 env

### 3.2 鉴权

**独立 service token，不复用 user JWT**：
- `GEO_MCP_TOKEN` 环境变量（生成方式：`openssl rand -hex 32`），同时配在 MCP server 启动 env + GEO 后台 `.env`
- GEO 新增中间件 `verify_mcp_token`：识别 header `X-MCP-Token` → 校验 → 注入虚拟 `mcp-service` 操作者身份（用于审计）
- MCP server 内每个 tool 调用都带 `X-MCP-Token` header
- 后台 admin tab 可显示当前活跃 MCP token + 吊销（v2）

### 3.3 工具粒度原则

**粗中取细**——一个 tool 对应一个明确意图，不要做"万能 batch tool"：
- ❌ `geo_action(action_type, params)` —— 太粗，LLM 难选
- ❌ `set_article_field(article_id, field, value)` —— 太细，LLM 调用爆炸
- ✅ `list_articles(filter)` / `compose_article(...)` / `submit_review_decision(...)` —— 一个意图一个 tool

POC 目标 **~15 个 tool**，分三组：

### 3.4 工具清单 v0（POC）

**A. Catalog 类（只读，复用现有 GEO API）**

| Tool | 对应 GEO API | 说明 |
|------|-------------|------|
| `list_question_pools()` | `GET /api/generation/question-pools` | 列问题池 |
| `list_question_items(pool_id, limit, category?)` | `GET /api/generation/question-pools/{id}/items` | 拉问题项 |
| `list_prompt_templates(scope)` | `GET /api/prompt-templates?scope=...` | 列提示词 |
| `list_pipelines(type?)` | `GET /api/pipelines` | 列工作流 |
| `list_articles(status?, review_status?, limit)` | `GET /api/articles` | 列文章 |
| `list_accounts(platform_code?, distribution_enabled?)` | `GET /api/accounts` | 列账号 |
| `get_article(article_id)` | `GET /api/articles/{id}` | 文章详情（含 content） |

**B. Action 类（写操作）**

| Tool | 对应 GEO API | 备注 |
|------|-------------|------|
| `compose_article(question_item_id, prompt_template_id, model?)` | 调 `ai_compose` 节点 handler 直接生文 | **新建一条直调路径**，绕过 pipeline 整体编排（POC 期间不跑 pipeline run，让 Loop 控） |
| `illustrate_article(article_id, category_ids?)` | 调 `image_library/hook.py` 现有逻辑 | 已有 |
| `submit_review_decision(article_id, decision, score_breakdown?, reasoning?)` | **新 API** `POST /api/articles/{id}/auto-review` | decision ∈ approved / needs_rewrite / rejected |
| `set_review_status(article_id, "approved" or "pending")` | `PATCH /api/articles/{id}` | 已有 |
| `create_distribute_task(article_ids, account_ids, name?)` | `POST /api/tasks` (task_type=article_round_robin) | 已有 |
| `notify_feishu(level, title, message, details?)` | **新 API** `POST /api/system/feishu-notify`（封装现有 `shared/feishu.py`） | level ∈ info / warning / error / done |

**C. Meta 类（评估 / 回流，全部新增）**

| Tool | 对应 GEO API | 说明 |
|------|-------------|------|
| `score_recent_articles(article_ids, dimensions?)` | **新 API** `POST /api/articles/score` | 让 GEO 内的 LLM（ai_format_model）批量给文章打分；维度可参数化 |
| `record_publish_metrics(record_id, metrics)` | **新 API** `POST /api/publish-records/{id}/metrics` | metrics = {views, likes, comments, shares} 等 |
| `get_template_performance(template_id, window_days)` | **新 API** `GET /api/prompt-templates/{id}/performance` | 该模板产出的文章 metrics 聚合 |
| `get_account_performance(account_id, window_days)` | **新 API** `GET /api/accounts/{id}/performance` | 该账号发布的文章 metrics 聚合 |

POC 期 **15 个 tool**；如有更多需求 v2 再加。

### 3.5 tool schema 约定

- 所有 tool 入参用 Pydantic schema（FastMCP 自动生成 JSON schema 给 LLM）
- 所有 tool 出参带 `{"ok": bool, "data": ..., "error": str?}` 顶层封装
- 失败时 ok=False + error 描述（不抛异常进 Claude Code）
- 大对象（如 article content）按需返回；list 类默认带 `limit` 防爆

---

## 4. GEO 新增能力

### 4.1 新表（最少集）

#### `auto_review_decisions`
Loop 自动审核的决策记录（独立于人工审核）。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer PK | |
| article_id | Integer FK NOT NULL | → articles.id |
| decision | String(20) NOT NULL | approved / needs_rewrite / rejected |
| score_total | Integer | 0-100，加权后 |
| score_breakdown | JSON | `{"factuality":85,"readability":90,...}` |
| reasoning | Text | LLM 评分理由 |
| decided_by | String(50) NOT NULL | `claude-code-loop` / `auto-reviewer-v1` |
| created_at | DateTime NOT NULL | |

索引：`(article_id, created_at DESC)`

#### `publish_record_metrics`
分发后的回流 metrics（如果现有 `Article.metrics` JSON 字段足够，可不建此表，直接复用）。

> **设计决策**：先用 `Article.metrics` JSON 字段（看现有 schema 有没有；若没有 → 新增），不开新表。新表是 v2 优化项。

### 4.2 新 API 列表

| Method | Path | 用途 | 鉴权 |
|--------|------|------|------|
| POST | `/api/articles/{id}/auto-review` | 写 auto_review_decision | MCP token |
| POST | `/api/articles/score` | 批量 LLM 评分（用 ai_format_model） | MCP token |
| POST | `/api/publish-records/{id}/metrics` | 写回阅读/互动 metrics | MCP token + user JWT 双通道 |
| GET | `/api/prompt-templates/{id}/performance` | 拉模板产出 metrics 聚合 | user JWT |
| GET | `/api/accounts/{id}/performance` | 拉账号产出 metrics 聚合 | user JWT |
| POST | `/api/system/feishu-notify` | 封装现有 webhook，供 MCP 调用 | MCP token |

### 4.3 Alembic 迁移

`0047_auto_review_decisions.py`：建一张 `auto_review_decisions` 表 + 索引。若现有 articles 表无 `metrics` JSON 字段，同迁移加一列 `metrics JSON NULL`。

---

## 5. Loop 配方（在 Claude Code 这边）

### 5.1 生文 Loop（POC 第一个）

**Goal**: 今天产出 5 篇过自动评分的文章，进入未审核库等人审。

**Prompt 框架**（伪码，实际跑时用 markdown 文件存配方）：

```
你是 GEO 平台「餐厅养成记」官方矩阵的生文 Loop runner。

目标：今天产出 5 篇过自动评分的文章入未审核库。

工具：你可以调用 geo MCP server 的 list_* / compose_article / illustrate_article /
score_recent_articles / submit_review_decision / notify_feishu。

每轮迭代：
1. list_question_pools() → 选默认 pool（pool[0].id）
2. list_question_items(pool_id, limit=5, status="pending") → 拿 5 个候选问题
3. list_prompt_templates(scope="generation") → 拿可选模板
4. For each 候选问题（直到产出 5 篇通过的，最多 10 轮防发散）：
   a. template_id = 从模板里挑一个（用 get_template_performance 看历史表现，优先 score 高的）
   b. article = compose_article(question_id, template_id)
   c. illustrate_article(article.id)
   d. score = score_recent_articles([article.id])
      → dimensions = [factuality, readability, style, policy_safety]
   e. if score.total >= 70:
        submit_review_decision(article.id, "approved", score_breakdown=..., reasoning=...)
        set_review_status(article.id, "pending")  # 进未审核库（人审兜底）
        累计 success_count
      elif score.total >= 40:
        submit_review_decision(article.id, "needs_rewrite", reasoning=...)
        换一个 prompt template 重试一次（最多 1 次）
      else:
        submit_review_decision(article.id, "rejected", reasoning=...)
5. notify_feishu(level="done", title="今日生文 Loop 完成",
   message=f"产出 {success_count}/5 篇过自评候选 · 用时 X 分钟 · 用了 Y 个 token")

停止条件：success_count >= 5 OR 累计循环 >= 15 轮 OR 超过 60 分钟
```

**存放位置**：`claude-loops/generation-loop.md`（项目根新增 `claude-loops/` 目录，专门放 Loop 配方）

### 5.2 发文 Loop（POC 第二个）

**Goal**: 把已审核库待发布文章分发到合适账号 + 回流上一轮发布的 metrics。

```
你是 GEO 平台「餐厅养成记」官方矩阵的发文 Loop runner。

目标：
1. 分发：把已审核库里 ready 的文章分发到 distribution_enabled 的账号
2. 回流：拉过去 24h 已完成的发布记录，写回阅读 / 互动 metrics

工具：list_articles / list_accounts / create_distribute_task / get_account_performance /
get_publish_metrics / record_publish_metrics / notify_feishu

每轮迭代：
1. 分发阶段：
   a. articles = list_articles(status="ready", review_status="approved", limit=20)
   b. accounts = list_accounts(distribution_enabled=true)
   c. For each account: get_account_performance(account_id, window_days=7)
       → 按 metrics.avg_views 排序
   d. 用 round-robin 把 articles 分给 top-N 账号
   e. create_distribute_task(article_ids=[...], account_ids=[...])

2. 回流阶段（每天跑一次）：
   a. 拉过去 24h status=succeeded 的 publish_records（要新增一个查询 tool 或扩展 list_articles）
   b. For each record:
      根据 platform_code 决定从哪拉 metrics（POC 期可用半自动飞书表录入 or stub）
      record_publish_metrics(record_id, {views, likes, ...})

3. notify_feishu(level="done", title="今日发文 Loop 完成",
   message=f"分发 {n_articles} 篇 / 回流 {n_metrics} 条")
```

POC 期回流可以用 stub（生成假数据），重点是验证 **数据流闭环 + Loop 编排正确**，真 metrics 接入留 v2。

### 5.3 评估器 Loop（按需，可作为单独运行的小 Loop）

```
Goal: 每周一跑一次模板 / 账号表现报告

工具：list_prompt_templates / get_template_performance / list_accounts / get_account_performance /
notify_feishu

流程：
1. For each prompt template: get_template_performance(id, window_days=7)
2. For each account: get_account_performance(id, window_days=7)
3. 整理成 markdown 周报
4. notify_feishu(level="info", title="模板/账号周报", message=...)
```

---

## 6. 飞书通知点

复用现有 `shared/feishu.py:notify_task_finished` 模式 + 新增 MCP tool `notify_feishu` 作为统一通知出口。

POC 期固定通知点：

| 时机 | level | 内容 |
|------|-------|------|
| 生文 Loop 启动 | info | 「[Loop 开始] 生文 Loop · 目标 5 篇」 |
| 生文 Loop 每完成 1 篇过审 | info | 「[Loop 进度] 已产出 X/5 篇」（可选；噪音） |
| 生文 Loop 结束 | done | 「[Loop 完成] 产出 X/5 篇过自评 · 用时 Y 分钟 · 用 Z token」 |
| 发文 Loop 结束 | done | 「[Loop 完成] 分发 X 篇 / 回流 Y 条 metrics」 |
| 自动审核 needs_rewrite | warn | （汇总单条 message，避免每篇一条消息）「今日 N 篇文章被自动审核标记重写」 |
| Loop 异常退出 | error | 「[Loop 失败] reason: ...」 |
| 评估器周报 | info | 「[周报] 模板表现 + 账号表现 markdown 摘要」 |

---

## 7. POC 范围 + 7-Day 节奏

| Day | 里程碑 | 验收 |
|-----|--------|------|
| **D1** | MCP server 骨架（FastMCP + 鉴权中间件）+ Catalog 类 3 个只读 tool（list_articles / list_question_items / list_prompt_templates）+ Claude Code 本机配 mcpServers 连通 | Claude Code 里 `/mcp` 看到 geo server connected，能列工具，调 list_articles 返回真实数据 |
| **D2** | 补完 Catalog 余下 4 个 tool + Action 类两个直调（compose_article / illustrate_article 复用 pipeline 节点 handler） | 在 Claude Code 里手动调一次 compose_article，能在 GEO UI 看到新文章 |
| **D3** | 新 API：`POST /api/articles/score`（用 ai_format_model 批量评分）+ MCP tool `score_recent_articles` + `submit_review_decision` + 新表 `auto_review_decisions` 迁移 | 测试评分返回的 JSON schema 稳定；手动调 submit_review_decision 在 DB 能看到 decision 记录 |
| **D4** | 写第一个 Loop 配方 `claude-loops/generation-loop.md` + 跑通端到端 + 飞书通知接通 | Claude Code 里 `/loop generation-loop.md` 跑完一轮，产出 ≥3 篇文章在未审核库 + 飞书收到完成通知 |
| **D5** | Action 类剩余 tool（create_distribute_task / notify_feishu）+ 发文 Loop 配方 + 跑通分发链路 | 跑完 generation + distribute 两个 Loop，新文章被分发到测试账号 |
| **D6** | 评估器 API（get_template_performance / get_account_performance）+ 反馈回流 stub 数据流跑通 | 评估器周报能产出（先用 stub metrics） |
| **D7** | 老板演示：分两段：(1) HTML 讨论稿回顾路径 (2) 本机现场跑 generation Loop + 飞书群截图 | 老板批准 → 决定 v2 方向（长跑服务器 / 真 metrics 接入 / Skill 包装等） |

---

## 8. 改动 checklist

**新增文件**：
- [ ] `server/mcp/server.py` —— FastMCP server 入口
- [ ] `server/mcp/tools/` —— 按 catalog/action/meta 分文件
- [ ] `server/mcp/auth.py` —— MCP token 校验
- [ ] `server/mcp/__init__.py`
- [ ] `server/app/modules/auto_review/` —— `models.py` + `router.py` + `service.py`（评分 + decision 持久化）
- [ ] `server/app/modules/performance/` —— `router.py` + `service.py`（聚合 metrics）
- [ ] `server/alembic/versions/0047_auto_review_decisions.py` —— 含可选的 `articles.metrics` JSON 字段
- [ ] `claude-loops/generation-loop.md` —— Loop 配方
- [ ] `claude-loops/distribute-loop.md`
- [ ] `claude-loops/weekly-report-loop.md`

**改动现有文件**：
- [ ] `server/app/main.py` —— 注册 `auto_review_router` + `performance_router`
- [ ] `server/app/core/config.py` —— 加 `GEO_MCP_TOKEN`、`GEO_MCP_TRANSPORT`（默认 stdio）配置
- [ ] `server/app/shared/feishu.py` —— 暴露通用 `notify(level, title, message)` 方法（给 MCP tool 用）
- [ ] `CLAUDE.md` —— 新模块说明 + MCP 启动方式
- [ ] `~/.claude.json`（用户本机）—— 配 mcpServers（不进 repo）

**不动**：
- pipelines / hot_lists / image_library / prompt_templates / question_bank / articles 路由本体
- 任何前端 tab（POC 不加 UI；运营通过飞书 + GEO 现有 tab 看结果）

**依赖新增**：
- `mcp[cli]` 或 `fastmcp` 加到 `requirements.txt`

---

## 9. 风险 / 已知问题

| 风险 | 缓解 |
|------|------|
| MCP 工具粒度设计偏差（太粗 LLM 难选 / 太细调用爆炸） | POC 期 15 个 tool 起步，D4-D7 跑 Loop 时观察 Claude 的 tool_use 频率，按需调 |
| Claude Code 本机断电 / 重启 = Loop 中断 | POC 接受；v2 上长跑服务器；Loop 配方要写成"幂等可续跑"（看 DB 状态决定从哪步开始） |
| 自动审核误杀真实文章 | 自动审核 decision 不直接动 `Article.review_status`，只写 `auto_review_decisions`；最终人审兜底 |
| MCP token 泄露 | 独立 service token、不复用 JWT；token 配在 env 不进 repo；v2 加吊销机制 |
| LLM 评分不稳定 | 用 ai_format_model（deepseek-v4-flash）+ `response_format=json_object` + 失败重试 1 次 |
| `compose_article` 绕过 pipeline 编排会不会跟现有发文 pipeline 冲突 | POC 期生文 Loop 用直调 ai_compose handler，**不创建 pipeline_run**；现有发文 pipeline 不动 |
| 回流 metrics POC 期是 stub | 接受；D7 演示老板时明说"数据接入留 v2"，先证明数据流 |
| Claude Code 每跑 1 个 Loop 会用多少 API 费用 | 用 ai_format_model 评分（便宜）；Claude Code 主 LLM 是 Opus，但 prompt 控制好（不要让它 read 全文）每轮可控 |

---

## 10. v2 / v3 路标

POC 跑通后，**v2** 候选改造：
- Skill 包装：把"生文 Loop / 发文 Loop"配方做成 Claude Code Skill（markdown frontmatter + 自动触发）
- 长跑服务器：部署 Claude Code 到内网长跑机器（tmux + 飞书心跳）
- 真 metrics 接入：头条/微信/抖音 平台 API 拉数据
- MCP token 吊销 + 审计 UI（GEO admin tab 加管理页）
- 多 Loop 并行（generation × N 主题、distribute × N 账号矩阵）

**v3** 远期：
- 飞书内 OpenClaw 风格交互：用户在飞书 @bot 触发 Loop / 查状态 / 改参数（要 GEO 暴露 HTTP MCP + 部署到公网或内网穿透）
- Skill 市场：把多个游戏 / 多个矩阵的 Loop 配方上架共享
- 多游戏支持：MCP server 加 game_context 参数，复用同一套 tools 但走不同上下文

---

## 11. 跟 Loop Engineering 讨论稿的关系

讨论稿的方案 C「Agent Town」描述「常驻 Loop Agent 各自迭代 + 共享任务池 + 状态机 + 预算闸 + 人工 checkpoint」，本稿是其 OpenClaw 范式具体落地：

| 讨论稿元素 | 本稿映射 |
|-----------|---------|
| 常驻 Loop Agent | Claude Code 进程跑 /loop |
| 任务池 + 状态机 | GEO DB（articles / auto_review_decisions / publish_records）|
| 预算闸 | Claude Code 自带 token 配额 + Anthropic API 月度上限 |
| 人工 checkpoint | "进未审核库等人审" + 飞书消息触达 |
| 选题 Loop（讨论稿 A.1） | 不在 POC，留 v2（要叠 hot_lists 借势抓） |
| 评审 Loop（讨论稿 A.2） | **本稿 POC 范围** —— `submit_review_decision` + `score_recent_articles` |
| 回流 Loop（讨论稿 A.3） | **本稿 POC 范围** —— `record_publish_metrics` + 评估器 API |

讨论稿写的"行业风口 + 双轨方案"对外讲故事时仍直接可用，**HTML 可视化稿**给老板看的部分不需要重做。

---

## 12. 附录：命名总览（防遗忘）

| 类型 | 名字 |
|------|------|
| MCP server 目录 | `server/mcp/` |
| MCP server 入口 | `python -m server.mcp.server` |
| GEO 新模块 | `server/app/modules/auto_review/` + `server/app/modules/performance/` |
| 新表 | `auto_review_decisions` |
| 新 API prefix | `/api/articles/{id}/auto-review` / `/api/articles/score` / `/api/publish-records/{id}/metrics` / `/api/prompt-templates/{id}/performance` / `/api/accounts/{id}/performance` / `/api/system/feishu-notify` |
| Loop 配方目录 | `claude-loops/` |
| Loop 配方文件 | `generation-loop.md` / `distribute-loop.md` / `weekly-report-loop.md` |
| 配置项 | `GEO_MCP_TOKEN` / `GEO_MCP_TRANSPORT`（stdio/http） |
| Alembic | `0047_auto_review_decisions` |
| 分支 | 复用 `feat/topic-loop-demo` 或新建 `feat/geo-mcp-loop`（建议新建） |
