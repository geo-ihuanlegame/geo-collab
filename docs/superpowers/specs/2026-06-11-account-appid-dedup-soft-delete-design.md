# 账号删除释放 app_id 占位 + 全局唯一 设计文档

- 日期：2026-06-11
- 模块：`server/app/modules/accounts/`
- 状态：设计已认可，待写实现计划

## 背景与问题

账号表 `Account` 已有软删（`is_deleted` / `deleted_at`），`delete_account()` 只翻标志位，并在仍有未完成发布记录时拒绝删除。

微信公众号（`wechat_mp`，API 型平台）的账号把 **AppID 存进 `platform_user_id`**，靠两道机制防重复登记：

1. 业务层查重：`create_api_account()` 检查 `(user_id, platform_id, platform_user_id=app_id)` 是否已存在。
2. DB 唯一约束：`uq_accounts_platform_user` on `(user_id, platform_id, platform_user_id)`。

**痛点**：两道机制都**把软删行也算进占位**——

- 业务查重的 SQL 没有 `is_deleted == False` 过滤；
- DB 唯一约束对软删行同样生效。

于是一旦"删除"了某个微信账号，它的 app_id 槽位仍被占用，用同一个 app_id 重新登记会抛 `该 AppID 已登记`。这就是"删了加不回来"。

浏览器平台（头条等）账号 `platform_user_id` 恒为 `NULL`，MySQL 唯一索引允许多个 NULL 互不冲突，所以查重实际只对 API 平台生效。

## 目标（已与用户确认）

- **核心目标**：删除一个 API 账号后，能用**同一个 app_id 重新登记**，且**保留发布历史**。
- **查重范围**：API 平台的 app_id 改为**全局唯一**——一个 app_id 全平台只能"活"一份（跨用户）。
- 非目标：真正的物理删行（被否决，见下）。

## 方案选型

考虑过三种，选定方案 1。

### 方案 1：软删释放身份槽位（选定）

删除时保留行（历史完整），但置空 `platform_user_id`、清 token、抹密钥 → `NULL` 即"不占任何身份槽"。唯一约束改全局。

- 优点：历史不丢、无外键改造、重新登记直接可用、全局唯一在 DB 层兜底、改动面最小。
- 缺点：死行会累积（成本极低）；死行 `api_credentials` 里仍留 app_id 字符串（密钥已抹）。

### 方案 2：真物理删除（否决）

`PublishRecord.account_id` 是无 `ON DELETE` 的 RESTRICT 外键。任何发布过的账号都无法直接物理删除，除非：

- 把 `publish_records.account_id` 改 nullable + `ON DELETE SET NULL`（孤立历史，丢失"是哪个账号发的"），或
- 级联删除记录（直接丢历史）。

与"保留历史"目标直接冲突，且要在 tasks 模块改外键/可空性，改动面最大。否决。

### 方案 3：混合（软删 + 管理员物理清除无历史账号）

默认走方案 1，额外给管理员一个"物理清除从未发布过的账号"能力。对有历史的真实账号不可用，主要覆盖误建账号场景。相对当前目标属于 YAGNI，暂不做。

## 详细设计（方案 1）

改动集中在 `accounts/service.py` + 一个 alembic 迁移 + 测试，前端基本不动。

### 1. 数据模型与迁移（风险最高）

- 唯一约束 `uq_accounts_platform_user` 从 `(user_id, platform_id, platform_user_id)` 改为**全局** `(platform_id, platform_user_id)`。
  - 浏览器账号 `platform_user_id` 恒为 NULL，互不冲突；约束实际只约束"活着的 API 账号"全局唯一。
