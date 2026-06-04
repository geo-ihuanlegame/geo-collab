# 04 · 数据库设计文档

| 项 | 内容 |
|----|------|
| 数据库 | MySQL 8（`mysql+pymysql`），**无 SQLite 兼容** |
| 迁移工具 | Alembic（`server/alembic/versions/`，当前 0001 → 0034） |
| 关联文档 | [03 技术架构](./03-technical-architecture.md) · [05 API 接口](./05-api-reference.md) |

> 表结构以 `server/app/modules/*/models.py` 为准。所有时间列存 UTC（`core/time.utcnow`）。核心业务表统一采用 `is_deleted + deleted_at` 软删除，查询默认过滤 `is_deleted=False`。

---

## 1. 实体关系总览（ER）

```
User(用户) ──┬─< Account(账号) ─── Platform(平台)
             ├─< Article(文章) ──< ArticleBodyAsset >── Asset(资源)
             │        │  └──< (article_stock_categories) >── StockCategory ──< StockImage
             │        └──< ArticleGroupItem >── ArticleGroup
             ├─< PublishTask(任务) ──< PublishTaskAccount >── Account
             │        └──< PublishRecord(记录) ──< TaskLog ── Asset(截图)
             │                   └── retry_of_record_id (自引用)
             ├─< GenerationSession(生成会话) ── QuestionPool ──< QuestionItem
             │                                       └── CategoryUsage
             └─< AuditLog(审计)

Platform ──< Account / PublishTask
Account  ──< AccountLoginSession / BrowserSession / RecordBrowserSession / BrowserProfileLock
WorkerHeartbeat(独立)    Skill / PromptTemplate(独立)
```

按域分组的表清单（共约 30 张）：

| 域 | 表 |
|----|----|
| 系统 | `users`、`platforms`、`worker_heartbeats` |
| 账号 | `accounts`、`account_login_sessions`、`browser_sessions`、`record_browser_sessions`、`browser_profile_locks` |
| 文章 | `articles`、`article_body_assets`、`article_groups`、`article_group_items`、`assets`、`tags`、`article_tags`、`article_stock_categories` |
| 任务 | `publish_tasks`、`publish_task_accounts`、`publish_records`、`task_logs` |
| AI 生文 | `generation_sessions`、`question_pools`、`question_items`、`category_usages` |
| 图片库 | `stock_categories`、`stock_images` |
| 技能/模板 | `skills`、`prompt_templates` |
| 审计 | `audit_logs` |

---

## 2. 系统域

### users — 用户

| 列 | 类型 | 约束 / 默认 | 说明 |
|----|------|-------------|------|
| id | int | PK | |
| username | varchar(80) | unique, index | 登录名 |
| password_hash | varchar(255) | | bcrypt |
| role | varchar(20) | default `operator` | `admin` / `operator` |
| is_active | bool | default true | 停用即拒登 |
| must_change_password | bool | default true | 为真时受保护接口返回 403，强制改密 |
| display_name | varchar(200) | null | |
| feishu_open_id | varchar(200) | null | 飞书用户标识 |
| solo_mode | bool | default false | |
| ai_format_preset_id | int | null | 用户默认 AI 排版模板 |
| created_at / last_login_at | datetime | | |

### platforms — 平台（种子数据）

| 列 | 类型 | 约束 | 说明 |
|----|------|------|------|
| id | int | PK | |
| code | varchar(50) | unique, index | 平台编码，如 `toutiao` |
| name | varchar(100) | | 显示名 |
| base_url | varchar(500) | null | |
| enabled | bool | default true | |

### worker_heartbeats — Worker 心跳

| 列 | 类型 | 约束 | 说明 |
|----|------|------|------|
| worker_id | varchar(100) | PK | `hostname-pid` |
| hostname / pid | varchar/int | null | |
| heartbeat_at | datetime | index | 30s 内有心跳 → 判定在线 |

---

## 3. 账号域

### accounts — 平台账号

