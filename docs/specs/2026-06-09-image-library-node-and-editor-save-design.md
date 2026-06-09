# 图片库主推/陪衬重构 + AI配图节点 + 编辑器图片保存 设计规格

**日期：** 2026-06-09
**状态：** 已批准（设计）
**改动范围：** 后端（模型/迁移/路由/新 pipeline 节点）+ 前端（图片库、智能体管理节点编辑器、内容编辑器）

> 配套视觉稿见根目录 `demo.pen`（已定稿，本次实现**不修改** demo）。

---

## 背景

当前图片库与配图存在三个结构性问题：

1. **图片库扁平**：`StockCategory`（栏目=一个 MinIO bucket）→ `StockImage`，没有「主推/陪衬」的语义区分。运营需要把「自家主推的一个游戏」与「文章里顺带提到的陪衬游戏」从结构上分开。
2. **配图不可组合**：配图逻辑只在 `ai_generation/scheme_executor.py` 内部隐式发生（scheme 运行后自动对全部 bucket 配图），无法作为「智能体管理」里的一个节点与其它节点组合编排。
3. **编辑器入口错位**：编辑器有「AI格式」按钮 + 「图片栏目」选择，但运营真正需要的是「把正文里选中的图片存进图库」。

本次改动：

- 图片库加 **主推/陪衬** 两类（`StockCategory.kind`），前端按两 tab 展示。
- 新增可组合的 **AI配图节点**（`ai_illustrate`），复用现有 AI格式 后端做配图。
- 编辑器去掉 AI格式 + 图片栏目，新增「图片保存」（WPS 风格保存框）。

### 关键语义（运营澄清，务必遵守）

- **主推**：运营**手动选定的一个游戏**，稳定。在配图节点里用一个下拉选定。
- **陪衬**：**不手动选**，一篇文章里可能出现多个、且不稳定（以文章实际提及为准）。哪些陪衬游戏命中由 **AI格式 后端函数**（[`run_ai_format`](../../server/app/modules/articles/ai_format.py)）按文章内容判定。
- 「AI格式」**只删前端按钮**，其后端 `run_ai_format` 保留并被配图节点复用。

---

## 决策记录

| 决策 | 结论 | 理由 |
|------|------|------|
| 存量栏目 `kind` 迁移默认值 | **全部回填 `companion`（陪衬）**，主推由管理员手工挑 | 主推是少量精选，逐个指定更准 |
| 主推图片如何进文章 | **主推 + 陪衬都当候选栏目丢给 AI 统一决定**插哪几张/插哪里 | 最大化复用 `run_ai_format` 现有 `candidate_categories` 逻辑 |
| `article_stock_categories` M2M 去留 | **保留休眠**（不 drop、不写迁移删除） | 照 skills 表惯例；规避数据/回滚风险 |
| `from-url` 兜底保存端点 | 列为 PR4 **可选项**，主路径不依赖 | 同源图片 `fetch`→Blob→现有上传端点即可 |

---

## 改动一：数据模型（迁移 `0043`）

`StockCategory` 增加 `kind` 列：

```python
# server/app/modules/image_library/models.py — StockCategory
kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="companion")
# 取值 'main'(主推) / 'companion'(陪衬)
```

迁移 `0043_stock_category_kind.py`（照 `0042_task_type_article_round_robin.py` 的 CHECK 写法）：

- `add_column('stock_categories', Column('kind', String(20), nullable=False, server_default='companion'))`
- 加 CHECK 约束：`kind IN ('main', 'companion')`
- 存量行由 `server_default` 自动回填 `companion`，无需额外 UPDATE。
- `downgrade`：drop CHECK + drop column。

`StockImage` **不改**（kind 随所属栏目继承）。

---

## 改动二：图片库 API（`image_library/router.py`）

- `CategoryCreate` / `CategoryRead` / `CategoryUpdate` 增加 `kind` 字段。
  - `CategoryCreate.kind`：默认 `'companion'`，校验 ∈ {`main`,`companion`}。
  - `CategoryUpdate.kind`：可选，允许主推↔陪衬互转。
- `GET /api/image-library/categories?kind=main|companion`：按 kind 过滤（`kind` 省略时返回全部，向后兼容）。
- 其余端点（images 上传/列出/删除/更新、`/api/stock-images/{id}/file`）**不变**。

---

## 改动三：新 pipeline 节点 `ai_illustrate`（UI 名「AI配图」）

