# 05 · API 接口文档

| 项 | 内容 |
|----|------|
| Base URL | 开发 `http://127.0.0.1:8000`；前端经 Vite 代理 `/api` → 8000 |
| 在线 Schema | FastAPI 自带 `/docs`(Swagger) 与 `/openapi.json`（部分内部端点 `include_in_schema=False`） |
| 关联文档 | [03 技术架构](./03-technical-architecture.md) · [04 数据库设计](./04-database-design.md) |

> 本文按路由模块组织全部 REST 端点。端点与鉴权来源：`server/app/main.py:create_app()` 的 `include_router(...)` 注册 + 各 `router.py`。

---

## 1. 通用约定

### 1.1 鉴权

- 登录后服务端写 **httpOnly cookie `access_token`**（JWT，HS256，TTL=`GEO_JWT_EXPIRE_HOURS` 默认 8h）。后续请求自动携带，无需手动加头。
- 除下列**公开端点**外，所有 `/api/*` 均需有效 cookie（路由级 `Depends(get_current_user)`）：
  - `GET /api/bootstrap`
  - `POST /api/auth/login`、`POST /api/auth/logout`
  - `GET /api/stock-images/*`（公开图片文件服务，前端 image-library 依赖，**有意公开**）
  - `/api/auth/*` 其余端点在路由内部自行校验 token（`me` / `change-password` / `users` 等）。
- **管理员专属**（`require_admin`，非 admin 返回 403）：`/api/auth/users*`、`/api/users/...`(更新他人)、`GET /api/system/status`、`GET /api/audit-logs`、账号导出/删除、文章/分组删除。
- 用户数据隔离：operator 仅能访问本人 `user_id` 下的文章/账号/任务（service 层过滤 + 所有权校验）。
- `must_change_password=true` 的用户访问受保护端点会收到 **403**，须先改密。

### 1.2 错误格式

统一 JSON：`{"detail": "<错误信息>"}`。状态码映射（`main.py` 全局处理器）：

| 场景 | 状态码 |
|------|--------|
| 参数/业务校验失败（`ClientError`/`ValidationError`/`AccountError`） | 400 |
| 未登录 / token 失效 | 401 |
| 权限不足 / 需改密 | 403 |
| 资源不存在 | 404 |
| 冲突（`ConflictError`，如幂等键重复、用户名已存在） | 409 |
| 触发限流 | 429 |
| 未捕获异常 | 500（`{"detail":"服务器内部错误"}`） |

### 1.3 限流与跨域

- 登录限流 **5 次/分钟**（slowapi）。
- CORS 仅放行 `http://127.0.0.1:5173`、`http://localhost:5173`，`allow_credentials=False`。

### 1.4 时间格式

所有 datetime 输出 ISO8601，末尾带 `Z` 表示 UTC（零时区）。

---

## 2. 引导 / 鉴权 / 用户

### 2.1 引导

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/api/bootstrap` | 公开 | 返回 `{needs_setup:true}`（无用户时）或 `{authenticated:false}`，前端判断是否需初始化 admin |

### 2.2 鉴权（`/api/auth`）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/api/auth/login` | 公开（限流 5/min） | body `{username, password}` → 写 cookie，返回 `{username, role, must_change_password, ai_format_preset_id}` |
| POST | `/api/auth/logout` | 公开 | 清 cookie |
| GET | `/api/auth/me` | cookie | 当前用户 `{id, username, role, must_change_password, ai_format_preset_id}` |
| POST | `/api/auth/change-password` | cookie | body `{old_password, new_password(≥8)}`，成功后清除 must_change_password |

### 2.3 用户管理（`/api/auth/users`，admin only）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/users` | 创建用户 `{username, password(≥8), role, display_name?}`；用户名重复 409；新用户 `must_change_password=true` |
| GET | `/api/auth/users` | 用户列表 |
| PATCH | `/api/auth/users/{user_id}` | 更新 `{is_active?, role?, display_name?, feishu_open_id?}`（不能改自己） |
| POST | `/api/auth/users/{user_id}/reset-password` | 重置密码 `{new_password(≥8)}`，置 `must_change_password=true` |

### 2.4 个人设置（`/api/users`）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| PATCH | `/api/users/me/settings` | cookie | 设置默认 AI 排版模板 `{ai_format_preset_id}`（校验该模板 scope=ai_format 且启用） |

---

## 3. 账号（`/api/accounts`）