| 列 | 类型 | 约束 / 默认 | 说明 |
|----|------|-------------|------|
| id | int | PK | |
| user_id | int | FK users, index | 所有者 |
| platform_id | int | FK platforms, index | |
| display_name | varchar(200) | | 自定义显示名 |
| platform_user_id | varchar(200) | null | 平台侧 UID |
| status | varchar(30) | default `unknown`, index | CHECK `valid/expired/unknown` |
| last_checked_at / last_login_at | datetime | null | |
| state_path | varchar(1000) | | `storage_state.json` 相对路径 |
| note | text | null | |
| is_deleted / deleted_at | bool/datetime | 软删除 | |
| created_at / updated_at | datetime | | |

- 唯一约束：`(user_id, platform_id, platform_user_id)`。
- 删除前置校验：存在活跃记录（pending/running/waiting_*）时拒绝软删。

### account_login_sessions — 交互式登录会话（Worker 驱动状态机）

| 列 | 类型 | 说明 |
|----|------|------|
| id | varchar(12) PK | 会话 ID |
| account_id | int FK(accounts, CASCADE), index | |
| platform_code / account_key | varchar | 定位 profile |
| channel | varchar(80) default `chromium` | |
| executable_path | varchar(500) null | |
| status | varchar(30) index | `pending→queued→starting→active→finish_requested→finishing→finished` / `…→cancel_requested→cancelling→cancelled` / `failed` |
| browser_session_id | varchar(12) null, index | 关联浏览器会话 |
| novnc_url | varchar(500) null | 远程接管入口 |
| logged_in | bool null | |
| result_url / result_title | varchar | 完成时抓取 |
| error_message | text null | |
| queue_reason | varchar(500) null | 排队原因（profile 锁占用等） |
| previous_status | varchar(30) null | 取消时回滚 |
| worker_id | varchar(100) null, index | |
| created_at / updated_at | datetime | |

### browser_sessions — 跨进程浏览器会话注册表

Worker 写、API 读，使 Web 进程能查询 Worker 持有的会话。字段：`id(PK,12)`、`platform_code`、`account_key`、`profile_key`、`display`、`novnc_url`、`started_at`、`last_activity_at`、`worker_id`、`keep_alive`、`stop_requested`。

### record_browser_sessions — 记录 ↔ 浏览器会话映射

`record_id(PK, FK publish_records CASCADE)` → `session_id(FK browser_sessions CASCADE)`。

### browser_profile_locks — Chrome profile 跨进程锁

| 列 | 类型 | 说明 |
|----|------|------|
| profile_key | varchar(255) PK | 一个持久化 profile 目录 |
| owner_kind | varchar(40) | 占用方类型（login / publish 等） |
| owner_id | varchar(80) | 占用方 ID |
| worker_id | varchar(100) null, index | |
| queue_reason | varchar(500) null | |
| acquired_at / heartbeat_at | datetime | |
| lease_until | datetime, index | 租约（默认 900s，心跳续租） |

---

## 4. 文章域

### articles — 文章（核心表）

| 列 | 类型 | 约束 / 默认 | 说明 |
|----|------|-------------|------|
| id | int | PK | |
| user_id | int | FK users, index | |
| title | varchar(300) | index | |
| author | varchar(200) | null | |
| cover_asset_id | varchar(64) | FK assets, null | 封面（头条必填） |
| **content_json** | text | default `{}` | **Tiptap 编辑器 JSON** |
| **content_html** | text | default `''` | **渲染 HTML** |
| **plain_text** | text | default `''` | **发布纯文本** |
| word_count | int | default 0 | |
| status | varchar(30) | default `draft`, index | CHECK `draft/ready/archived` |
| client_request_id | varchar(80) | null | 幂等键，`(user_id, client_request_id)` unique |
| version | int | default 1 | 乐观版本（封面更新等） |
| ai_checking | bool | default false, index | AI 排版锁 |
| ai_checking_started_at | datetime | null | 检测僵锁超时 |
| ai_format_error | text | null | AI 排版错误 |
| stock_category_id | int | FK stock_categories, null | 旧单分类（多对多见关联表） |
| is_deleted / deleted_at | | 软删除 | |
| created_at / updated_at | datetime | | |