### 注册
- 新建 `server/app/modules/pipelines/nodes/ai_illustrate.py`，模块底部 `register("ai_illustrate", run_ai_illustrate)`。
- 在 `server/app/modules/pipelines/nodes/__init__.py` 增加 import 触发注册（`main.py` 顶部已 `import ...pipelines.nodes`）。

### 配置（config）
对应 demo Frame 1 配置面板：

| 字段 | 类型 | 含义 |
|------|------|------|
| `main_category_id` | int（必填，须为 kind=main 栏目） | 主推游戏（手选一个） |
| `include_companion` | bool（默认 true） | 「陪衬配图」开关 |
| `preset_id` | int（可选） | ai_format scope 的提示词模板 |

### 输入 / 输出
- 输入：上游 `article_ids`（`flow_meta` 默认透传整个上游输出）。
- 输出：`{"article_ids": [...], "errors": [...]}`，`NodeResult.article_ids` 透传给下游 `to_review` / `distribute`。

### 行为（照 `scheme_executor.py:329-357` 的成熟调用法）
```
对每个 article_id（ThreadPoolExecutor(max_workers=4) 并发，每线程自建 session）：
  1. 短事务：置 ai_checking 锁（ai_checking=True, ai_checking_started_at=lock_started_at,
     ai_format_error=None），commit，close。
  2. 构造候选栏目 candidate_categories =
       [主推栏目] + (include_companion ? 所有 kind=companion 栏目 : [])
     —— 用 ai_format.py 新增 helper category_contexts_for(db, main_category_id, include_companion)
        （复用现有私有 _category_context）。
  3. run_ai_format(article_id, include_images=True, lock_started_at=lock_started_at,
                   preset_id=preset_id, user_id=ctx.user_id,
                   candidate_categories=candidate_categories)
单篇失败收进 errors（不中断，交由运行聚合为 partial_failed），与 ai_compose 一致。
```

并发安全：每篇文章独立置锁、`run_ai_format` 内部自建 `SessionLocal` 且有锁指纹复核（`_article_lock_matches`），按 article 隔离，节点并发安全。

### ai_format.py 新增 helper
```python
def category_contexts_for(db, *, main_category_id, include_companion) -> list[dict]:
    """主推栏目 + (可选)全部陪衬栏目 的 {id,name,description} 候选列表。
    复用 _category_context；main_category_id 必须是 kind=main 的栏目。"""
```
现有 `all_category_contexts` 保留（scheme 自动配图仍用它，本次不动）。

---

## 改动四：前端 — 图片库 + 节点编辑器

### 图片库（`web/src/features/image-library/ImageLibraryWorkspace.tsx`）
- 顶部加 **主推游戏 / 陪衬游戏** 两 tab，切 tab 即 `GET /categories?kind=main|companion`。
- tab 下：栏目列表 → 缩略图网格，沿用现有卡片 / lightbox / 骨架屏 / 空状态（见 `2026-05-26-image-library-ui-upgrade-design.md`）。
- 新建栏目时带 `kind`（在当前 tab 下创建即归该类）。
- 对应 demo「图片库」帧。

### 节点编辑器（`web/src/features/pipelines/PipelineEditor.tsx` + `web/src/api/pipelines.ts`）
- 注册「AI配图」节点类型，配置面板：
  - 主推下拉（拉 `?kind=main` 栏目）
  - 「陪衬配图」开关（`include_companion`）
  - 提示词模板下拉（ai_format scope，可空）
- 对应 demo Frame 1。

---

## 改动五：前端 — 内容编辑器改造 + 图片保存

### 删除
- `web/src/components/editor/EditorToolbar.tsx`：删末尾「AI格式 / AI格式·配图」按钮（约 L85–96），及 `onAiFormat` / `aiChecking` / `aiFormatRemainingSeconds` / `stockCategorySelected` 入参。
- `web/src/features/content/ContentWorkspace.tsx`：删「图片栏目」选择 UI 及其 wiring（`article.stock_categories` 数据休眠，不再编辑）。
- 后端 `run_ai_format` 及其手动触发路由**保留**（被节点复用 / 路由休眠），仅断开编辑器这一处调用。

### 新增「图片保存」（位置 = 原 AI格式 处）
- EditorToolbar 该位置换成「图片保存」按钮，仅当选中图片节点（`editor.isActive("image")`）可点。
- 点击 → 弹 WPS 风格保存框（新组件 `web/src/components/editor/ImageSaveDialog.tsx`，对应 demo Frame 4/5）：
  1. 选 **主推 / 陪衬**（kind tab）
  2. 选 **栏目**（按 kind 过滤的栏目列表）
  3. 填 **文件名**（+ 可选标签 / 描述）→ 保存