> ⚠️ 路由顺序：`/{account_id:int}/login-session` 必须先于 `/{platform_code}/login-session` 注册（整数优先匹配）。

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/api/accounts/platforms` | cookie | 可用平台列表 |
| GET | `/api/accounts` | cookie | 账号列表（operator 仅自己） |
| POST | `/api/accounts/{platform_code}/login` | cookie | 由 storage_state 注册账号 |
| POST | `/api/accounts/{account_id}/login-session` | cookie | 发起既有账号登录/续登会话 → 返回 novnc_url |
| GET | `/api/accounts/{account_id}/login-session/{session_id}/status` | cookie | 轮询会话状态机 |
| POST | `/api/accounts/{account_id}/login-session/{session_id}/finish` | cookie | 完成登录，抓取 storage_state |
| DELETE | `/api/accounts/{account_id}/login-session/{session_id}` | cookie | 取消登录会话 |
| POST | `/api/accounts/{platform_code}/login-session` | cookie | 为新账号发起登录会话 |
| POST | `/api/accounts/export` | admin | 导出账号授权包（ZIP，上限 50MB） |
| POST | `/api/accounts/import` | cookie | 导入账号授权包 |
| POST | `/api/accounts/{account_id}/check` | cookie | 校验账号登录态 |
| POST | `/api/accounts/{account_id}/relogin` | cookie | 重新抓取 storage_state（续登） |
| PATCH | `/api/accounts/{account_id}` | cookie | 改 display_name |
| DELETE | `/api/accounts/{account_id}` | admin | 软删除（有活跃记录时拒绝） |

---

## 4. 文章 / 分组 / 资源

### 4.1 文章（`/api/articles`）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/api/articles` | cookie | 列表，支持搜索 `q`、分页 `skip/limit(≤200)`；返回含 `published_count`（成功发布数） |
| POST | `/api/articles` | cookie | 创建（支持 `client_request_id` 幂等）。不接受 stock_category，需后续 PATCH |
| GET | `/api/articles/{id}` | cookie | 详情（顺带清理过期 AI 锁） |
| PUT | `/api/articles/{id}` | cookie | 更新（AI 排版锁定中受限）。PATCH 语义下传 `null` 不清空字段 |
| DELETE | `/api/articles/{id}` | admin | 软删除 |
| POST | `/api/articles/{id}/cover` | cookie | 设封面（乐观版本校验） |
| POST | `/api/articles/{id}/ai-format` | cookie | 触发后台 AI 排版（**202**），可带 `preset_id` 选模板；前端轮询 `ai_checking` |

### 4.2 文章分组（`/api/article-groups`）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/api/article-groups` | cookie | 列表 |
| POST | `/api/article-groups` | cookie | 创建 |
| GET | `/api/article-groups/{id}` | cookie | 详情 |
| PUT | `/api/article-groups/{id}` | cookie | 更新 |
| DELETE | `/api/article-groups/{id}` | admin | 删除 |
| PUT | `/api/article-groups/{id}/items` | cookie | 原子替换分组内文章（带顺序） |

### 4.3 资源上传（`/api/assets` + `/api/chunked-assets`）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/api/assets` | cookie | 小文件（< 3MB）直传；magic-bytes 校验（PNG/JPEG/WebP/GIF） |
| POST | `/api/chunked-assets/upload-start` | cookie | body `{total_size}`，校验 ≤ 20MB，返回 `upload_id` |
| POST | `/api/chunked-assets/upload-chunk/{upload_id}` | cookie | 上传单分块（3MB，前端并发 4，可乱序） |
| POST | `/api/chunked-assets/upload-status/{upload_id}` | cookie | 查询进度（已收分块、是否完成） |
| POST | `/api/chunked-assets/upload-complete/{upload_id}` | cookie | 合并、服务端算 SHA256、格式校验、落 Asset。**须 re-raise 415 等 HTTPException，不可包成 500** |

> 上限：`MAX_ASSET_BYTES=20MB`（单图）、`MAX_ZIP_BYTES=50MB`（账号导出 ZIP）。前端**不算 SHA256**。

---

## 5. 任务 / 发布记录

### 5.1 任务（`/api/tasks`）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/api/tasks` | cookie | 列表，分页 `skip/limit(1–500)` |
| POST | `/api/tasks` | cookie | 创建任务（`single`/`group_round_robin`；支持 `client_request_id` 幂等，冲突 409） |
| POST | `/api/tasks/preview` | cookie | 预览分配（不落库）：哪篇→哪号 |
| GET | `/api/tasks/{id}` | cookie | 详情 |
| POST | `/api/tasks/{id}/execute` | cookie | 排队执行，**立即 202**；任务已终态返回 409 |
| POST | `/api/tasks/{id}/cancel` | cookie | 请求取消（置 cancel_requested） |
| GET | `/api/tasks/{id}/records` | cookie | 任务下所有记录 |
| GET | `/api/tasks/{id}/logs` | cookie | 任务日志，增量 `after_id`，分页 `limit(1–500)` |
| GET | `/api/tasks/{id}/stream` | cookie | **SSE** 实时流：每秒推 `task`/`records`/`log`/`done` 事件 |

### 5.2 发布记录（`/api/publish-records`）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/api/publish-records/{id}/manual-confirm` | cookie | 确认 `waiting_manual_publish` 记录 `{outcome, publish_url?, error_message?}` |
| POST | `/api/publish-records/{id}/resolve-user-input` | cookie | 解决 `waiting_user_input`（验证码/失效处理完）→ 重置 pending 续跑 |
| POST | `/api/publish-records/{id}/retry` | cookie | 重试失败记录 → 新建 `retry_of_record_id` 记录（原记录不变；重试记录不可再重试） |

