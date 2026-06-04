# 02 · 产品设计文档

| 项 | 内容 |
|----|------|
| 关联文档 | [01 需求分析](./01-requirements-analysis.md) · [03 技术架构](./03-technical-architecture.md) · [05 API 接口](./05-api-reference.md) |

> 本文把需求落成"产品能看见的样子"：有哪些功能模块、信息怎么组织、关键流程怎么走、对象有哪些状态、谁能做什么。状态机与流程均与代码一致。

---

## 1. 产品地图（功能模块）

```
Geo 协作平台
├── 内容（Content）
│   ├── 文章：富文本编辑(Tiptap)、封面、AI 排版、状态(草稿/就绪/归档)
│   ├── 文章分组：用于批量轮询发布
│   └── 资源：图片上传（小文件 / 分块）
├── AI 生文（AI Generation）
│   ├── 生成会话：选 Skill + Prompt 模板 + 选题 → 批量出稿入库
│   ├── 问题库（选题池）：飞书多维表同步、按分类自动轮取
│   ├── Skill 管理
│   └── Prompt 模板管理（generation / ai_format 两种 scope）
├── 图片库（Image Library）
│   ├── 图片分类（StockCategory，对应 MinIO bucket）
│   ├── 图片素材 CRUD
│   └── 公开图片文件服务（供文章内嵌）
├── 账号（Accounts）
│   ├── 平台账号 CRUD
│   ├── 登录会话：noVNC 扫码登录 / 续登 / 状态检查
│   └── 授权导入导出（ZIP）
├── 任务（Tasks / 发布）
│   ├── 发布任务：single / group_round_robin
│   ├── 分配预览
│   ├── 执行 / 取消 / 重试
│   ├── 发布记录：状态追踪、人工接管、发布前确认
│   └── 任务日志 + SSE 实时流
└── 系统（System）
    ├── 用户管理（admin）
    ├── 系统状态（计数 + worker 在线）
    └── 操作审计日志（admin）
```

---

## 2. 信息架构（前端导航）

前端为单页应用（React 19 + Vite），按 `web/src/features/` 划分工作区（Workspace）：

| 导航 | 工作区组件 | 对应后端 | 角色可见 |
|------|-----------|----------|----------|
| 内容 | `content/ContentWorkspace` | `/api/articles`、`/api/article-groups`、`/api/assets` | operator/admin |
| AI 生文 | `ai-generation/AiGenerationWorkspace`（GenerateTab + SkillsPromptsTab） | `/api/generation/*`、`/api/skills`、`/api/prompt-templates` | operator/admin |
| 图片库 | `image-library/ImageLibraryWorkspace` | `/api/image-library/*` | operator/admin |
| 账号 | `accounts/AccountsWorkspace` | `/api/accounts/*` | operator/admin（导出/删除需 admin） |
| 任务 | `tasks/TasksWorkspace` | `/api/tasks/*`、`/api/publish-records/*` | operator/admin |
| 提示词 | `prompt-templates/PromptsWorkspace` | `/api/prompt-templates` | operator/admin |
| 系统 | `system/SystemWorkspace` | `/api/system/status` | admin |
| 审计日志 | `system/AuditLogsWorkspace` | `/api/audit-logs` | admin |
| 用户 | `auth/UsersWorkspace` | `/api/users` | admin |
| 登录 / 改密 | `auth/LoginPage`、`ChangePasswordPage` | `/api/auth/*` | 全员 |

> SPA 由 FastAPI 同时托管：非 `/api/` 路径返回 `web/dist/index.html`（`main.py`）。开发期前端跑 Vite（5173 端口，CORS 仅放行 5173）。

---

## 3. 核心对象与关系（产品视角）