> **三份正文并行存储**是本表的核心设计：编辑取 `content_json`、展示取 `content_html`、发布取 `plain_text`。改任一份需同步另两份（后端 `converter.py` / `parser.py` 保证）。

### 其余文章域表

| 表 | 关键列 | 说明 |
|----|--------|------|
| `assets` | id(varchar64,PK uuid)、user_id、filename、ext、mime_type、size、sha256(index)、storage_key(unique)、width/height、webp_storage_key/webp_size、thumb_storage_key/thumb_size、软删除 | 上传资源；SHA256 服务端算，含 WebP 全幅与 400px 缩略图派生 |
| `article_body_assets` | article_id、asset_id、position、editor_node_id | 正文图片位置关联（Tiptap 节点） |
| `article_groups` | user_id、name、description、version、软删除 | `(user_id, name)` 唯一 |
| `article_group_items` | group_id、article_id、sort_order | `(group_id, article_id)` 唯一，带排序 |
| `tags` / `article_tags` | | 标签多对多 |
| `article_stock_categories` | article_id、stock_category_id | 文章 ↔ 图库栏目 多对多（CASCADE） |

---

## 5. 任务域

### publish_tasks — 发布任务

| 列 | 类型 | 说明 |
|----|------|------|
| id | int PK | |
| user_id | int FK, index | |
| name | varchar(300) | |
| task_type | varchar(40) index | CHECK `single/group_round_robin` |
| status | varchar(40) default `pending`, index | CHECK `pending/running/succeeded/partial_failed/failed/cancelled` |
| platform_id | int FK, null, index | |
| article_id | int FK, null | single 用 |
| group_id | int FK, null | group_round_robin 用 |
| stop_before_publish | bool default false | 发布前停顿 |
| client_request_id | varchar(80) null | `(user_id, client_request_id)` unique |
| worker_id | varchar(100) null, index | 认领的 worker |
| worker_lease_until | datetime null | 认领租约（10min，CAS 续租） |
| worker_heartbeat_at | datetime null | |
| cancel_requested | bool default false | |
| scheduled_at | datetime null | 计划发布时间 |
| is_deleted/deleted_at、created_at、started_at、finished_at | | |

### publish_records — 发布记录（执行实例）

| 列 | 类型 | 说明 |
|----|------|------|
| id | int PK | |
| task_id | int FK, index | |
| article_id / platform_id / account_id | int FK, index | |
| status | varchar(40) default `pending`, index | CHECK `pending/running/waiting_manual_publish/waiting_user_input/succeeded/failed/cancelled` |
| publish_url | varchar(1000) null | 成功结果 URL |
| error_message | text null | 失败原因 |
| queue_reason | varchar(500) null | 排队原因 |
| snapshot_title / snapshot_content_json | varchar/text null | 发布时内容快照 |
| retry_of_record_id | int FK(self) null | 重试指向原记录（自引用） |
| started_at / finished_at | datetime null | |
| lease_until | datetime null | 执行租约，崩溃后据此重置 pending |
| is_deleted/deleted_at | | 软删除 |

### 其余任务域表

- `publish_task_accounts`：`task_id`、`account_id`、`sort_order`，`(task_id, account_id)` 唯一 —— 任务的目标账号集（轮询顺序）。
- `task_logs`：`task_id`、`record_id(null)`、`level`(CHECK `info/warn/error`)、`message`、`screenshot_asset_id(FK assets)`、`created_at` —— 任务/记录日志，可挂失败截图。

---

## 6. AI 生文域

