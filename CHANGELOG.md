# 变更日志

本文件记录 **Geo 协作平台** 的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]

---

## [0.5.0] - 2026-05-22

### 新增
- **AI 生文模块**：新增 `skills`（技能库）、`prompt_templates`（提示词模板）、`generation_sessions`（生成批次）三张表，支持 LiteLLM + LangGraph 驱动的批量 AI 文章生成（迁移 0022）
- **图片库（MinIO）**：新增 `stock_categories`（素材分类）与 `stock_images`（素材图片）表；文章新增 `stock_category_id` 外键，支持文章关联素材分类（迁移 0024）
- **AI 审核锁**：文章表新增 `ai_checking` / `ai_checking_started_at` 字段，防止 AI 审核并发冲突（迁移 0023）
- **AI 智能排版**：新增 AI 格式化接口 `POST /api/articles/{id}/ai-format`，支持自动标题检测、段落分组和 Tiptap 转换；前端新增"AI 排版"按钮
- **头条驱动增强**：头条发布驱动新增对标题（`h1-h6`）和粗体（`bold`）格式的支持；优化段落插入逻辑，支持段落自动分组和间距紧凑化处理

### 变更
- `client_request_id` 唯一约束改为 `(user_id, client_request_id)` 联合约束，允许不同用户复用同一请求 ID（迁移 0020）
- `article_groups.name` 由全局唯一索引改为非唯一索引，唯一性约束收窄至用户维度 `(user_id, name)`（迁移 0021）
- AI 格式化引擎默认切换为中文 prompt，提升中文文本排版质量

### 修复
- 修复：AI 审核期间文章加锁防止并发编辑/删除操作
- 修复：AI 格式化后重新生成 HTML 和纯文本表示，确保多格式一致性
- 修复：`setAiChecking(true)` 同步更新 UI 状态，提供即时视觉反馈

---

## [0.4.0] - 2026-05-20

### 新增
- **全平台软删除**：`articles`、`article_groups`、`publish_tasks`、`publish_records` 统一新增 `is_deleted` / `deleted_at` 字段，支持逻辑删除（迁移 0019）

### 变更
- 账号登录 session 新增 `previous_status` 字段，取消操作时可恢复账号原始状态（迁移 0018）

---

## [0.3.0] - 2026-05-18

### 新增
- **资产软删除**：`assets` 表新增 `is_deleted` / `deleted_at` 字段（迁移 0015）
- **图片转换缓存**：`assets` 表新增 `webp_storage_key`、`webp_size`、`thumb_storage_key`、`thumb_size` 字段，支持 WebP 转码和缩略图存储（迁移 0016）
- **账号软删除**：`accounts` 表新增 `is_deleted` / `deleted_at` 字段（迁移 0017）

---

## [0.2.0] - 2026-05-15

### 新增
- **noVNC 远程浏览器会话**：新增 `browser_sessions` 和 `record_browser_sessions` 表，关联发布记录与 Xvfb/x11vnc 浏览器实例；`publish_tasks` 新增 `worker_id` / `worker_lease_until` 字段（迁移 0010）
- **Worker 心跳与任务控制**：新增 `worker_heartbeats` 表；`publish_tasks` 新增 `cancel_requested`、`worker_heartbeat_at` 字段；`browser_sessions` 新增 `platform_code`（迁移 0011）
- **账号登录会话**：新增 `account_login_sessions` 表，由 worker 独占管理登录流程，记录登录状态、noVNC URL 及结果（迁移 0012）
- **飞书集成**：用户表新增 `display_name`、`feishu_open_id` 字段，支持飞书消息通知（迁移 0013）
- **Schema 补全**：新增 `tags` / `article_tags` 表；`publish_tasks` 新增 `scheduled_at` 定时发布字段；`publish_records` 新增 `snapshot_title` / `snapshot_content_json` 快照字段；用户新增 `solo_mode` 单人模式开关（迁移 0014）

---

## [0.1.0] - 2026-05-12

### 新增
- **平台管理**：新增 `platforms` 表，支持多平台注册（`code`、`name`、`base_url`、`enabled`）（迁移 0001）
- **账号管理**：新增 `accounts` 表，关联平台，记录账号状态（`valid` / `expired` / `unknown`）、登录时间及 Playwright 浏览器配置路径（迁移 0002）
- **内容管理**：新增 `assets`（资源文件）、`articles`（文章，含 Tiptap JSON/HTML/纯文本）、`article_body_assets`（文章正文图片关联）三张表（迁移 0003）
- **文章组**：新增 `article_groups` 和 `article_group_items` 表，支持文章分组与轮询发布（迁移 0004）
- **任务与发布记录**：新增 `publish_tasks`（发布任务）、`publish_task_accounts`（任务账号关联）、`publish_records`（发布记录，含重试溯源与租约）、`task_logs`（任务日志，支持截图）表（迁移 0005）
- **用户认证**：新增 `users` 表，支持密码哈希、角色（`admin` / `operator`）、强制改密标志，以及基于 httpOnly JWT Cookie 的登录机制（迁移 0006）
- **全文搜索索引**：文章表新增 MySQL ngram 全文索引（`title`、`author`），以及账号平台状态、发布记录任务状态的复合索引（迁移 0007）
- **多租户权限隔离**：`accounts`、`articles`、`article_groups`、`publish_tasks`、`assets` 五张核心表统一新增 `user_id` 外键，实现用户维度数据隔离（迁移 0008）
- **全文搜索扩展**：文章全文索引扩展覆盖 `plain_text` 字段（迁移 0009）
- **Docker Compose 部署**：支持一键容器化部署，自动执行数据库迁移与初始用户播种；仅支持 Linux 服务器，noVNC 端口默认绑定 `127.0.0.1`

[Unreleased]: https://github.com/44lf/geo-collab/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/44lf/geo-collab/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/44lf/geo-collab/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/44lf/geo-collab/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/44lf/geo-collab/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/44lf/geo-collab/releases/tag/v0.1.0