- 保存链路（基本零后端改动）：
  - 取选中图 `editor.getAttributes("image").src`
  - `fetch(src)` → `Blob`（同源的 `/api/stock-images/...`、文章附件均可取）
  - 调**现有** `POST /api/image-library/images`（multipart：`file`=blob, `category_id`, `filename`）
- **可选兜底（PR4 可选）**：外链图可能被 CORS 挡 → 新增 `POST /api/image-library/images/from-url`（服务端抓取 url 再存）。主路径不依赖。

---

## 分 PR 落地

每个 PR 独立可发布；PR2/3/4 依赖 PR1。

| PR | 内容 | 依赖 |
|----|------|------|
| **PR1** | 迁移 `0043` + `kind` 进 models/schemas/router + `?kind=` 过滤 + 后端测试 | — |
| **PR2** | `ai_illustrate` 节点 + 注册 + `category_contexts_for` helper + flow_meta + 测试（mock litellm / run_ai_format） | PR1 |
| **PR3** | 图片库前端 主推/陪衬 tabs + PipelineEditor「AI配图」节点配置 UI | PR1 |
| **PR4** | 编辑器去 AI格式 / 图片栏目 + 「图片保存」弹框；（可选 `from-url` 兜底） | PR1 |

---

## 改动文件汇总

| 文件 | 改动类型 | PR |
|------|----------|----|
| `server/alembic/versions/0043_stock_category_kind.py` | 新增迁移 | PR1 |
| `server/app/modules/image_library/models.py` | `StockCategory` 加 `kind` | PR1 |
| `server/app/modules/image_library/router.py` | schemas 加 `kind` + `?kind=` 过滤 | PR1 |
| `server/app/modules/articles/ai_format.py` | 新增 `category_contexts_for` helper | PR2 |
| `server/app/modules/pipelines/nodes/ai_illustrate.py` | 新增节点 | PR2 |
| `server/app/modules/pipelines/nodes/__init__.py` | import 触发注册 | PR2 |
| `server/app/modules/pipelines/flow_meta.py` | 节点透传/跳过元数据（如需要） | PR2 |
| `web/src/features/image-library/ImageLibraryWorkspace.tsx` | 主推/陪衬 tabs | PR3 |
| `web/src/api/image-library.ts` | `kind` 字段 + `?kind=` 查询 | PR3 |
| `web/src/features/pipelines/PipelineEditor.tsx` | 「AI配图」节点配置 UI | PR3 |
| `web/src/api/pipelines.ts` | 节点类型/配置类型 | PR3 |
| `web/src/components/editor/EditorToolbar.tsx` | 删 AI格式、加图片保存按钮 | PR4 |
| `web/src/features/content/ContentWorkspace.tsx` | 删图片栏目选择 + 接图片保存弹框 | PR4 |
| `web/src/components/editor/ImageSaveDialog.tsx` | 新增 WPS 风格保存框 | PR4 |
| `server/app/modules/image_library/router.py` | （可选）`from-url` 端点 | PR4 |

---

## 测试

- **后端**
  - 迁移 `0043` 覆盖（FTS/迁移测试套路）：升级后 `kind` 列存在、CHECK 生效、存量回填 `companion`。
  - `GET /categories?kind=` 过滤正确性。
  - `ai_illustrate` 节点：mock `run_ai_format`（或 mock litellm），验证候选栏目 = 主推 +（开关控制的）陪衬、article_ids 透传、单篇失败聚合为 partial_failed。
  - （若做）`from-url` 端点：成功保存 + 非法 url / 抓取失败的错误码。
- **前端**：`pnpm --filter @geo/web typecheck` + `build` 通过。

---

## 风险 / 注意

1. **节点并发锁**：每篇文章独立置 `ai_checking` 锁，`run_ai_format` 内部有锁指纹复核，按 article 隔离 → 安全。
2. **图片保存抓跨源图**：同源图片 `fetch` 可取；外链图靠可选 `from-url` 兜底。
3. **scheme 自动配图不动**：现有 `scheme_executor` 对全部 bucket 自动配图的行为本次保留，未来可考虑与节点统一（超出本次范围）。
4. **demo.pen 全程不碰**。
5. **CORS**：图片库 `GET /categories` 已需登录；新增 `?kind=` 不改鉴权。`/api/stock-images/*` 保持公开（前端嵌入依赖）。