| 表 | 关键列 | 说明 |
|----|--------|------|
| `generation_sessions` | user_id、skill_id、prompt_template_id、extra_instruction、status(CHECK `pending/running/done/failed`)、article_ids(JSON文本)、question_item_ids(JSON文本)、pool_id、auto_count、error_message、completed_at | 一次批量生文；产出 article_ids；手动选题与 auto_count 互斥 |
| `question_pools` | user_id、name、feishu_app_token、feishu_table_id、last_synced_at、软删除 | 选题池 = 一张飞书多维表 |
| `question_items` | pool_id、record_id、fields(JSON)、question_text、category(index)、status(CHECK `pending/consumed`)、article_id、synced_at | 一条 = 一篇文章；`(pool_id, record_id)` 唯一去重；消费后不复活 |
| `category_usages` | pool_id+category(复合PK)、last_used_at | 自动选题"最久未用分类优先" |

---

## 7. 图片库域

| 表 | 关键列 | 说明 |
|----|--------|------|
| `stock_categories` | id、name(unique)、bucket_name(unique,63)、description、official_url | 一个分类 = 一个 MinIO bucket；official_url 用于配图署名 |
| `stock_images` | category_id(index)、minio_key(unique)、filename、description、tags(JSON)、width/height | 素材；通过公开端点 `/api/stock-images/{id}/file` 对外提供 |

---

## 8. 技能 / 模板 / 审计

### skills

`id`、`name`、`description`、`content(text)`、`storage_path`(废弃)、`file_stats`(废弃)、`is_enabled`、`is_deleted`。Skill 内容现以 `content` 列承载（早期文件夹形态已收敛进 DB）。

### prompt_templates

`id`、`name`、`content`、`scope`(index, `generation`/`ai_format`)、`user_id`(null=系统模板)、`is_system`、`is_enabled`、`is_deleted`。与 Skill 是**组合关系**，不从属。

### audit_logs — 审计日志

| 列 | 类型 | 说明 |
|----|------|------|
| id | int PK autoincrement | |
| user_id | int FK, null, index | 失败登录可为空 |
| username | varchar(80) null | 冗余存名，用户删后不丢归属 |
| action | varchar(80) index | 如 `user.login`、`account.create` |
| target_type | varchar(40) index | `user`/`account`/`system`… |
| target_id | varchar(80) null, index | 兼容 int 与 UUID |
| payload_json | JSON null | 变更摘要，敏感字段（password/token/secret…）由 service 脱敏 |
| ip_address | varchar(45) null | 容纳 IPv6 |
| user_agent | varchar(255) null | |
| created_at | datetime index | |

复合索引：`(user_id, created_at)`、`(action, created_at)`、`(target_type, target_id)` —— 支撑审计端点的过滤 + 游标分页。

---

## 9. 全文检索（FTS）

- MySQL `FULLTEXT INDEX ... WITH PARSER ngram`（中文分词），无 Elasticsearch。
- 覆盖文章标题与 `plain_text`（迁移 `0007_fts_indexes`、`0009_fts_add_plain_text`）。
- FTS 与迁移正确性由 `server/tests/test_fts_and_migrations.py` 验证。

---

## 10. 迁移与约定

- 迁移目录 `server/alembic/versions/`，从 `0001_create_platforms` 到 `0034_audit_logs`（含 FTS、软删除补列、AI 生文、图片库、问题库、审计等增量）。
- `alembic.ini` 的 `sqlalchemy.url` 是占位符，运行时由 `get_database_url()` 覆盖（优先 `GEO_DATABASE_URL`，否则拼 `GEO_DB_*`）。
- 升级：`alembic upgrade head`；Docker 启动自动执行。
- **约定**：
  - 时间列存 UTC；
  - 核心表软删除（`is_deleted`+`deleted_at`）；
  - 幂等键 `client_request_id` 配 `(user_id, client_request_id)` 唯一约束；
  - MySQL TEXT 列不能有字面 DEFAULT（用 Python 端 default）；
  - 最新迁移头随版本变化，**勿在文档写死版本号**，以 `versions/` 最新文件为准。

---

> 下一篇：[05 API 接口文档](./05-api-reference.md)。
