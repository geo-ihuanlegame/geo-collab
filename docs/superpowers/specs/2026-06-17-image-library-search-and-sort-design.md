# 设计：图片库全库搜索 + 栏目排序

> 状态：已确认，进入实现。范围 = 搜索框接全库跨栏目搜索（下拉浮层 + 跳转高亮）+ 把死的「筛选」按钮换成栏目排序下拉。两者逻辑独立。

## 背景与动机

图片库（[`web/src/features/image-library/ImageLibraryWorkspace.tsx`](../../../web/src/features/image-library/ImageLibraryWorkspace.tsx)）顶栏有一个搜索框和一个「筛选」按钮，**两者都是纯 UI 占位、没有任何逻辑**：

- 搜索框（`ImageLibraryWorkspace.tsx:252-260`）只把输入写进 `searchInput` state，这个 state 在整个组件里**再没被读取过** —— 不过滤、不发请求。组件自己在 `:48-49` 留了注释「本次仅前端占位（可输入但不过滤），过滤逻辑待后端做全库搜索时再接」。
- 「筛选」按钮（`ImageLibraryWorkspace.tsx:262-264`）**连 `onClick` 都没有**，注释写「菜单 / 过滤维度待后端确定后再接」。

后端也没有搜索能力：[`GET /api/image-library/images`](../../../server/app/modules/image_library/router.py)（`router.py:365-378`）只支持 `category_id`（按栏目）和 `tag`（**精确**标签匹配 `tags.contains([tag])`）两个过滤参数，没有跨栏目、多字段、模糊的搜索。

**预期结果**：

1. **搜索框**变成全库跨栏目搜索：关键词模糊匹配文件名 / 描述 / 标签 / 栏目名（任一命中即出），结果显示在搜索框下方的下拉浮层；点某条 → 切到该图所在 tab + 栏目 → 主网格滚动到该图并短暂高亮。
2. **「筛选」按钮**替换为**栏目排序下拉**，给左侧栏目列表排序，4 个选项：栏目名 A→Z（中文按拼音）/ Z→A / 栏目创建时间（现状默认）/ 栏目内最新图片时间（哪个栏目最近加过图就靠前）。

排序与搜索是两条互不相干的逻辑。

## 设计

### 1. 后端 · 新增搜索端点

新增 `GET /api/image-library/search`（挂在已登录的 `router` 上，沿用 `Depends(get_current_user)`），**不复用** `list_images`：搜索结果浮层要展示并跳转到所属栏目，需要返回栏目名 / kind，而 `StockImageRead` 不含这些；且「全库多字段模糊 OR」与 `list_images` 的「按栏目 / 精确标签过滤」语义不同，分开更干净。

- **入参**：
  - `q: str`（必填）—— `strip()` 后为空则直接返回 `[]`（不查库）。
  - `limit: int = 50` —— clamp 到 `[1, 200]`。
