# 审核 + 方案分组 + 自动分发 —— 实现契约（2026-06-04）

并行 sub-agent 的唯一事实源。所有 agent 按本契约实现，**只改自己负责的文件**，互不越界。
设计已经前端 pencil 示意确认（demo.pen 的 S1/S2/S3）。

## 0. 全局约定（必须遵守 CLAUDE.md）

- MySQL only。service 层抛命名异常（`ClientError`/`ConflictError`/`ValidationError`/`AccountError`），**不要抛裸 `ValueError`**。
- 后端跑 `ruff check` + `ruff format` + `mypy server/app`（CI 硬门禁）。前端跑 `pnpm --filter @geo/web typecheck` + `build`。
- 测试需要 MySQL（`GEO_TEST_DATABASE_URL`）；本机可能没有 → 写测试，跑不了就在汇报里注明「未执行（缺 MySQL）」。
- 迁移头看 `server/alembic/versions/` 最新文件，down_revision 指向它。**只有 A1 写迁移**。

## 1. 数据模型（✅ 已由编排者完成，agent 勿重复）

> `Article.review_status` 字段 + 迁移 `0037_add_review_status_to_articles.py` 已写好并通过 ruff/mypy。
> A1 直接用这个字段，**不要**再改 `models.py` 的字段定义、**不要**再写迁移。

`Article` 字段（已存在）：

```python
review_status: Mapped[str] = mapped_column(
    String(20), default="approved", server_default="approved", index=True
)
# CheckConstraint: review_status in ('pending', 'approved')
```

**关键决策**：`server_default="approved"`。
- 既有文章 + 手工新建文章 = `approved`（不破坏现有发布/测试，人工内容视为已审）。
- **只有 AI 方案生成的文章被显式置为 `pending`**（A2 负责），进入「未审核」。
- 撤销审核可把任意文章打回 `pending`。

迁移：加列（server_default 自动让存量行 = approved，无需手动 backfill）+ CheckConstraint + index。

## 2. 后端 API 契约

### 2a. 文章审核（A1，articles 模块）

- `POST /api/articles/{id}/approve` → 置 `review_status='approved'`，`version+=1`，返回 `ArticleRead`。
- `POST /api/articles/{id}/revoke-approval` → 置 `pending`，返回 `ArticleRead`。
- `GET /api/articles` 新增 query `review_status: 'pending'|'approved'|None`（None=全部）。传入则在 `list_articles` 过滤。
- `ArticleListRead` / `ArticleRead` 新增 `review_status: str`。
- 审核动作走专用端点，**不要**让通用 `ArticleUpdate` 改 review_status（保持门禁完整）。

### 2b. 分组审核进度（A1，article-groups）

- `ArticleGroup` 读模型新增 `review_summary: {total: int, approved: int}`（统计组内未删除文章）。
  - 「整组已审核」= `approved == total and total > 0`。
- `POST /api/article-groups/{id}/approve-all` → 把组内所有未删除文章置 `approved`，返回组（含 review_summary）。

### 2c. 发布门禁 + 自动分发（A3，tasks 模块）

- **门禁**：`create_task`（或 `_validated_task_inputs`）里，对目标文章（single=article_id；group=组内全部文章）校验 `review_status=='approved'`；存在未过审 → 抛 `ValidationError("存在未通过审核的文章，无法发布")`（→400）。此门禁对所有建任务路径生效。
- **自动分发端点**：`POST /api/tasks/auto-distribute`
  - body：`{article_id?: int, group_id?: int, account_ids: list[int], name?: str}`（article_id / group_id 二选一）。
  - 行为：
    1. 校验审核门禁（同上；组要求整组过审）。
    2. 校验 account_ids 都是当前用户的 `status=='valid'` 账号（复用 `_validated_accounts` 逻辑）。
    3. 组装 `TaskCreate`：`task_type='group_round_robin'`（group_id 时）或 `'single'`（article_id 时）；`accounts=[{id, sort_order}]` 按传入顺序；`platform_code` 由账号平台推出（同现有逻辑）。
    4. 复用现有 `create_task(...)` 建任务，再触发执行（同 `POST /api/tasks/{id}/execute` 的后台路径，`stop_before_publish=False`）。
    5. 返回 `TaskRead`（含 records 预览即可）。
  - round-robin 语义沿用现有 `_build_assignments`（一篇文章轮流分到一个账号）。