| 对象 | 含义 | 关键关系 |
|------|------|----------|
| 用户 User | 平台成员，role=admin/operator | 拥有文章/账号/任务（user_id 隔离） |
| 平台 Platform | 一个内容平台（如 toutiao） | 1 平台 N 账号 |
| 账号 Account | 某平台的运营账号 + 登录态 | 属于用户与平台 |
| 文章 Article | 待发布内容（Tiptap 三份正文） | 可属多个图片分类、可入多个分组 |
| 文章分组 ArticleGroup | 一批文章，用于轮询发布 | 有序 items |
| 发布任务 PublishTask | "把文章发到账号"的编排单元 | 关联文章/分组 + 多账号 |
| 发布记录 PublishRecord | 一次"(文章,账号)"发布的执行实例 | 属于任务，有状态机 |
| 生成会话 GenerationSession | 一次 AI 批量生文 | 产出 article_ids |
| 审计日志 AuditLog | 一条操作留痕 | 关联用户/目标对象 |

> 数据层 ER 见 [04 数据库设计](./04-database-design.md)。

---

## 4. 关键流程

### 4.1 批量发布主流程（S1）

```
建任务(选分组+多账号, stop_before_publish?)
      │  POST /api/tasks   （client_request_id 幂等）
      ▼
分配预览（可选）  POST /api/tasks/preview  → 显示 哪篇→哪号
      │
      ▼
执行  POST /api/tasks/{id}/execute  →  202（立即返回）
      │           （生产由 worker 轮询抢占执行；开发可后台线程）
      ▼
逐条记录执行：全局并发≤5 → 每账号串行
      │
      ├─ 正常 → succeeded（记录 publish_url）
      ├─ stop_before_publish → waiting_manual_publish ──人工──> manual-confirm → succeeded/failed
      ├─ 验证码/登录失效 → waiting_user_input ──noVNC人工──> resolve-user-input → 重新 pending → 继续
      └─ 异常 → failed（记录 error_message + 截图）
      ▼
任务聚合终态：succeeded / partial_failed / failed / cancelled
      ▼
飞书告警推送（含成功/失败/总数）
```

实时观测：前端订阅 `GET /api/tasks/{id}/stream`（SSE，每秒推送 task / records / log / done 事件）。

### 4.2 账号登录 / 续登流程（S5）

```
发起登录会话  POST /api/accounts/{id}/login-session
      │   （或 /api/accounts/{platform_code}/login-session 新账号）
      ▼
会话状态机：pending → queued → starting → active
      │     （worker 抢占、抢 profile 锁、起 Xvfb/x11vnc/websockify）
      ▼
返回 novnc_url —— 运营打开远程浏览器，手动扫码/登录
      ▼
完成  POST .../finish  →  finish_requested → finishing → finished
      │   worker 抓取 storage_state.json，更新 Account.status=valid
      ▼
（过期后）check / relogin 复用同一状态机续登
```

### 4.3 AI 生文流程（S6）

```
建会话  POST /api/generation/sessions（选 skill + prompt + 选题/auto_count）
      │  校验 skill/prompt 启用、批量上限 20，返回 202 + session_id
      ▼
后台线程跑 LangGraph 管线：
   planner_node（准备任务清单，task_specs 由 _build_task_specs 构建）
      ▼
   parallel_write_node（ThreadPoolExecutor max_workers=4）
      · 每篇调 LiteLLM(GEO_AI_MODEL) 生成 Markdown
      · 提取 # 标题 → markdown_to_tiptap / markdown_to_html
      · create_article() 落库；问题库 item 标记 consumed
      ▼
   finalize_node：会话 status = done / failed
      ▼
前端轮询  GET /api/generation/sessions/{id}  观察状态与产出 article_ids
```

选题来源：手动选 item（按分类分组，每组一篇）或自动模式（按 `CategoryUsage` 最久未用的分类轮取、随机取样，不消耗 item）。问题库可从飞书多维表同步（`feishu_app_token` + `feishu_table_id`）。

---

## 5. 状态机定义（与代码一致）

### 5.1 文章状态 Article.status

```
draft（草稿） ──→ ready（就绪，可发布） ──→ archived（归档）
```
- 约束：`articles.models.py` CHECK `draft | ready | archived`，默认 `draft`。
- AI 排版期间另有 `ai_checking`（布尔锁）+ `ai_checking_started_at`（检测超时僵锁）+ `ai_format_error`。