- **查询**：`StockImage` JOIN `StockCategory`（每张图必属一个栏目，inner join 即可），`WHERE` 对下列 4 项 **OR** 模糊匹配：
  - `StockImage.filename LIKE %q%`
  - `StockImage.description LIKE %q%`（`description` 可空，LIKE 对 NULL 不命中，符合预期）
  - `StockCategory.name LIKE %q%`
  - 标签：`func.json_search(StockImage.tags, 'all', f'%{q}%').isnot(None)` —— 模糊匹配 JSON 数组里的任一标签项。
  - **LIKE 转义**：先对 `q` 里的 `\` `%` `_` 转义再拼 `%...%`，并在 SQLAlchemy `.like(pattern, escape='\\')` 指定转义符，避免用户输入的通配符被当成模式。同一个转义后的串也用于 `json_search`。
  - 大小写：MySQL 默认 collation 不区分大小写，`LIKE` 与 `JSON_SEARCH` 均按现有 collation 行为，不额外处理。
- **排序 / 截断**：`ORDER BY StockImage.created_at DESC`，`LIMIT limit`。
- **返回** `list[SearchResultRead]`，每项：
  - `id: int`、`filename: str`、`url: str`（`/api/stock-images/{id}/file`，复用现有 `_to_image_read` 的 url 规则）
  - `category_id: int`、`category_name: str`、`kind: str`（`main` / `companion`，跳转时用来切 tab）

### 2. 后端 · 栏目列表带「最新图片时间」

为支持「栏目内最新图片时间」排序（前端本地 sort，见 §4），`list_categories` 的返回项加一个字段：

- `CategoryRead` 增 `latest_image_at: datetime | None`（栏目下没有图片时为 `None`）。
- `list_categories`（`router.py:219-229`）改为 `StockCategory` **LEFT JOIN** 一个 `StockImage` 按 `category_id` 分组取 `MAX(created_at)` 的子查询，把该值映射到 `latest_image_at`。其余行为（`kind` 过滤、默认 `ORDER BY created_at DESC`）不变 —— 后端返回顺序不重要，前端会按所选排序重排。

> 选型理由：把「最新图片时间」放进栏目列表返回，四种排序就全在前端本地 sort，切换排序零额外请求、即时生效；中文拼音排序（A→Z）也只有前端 `localeCompare` 做得方便。若改成把排序传给后端 `ORDER BY`，则拼音排序难做、且每次换排序都要重新请求。

### 3. 前端 · 搜索浮层

新增 API 客户端 `searchImages(q, limit?)`（[`web/src/api/image-library.ts`](../../../web/src/api/image-library.ts)）→ `GET /api/image-library/search`，返回 `ImageSearchResult[]`。

`ImageLibraryWorkspace` 改动：

- `searchInput` 加 **300ms 防抖**；`trim()` 后非空才调 `searchImages`，把结果填进 `searchResults` 并展开浮层。空输入立即清空结果、收起浮层。
- 浮层：绝对定位在搜索框正下方，限定最大高度可滚动；包含三态 —— **loading**（请求中）/ **空结果**（「无匹配」）/ **错误**（toast 或浮层内提示）。
- 每条结果 = 缩略图（`<img src={item.url}>`）+ 文件名 + 所属栏目角标（chip 显示 `category_name`）。
- 关闭浮层：点击浮层外部、按 `ESC`、清空输入框 —— 三者都收起。
- **点击某条结果（跨 tab / 跨栏目跳转 + 高亮）**：

  这里有个时序坑必须处理 —— 现有 `kindTab` 的 effect（`ImageLibraryWorkspace.tsx:72-80`）在切 tab 后会把 `selectedCategoryId` **无条件重置成 `cats[0].id`**，会覆盖我们想选中的目标栏目；而 `selectedCategoryId` 的 effect（`:82-93`）切栏目后才异步拉图片，图片到位前无法滚动定位。设计如下：

  1. 点击时记一个 `pendingJump = { kind, categoryId, imageId }`，同时收起浮层、清空 `searchInput`。
  2. 若 `pendingJump.kind !== kindTab`，`setKindTab(kind)`；否则不动 tab。
  3. **栏目加载完成后**（`kindTab` effect 的 `.then(cats => ...)` 里）：若存在 `pendingJump` 且 `cats` 含 `pendingJump.categoryId`，选中它而非 `cats[0]`；否则维持原「选第一个」逻辑。
  4. **图片加载完成后**（`selectedCategoryId` effect 的 `.then` 里，或一个依赖 `images` + `pendingJump` 的 effect）：若 `images` 含 `pendingJump.imageId`，`scrollIntoView` 到对应卡片并加一个短暂高亮 class（约 1.5s 后移除），随后清空 `pendingJump`。
  5. 卡片需要可定位：给图片卡片加 `id={`il-card-${img.id}`}`（或 ref map）供 `scrollIntoView` 用，高亮通过临时 class 实现。

### 4. 前端 · 栏目排序下拉

把现在的「筛选」按钮（`:262-264`）替换为排序下拉（原生 `<select>` 或自绘小菜单，沿用 `secondaryButton` 视觉；图标可由 `SlidersHorizontal` 换成更贴切的 `ArrowUpDown`）。

- state `categorySort: 'created' | 'name_asc' | 'name_desc' | 'latest_image'`，默认 `'created'`（对齐现状）。
- 渲染左侧 sidebar 前对 `categories` 本地 sort 得到 `sortedCategories`：
  - `name_asc`：`a.name.localeCompare(b.name, 'zh-Hans-CN')`（中文近似拼音序）
  - `name_desc`：上面取反
  - `created`：`created_at` 倒序（新→旧）
  - `latest_image`：`latest_image_at` 倒序，`null`（无图栏目）排最后
- 切换排序纯前端、即时生效，不发请求。切 main/companion tab 不影响所选排序（`categorySort` 不随 tab 重置）。

### 5. 前端类型

[`web/src/types.ts`](../../../web/src/types.ts)：

- `StockCategory`（`:418-426`）加 `latest_image_at: string | null`。
- 新增 `ImageSearchResult`：`{ id: number; filename: string; url: string; category_id: number; category_name: string; kind: "main" | "companion" }`。

## 要改的文件

**后端**

- `server/app/modules/image_library/router.py`
  - 新增 `SearchResultRead` 出参模型 + `GET /search` 路由（多字段 OR 模糊 + LIKE 转义 + json_search + limit clamp）。
  - `CategoryRead` 加 `latest_image_at`；`_to_category_read` 与 `list_categories`（LEFT JOIN max(image.created_at) 子查询）相应改。
- （仅当排序/搜索逻辑下沉到 service 时才动 `service.py`；本设计查询直接写在 router，与现有 `list_images` 风格一致，不强制拆 service。）

**前端**

- `web/src/api/image-library.ts` — 新增 `searchImages(q, limit?)`。
- `web/src/types.ts` — `StockCategory.latest_image_at` + `ImageSearchResult`。
- `web/src/features/image-library/ImageLibraryWorkspace.tsx` — 搜索防抖 + 浮层 + 跳转高亮（含跨 tab 时序处理）；「筛选」按钮 → 排序下拉 + 本地 sort。
- 样式：浮层 / 高亮 / 排序下拉的 CSS（沿用现有图片库样式文件，跟随 `imageLibrary*` 类命名）。

## 测试

后端（MySQL，pytest）：

- 新增 `server/tests/test_image_library_search.py`：
  - 多字段命中（filename / description / 标签 / 栏目名各一例）。
  - 跨栏目：建两个栏目各放命中图，一次 `q` 都能搜到。
  - 标签模糊：`json_search` 命中数组里的某个标签子串。
  - `limit` clamp（>200 截到 200、缺省 50）、空 `q` 返回 `[]`。
  - LIKE 转义：含 `%` / `_` 的 `q` 按字面匹配，不当通配符。
- `latest_image_at`：可并入现有 `server/tests/test_image_library_folder_ops.py` 或新增用例 —— 有图栏目返回最新图 `created_at`、无图栏目返回 `None`、排序符合预期。

前端无单测框架，门禁 = `pnpm --filter @geo/web typecheck` + `build`。

## 不做（YAGNI）

- 不做按 kind / 栏目 / 尺寸 / 有无标注的条件**过滤**（用户已明确「筛选」就是排序下拉，不是条件过滤）。
- 搜索浮层不做分页 / 无限滚动，只取前 `limit` 条（默认 50）；超出靠收窄关键词。
- 不引入全文索引（FULLTEXT）；图片库量级用 `LIKE` + `JSON_SEARCH` 足够，避免为此加迁移。
- 排序不持久化到用户偏好 / localStorage，刷新回默认。