- 新增 schema `AutoDistributeRequest` 放 `tasks/schemas.py`。

## 3. AI 生文：方案成组 + 标未审核（A2，scheme_executor.py）

在 `_aggregate_run` 汇总出 `run.article_ids`（status done）后：

1. 把这批生成文章全部置 `review_status='pending'`（AI 内容默认进未审核）。
2. 建一个 `ArticleGroup`：
   - `name = f"{run.created_at:%Y/%m/%d %H:%M} · {scheme.name}"`（取 `GenerationScheme.name`，按 run.scheme_id 查）。
   - 名称撞 `(user_id, name)` 唯一约束时，追加 ` #{run.id}`。
   - 用 `ArticleGroupItem`（sort_order 按 article_ids 顺序）把全部文章加进去。
3. best-effort：成组/标记失败只记日志，不影响 run 状态。用 run 的 user_id。
4. 复用 articles 模块现有的 group 创建逻辑/模型；不要新建表。

> 注：本文件末尾已有 `_assign_temp_cover_from_bucket`（临时封面，bucket=`cantingyangchengji`），勿动。A2 只加成组 + 标 pending。

## 4. 前端（A4，web/）

### 类型 & API
- `types.ts`：`ArticleSummary` / `Article` 加 `review_status: 'pending'|'approved'`；`ArticleGroup` 加 `review_summary?: {total:number; approved:number}`。
- `api/articles.ts`：`approveArticle(id)`、`revokeArticleApproval(id)`、`approveGroup(groupId)`；`listArticles` 支持 `review_status` 参数。
- `api/tasks.ts`：`autoDistribute({article_id?, group_id?, account_ids, name?})`。

### 内容工作台（ContentWorkspace.tsx + ArticleListItem.tsx + styles.css）
- 列表上方加 **未审核 / 已审核** tab（复用 `.aiTabBtn` 视觉），按 `review_status` 过滤列表。
- 文章行展示审核 badge（复用 `.badge`：approved=绿，pending=灰/黄）；未审核行加「通过审核」inline 按钮。
- 方案组行：显示 `review_summary` 进度（如「3/5 已审核」）；未满时「全部通过」按钮（调 approveGroup），整组过审后显示「自动分发」。
- 编辑区加审核条（未过审：灰 + 「通过审核」；已过审：绿 + 「撤销审核」+「自动分发」）。
- 「已审核」tab：勾选文章后顶部出现批量条「已选 N 篇 + 自动分发」。

### 自动分发弹窗（新文件 DistributeModal.tsx + styles.css）
- 复用 `.modal` / `.modalBackdrop` / `.modalHeader` / `.modalActions` 样式。
- 目标摘要（方案组 N 篇 / 单篇）。
- 账号区：`listAccounts()` 过滤 `status==='valid'`，按平台分组，每平台「全选」，默认全选；失效账号置灰不可选。
- round-robin 投放预览（N 篇 → M 个账号 轮流）。
- 确认 → `autoDistribute(...)` → 成功 toast + 关闭（可跳到发布任务页）。

参照 demo.pen 的 S1（未审核）/ S2（已审核+批量分发）/ S3（弹窗）。

## 5. 集成（编排者，最后做）
- 应用迁移 → 全后端 ruff/mypy → 前端 typecheck/build → 跑 pytest（若有 MySQL）。
- 修跨 agent 集成问题（contract 不一致优先按本文件）。