---

## 6. AI 生文（`/api/generation`）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/api/generation/sessions` | cookie | 建会话即启动（**202** `{session_id, status}`）；校验 skill/prompt 启用；手动选题 `question_item_ids` 或自动 `auto_count`（互斥）；批量上限 20 |
| GET | `/api/generation/sessions/{id}` | cookie | 会话状态与产出 `article_ids` |
| GET | `/api/generation/question-pools` | cookie | 选题池列表（含 pending 计数） |
| POST | `/api/generation/question-pools` | cookie | 建选题池 |
| POST | `/api/generation/question-pools/{pool_id}/sync` | cookie | 从飞书多维表同步（返回 `{total, added, updated, skipped_consumed}`） |
| GET | `/api/generation/question-pools/{pool_id}/items` | cookie | 选题项列表（可筛 status） |

---

## 7. 技能 / 提示词模板

### 7.1 Skill（`/api/skills`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/skills` | 列出启用的 skill |
| POST | `/api/skills` | 创建 |
| PUT | `/api/skills/{id}` | 全量更新（name/content/description） |
| PATCH | `/api/skills/{id}` | 切换 is_enabled |
| DELETE | `/api/skills/{id}` | 软删除 |

### 7.2 Prompt 模板（`/api/prompt-templates`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/prompt-templates` | 可见模板（本人 + 系统），可按 `scope` 过滤 |
| POST | `/api/prompt-templates` | 创建（`is_system` 仅 admin） |
| PUT | `/api/prompt-templates/{id}` | 全量更新 |
| PATCH | `/api/prompt-templates/{id}` | 切换 is_enabled / scope / is_system |
| DELETE | `/api/prompt-templates/{id}` | 软删除 |

> `scope ∈ {generation, ai_format}`：generation 用于写作，ai_format 用于标题/配图识别。

---

## 8. 图片库

### 8.1 管理（`/api/image-library`，需鉴权）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/image-library/categories` | 建分类（同时建 MinIO bucket） |
| GET | `/api/image-library/categories` | 分类列表 |
| PATCH | `/api/image-library/categories/{id}` | 改 name/description/official_url |
| POST | `/api/image-library/images` | 上传图片（JPEG/PNG/WebP/GIF）→ MinIO + DB |
| GET | `/api/image-library/images` | 列表，可筛 `category_id` / `tag` |
| PATCH | `/api/image-library/images/{id}` | 改 tags / description |
| DELETE | `/api/image-library/images/{id}` | 删除（MinIO + DB） |

### 8.2 公开文件服务（`/api/stock-images`，**无需鉴权**）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/stock-images/{image_id}/file` | 从 MinIO 代理图片字节，供文章公开内嵌 |

---

## 9. 系统 / 审计（admin only）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/api/system/status` | admin | 系统健康：`service`、`directories_ready`、`article_count`、`account_count`、`task_count`、`pending_task_count`、`active_browser_sessions`、`worker_online`(30s 心跳)、`browser_ready`、`novnc_runtime_ready`（DB 故障时计数返回 -1） |
| GET | `/api/audit-logs` | admin | 审计查询：过滤 `user_id`/`action_prefix`/`target_type`/`target_id`/`start_at`/`end_at`，游标分页 `cursor`/`limit(≤500)`，返回 `{items, next_cursor}` |

> `/api/system/status` 返回系统计数与 worker 在线状态，是系统总览的数据来源。

---

## 10. 端点速查（按模块计数）

| 模块 | 前缀 | 端点数 | 鉴权基线 |
|------|------|--------|----------|
| 引导 | `/api/bootstrap` | 1 | 公开 |
| 鉴权 | `/api/auth` | 4 + 用户管理 4 | 公开/内部校验/admin |
| 个人设置 | `/api/users` | 1 | cookie |
| 账号 | `/api/accounts` | 14 | cookie（导出/删除 admin） |
| 文章 | `/api/articles` | 7 | cookie（删 admin） |
| 文章分组 | `/api/article-groups` | 6 | cookie（删 admin） |
| 资源 | `/api/assets` + `/api/chunked-assets` | 1 + 4 | cookie |
| 任务 | `/api/tasks` | 9 | cookie |
| 发布记录 | `/api/publish-records` | 3 | cookie |
| AI 生文 | `/api/generation` | 6 | cookie |
| 技能 | `/api/skills` | 5 | cookie |
| 模板 | `/api/prompt-templates` | 5 | cookie |
| 图片库 | `/api/image-library` | 7 | cookie |
| 图片文件 | `/api/stock-images` | 1 | 公开 |
| 系统 | `/api/system` | 1 | admin |
| 审计 | `/api/audit-logs` | 1 | admin |

> 权威清单以代码为准；新增端点请同步本表，并在 `/docs`（Swagger）核对参数与返回 schema。

---

> 下一篇：[06 开发指南](./06-development-guide.md)。