### 5.2 发布任务状态 PublishTask.status

```
pending ──execute──→ running ──┬─→ succeeded       （全部记录成功）
                               ├─→ partial_failed  （有成功也有失败）
                               ├─→ failed          （无成功）
                               └─→ cancelled       （取消）
```
- 任务类型 `task_type`：`single | group_round_robin`。
- `cancel_requested` 标记请求取消；终态由 `aggregate_task_status` 计算。

### 5.3 发布记录状态 PublishRecord.status

```
pending ──claim──→ running ──┬─→ succeeded
                             ├─→ failed（error_message + 截图）
                             ├─→ cancelled
                             ├─→ waiting_manual_publish ──manual-confirm──→ succeeded/failed
                             └─→ waiting_user_input     ──resolve-user-input──→ pending（重跑）
```
- 重试：`retry` 对失败记录**新建**一条 `retry_of_record_id` 指向原记录的新记录（原记录不改）；重试记录不可再重试。
- 崩溃恢复：`lease_until` 过期的 `running` 记录在启动时被重置回 `pending`。

### 5.4 账号登录会话状态 AccountLoginSession.status

```
pending → queued → starting → active ─┬─ finish_requested → finishing → finished
                                       └─ cancel_requested → cancelling → cancelled
pending/queued ─────────────────────────────────────────────────────────→ failed
```
- 终态：`finished | cancelled | failed`；`previous_status` 用于取消时回滚。

### 5.5 生成会话状态 GenerationSession.status

```
pending → running → done
                 └─→ failed（error_message）
```

---

## 6. 角色与权限矩阵

两种角色：`admin`、`operator`（`User.role`）。除少数公开端点外均需 JWT cookie（`get_current_user`）。

| 能力 | operator | admin | 说明 |
|------|:--------:|:-----:|------|
| 登录 / 改密 / 看自己 | ✅ | ✅ | `/api/auth/*` |
| 文章 / 分组 / 资源 CRUD | ✅(自己) | ✅ | user_id 隔离；删除文章/分组需 admin |
| AI 生文 / Skill / 模板 | ✅ | ✅ | 系统级模板 `is_system` 仅 admin 可建 |
| 图片库 CRUD | ✅ | ✅ | 公开文件服务 `/api/stock-images/*` 无需鉴权（有意） |
| 账号 CRUD / 登录会话 / 续登 | ✅(自己) | ✅ | 导出/删除账号需 admin |
| 任务创建 / 执行 / 重试 | ✅(自己) | ✅ | user_id 隔离 |
| 用户管理 | ❌ | ✅ | `/api/users`，`require_admin` |
| 操作审计 | ❌ | ✅ | `/api/audit-logs`，`require_admin` |
| 系统状态 | ❌ | ✅ | `/api/system/status` |

> 非 admin 仅能访问本人 user_id 下的对象（账号/任务/文章在 service 层按 user_id 过滤并校验所有权）。

---

## 7. 关键交互规则（产品约束）

- **幂等**：文章、任务、AI 会话支持 `client_request_id`，重复提交不重复创建（并发重试安全）。
- **封面必填（头条）**：头条驱动在文章无封面时直接报错；产品上需在发布前提示。
- **正文三份同步**：编辑器存 `content_json`(Tiptap)、`content_html`、`plain_text` 三份，任一改动需同步——产品上由后端转换保证，前端只编辑 Tiptap。
- **分块上传阈值**：图片 ≥ 3MB 自动走分块上传（前端并发 4），单图上限 20MB。
- **PATCH 不清空**：文章 PATCH 传 `null` 不会清空字段（service 层过滤 None）——需要清空要用专用端点/哨兵值。
- **发布前确认 vs 人工接管**：`stop_before_publish` 是"正常停顿等确认"；`waiting_user_input` 是"非预期需人工"（验证码/失效）——两者语义不同，勿混用。

---

> 下一篇：[03 技术架构设计](./03-technical-architecture.md)。