- 不新增列。`NULL` 即"不占身份槽"。
- **迁移三步，顺序不能错**：
  1. **数据修复（含存量死行）**：对所有 `is_deleted=True` 的现存行，做和新删除逻辑一致的清理——
     - `platform_user_id = NULL`（释放老占位，顺带解决现存"删了加不回来"的存量账号）
     - `api_token_cache = NULL`
     - `api_credentials` 抹除 `app_secret`，保留 `app_id`
  2. **冲突探测**：检查剩余**活账号**中是否存在 `(platform_id, platform_user_id)` 非空重复（历史 per-user 允许两个用户登记同一 app_id；导入的浏览器账号也可能带非空 platform_user_id）。
     - 若有：迁移**中止并打印是哪几行（account id / platform / app_id）**，留人工裁决，**不擅自删数据**。
  3. 删旧约束 → 建新全局约束。
- 历史回溯：约束在 `0002` 曾是 `(platform_id, platform_user_id)`，`0008` 为多租户加上了 `user_id`。本次是有意改回全局——PR 描述需点明这是**语义变更**。

### 2. 删除行为（`delete_account`）

- 保留现有守卫：仍有 `pending/running/waiting_manual_publish/waiting_user_input` 发布记录时抛 `ClientError` 拒绝删除。
- 软删时除 `is_deleted=True` / `deleted_at=now` 外，额外：
  - `platform_user_id = None`（释放全局占位）
  - `api_token_cache = None`
  - `api_credentials` 抹除 `app_secret`，保留 `app_id`（死行不展示，仅留痕审计）
- `PublishRecord.account_id` 不动，账号行仍在 → 发布历史完整可查。

### 3. 查重与并发安全

- `create_api_account()` 与 `_ensure_app_id_available()` 的查重去掉 `user_id`，改为**全局 + 仅活账号**（`is_deleted == False`），更新场景自排除当前行。
  - 死行 `platform_user_id` 已为 NULL，`is_deleted` 过滤是双保险。
- 冲突文案改为「该 AppID 已被登记（全平台唯一）」，**不暴露占用者身份**（隐私）。
- 并发兜底：`db.flush()` 抛 `IntegrityError`（两请求同 app_id 抢注）时捕获转 `ConflictError`，不漏成 500。全局唯一后竞态窗口更值得堵。
- 产品语义副作用：app_id 要"转移"给另一用户，原用户须先删；符合"一个公众号一份活登记"。

### 4. 前端

- 基本无需改动：删除（admin、软删）与重新登记（`POST /api/accounts`）走的都是现有入口。唯一可见变化是删完能重新加回同一 app_id。
- 待核对项：
  - 删除确认弹窗文案别承诺"永久删除/不可恢复"（实为软删）。
  - 导出/导入路径对 `is_deleted` 行的处理（导出应排除已删行；导入查重需对齐全局规则）。

### 5. 测试（扩 `server/tests/test_accounts_api_wechat.py`）

- 软删微信账号 → `platform_user_id` 置空、`app_secret` 抹除、`app_id` 保留、`is_deleted` 置位、`api_token_cache` 清空。
- 删后用同一 app_id 重新登记 → 成功（旧行为是 409）。
- 全局唯一：用户 A 活着登记 app_id X，用户 B 再登记同一 X → 409（**新行为**；旧行为允许）。
- 有未完成发布记录时删除 → 仍 400 拒绝。
- 已删账号的发布历史仍可查（记录仍指向存在的账号行）。
- 迁移：验证约束切换 + 存量 `is_deleted=True` 行被清理（platform_user_id null、secret 抹除）。

## 主要风险

- 唯一约束切换在存量数据上可能失败（跨用户重复 app_id / 导入的浏览器账号带重复 platform_user_id）。迁移第 2 步"探测并中止"是安全阀。
- 这是**语义变更**（per-user → 全局唯一），PR 描述须讲清楚，避免协作者误判。

## 影响文件清单

- `server/app/modules/accounts/service.py` — `delete_account` / `create_api_account` / `_ensure_app_id_available`
- `server/app/modules/accounts/models.py` — `uq_accounts_platform_user` 定义
- `server/alembic/versions/XXXX_*.py` — 新迁移（数据修复 + 约束切换）
- `server/tests/test_accounts_api_wechat.py` — 扩测试
- （核对）`web/src/features/accounts/` 删除确认文案；`accounts/auth.py` 导出/导入对死行的处理
