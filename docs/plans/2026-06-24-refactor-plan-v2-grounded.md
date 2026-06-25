# Geo 协作平台 — 业务重构方案 v2（代码事实驱动 · 从零重做）

> 状态：规划稿 · 日期：2026-06-24
> 方法：六路并行代码探查（依赖图 / 跨模块事务 / 运行时并发 / 前端路由 / 表所有权 / Git&部署）交叉验证后综合，**所有结论尽量带 file:line**。
> 与同日旧稿 `2026-06-24-go-microservices-refactor-plan.md` 的关系：本稿不覆盖旧稿；旧稿的三条决策基线（发文保留 Python / 先共享库软隔离 / 绞杀者迁移）经本次代码核实**全部成立**，但本稿**修正了旧稿对服务边界的若干误判**，并补齐旧稿完全缺失的 **Git 分支策略**与**团队规模现实性评估**。

---

## 0. 一句话结论（先看这个）

代码事实支持「业务模块独立部署」，但**不支持把单体直接切成 12 个 Go 微服务**——以 2~3 人活跃团队的规模，那是用分布式复杂度淹没一个还在高频迭代的产品。**推荐：先做「模块化单体 + 按需抽离」的粗粒度拆分（4~5 个可独立部署单元），而非细粒度微服务集群。** 前端独立 URL 路由优先用 react-router 打通、ant-design-pro 按需引入。Git 用 monorepo + CODEOWNERS 目录所有权 + trunk-based，这是避免多人冲突的真正杠杆。

---

## 1. 现状事实基线（六路探查交叉结论）

### 1.1 模块与依赖（探查①）
- `server/app/modules/` 下 **17 个目录**，其中 `flows`/`plugins`/`skills` 是休眠/雏形模块；活跃业务域约 12 个。
- 模块 import 图是**无环 DAG**（靠若干 lazy import 打破潜在环）。入度排名：`system`(11) > `articles`(9) > `tasks`(8) > `audit`(7)。
- `mcp_catalog` 是高扇出**只读聚合器**（依赖 6 模块），天然对应未来的 BFF/Gateway 聚合层。

### 1.2 最硬的三处耦合（探查①③⑤交叉）
1. **`articles` ↔ `tasks` 双向耦合**（旧稿误当 articles 是单纯枢纽）：
   - `articles/service.py:41` import `PublishRecord/PublishTask`（articles 查「已分发/在途」状态要读发布记录）。
   - `tasks/service.py:21` import `Article/ArticleGroup`（建发布任务要读文章）。
   - 双向，靠 lazy import 才没在 import 层成环。拆服务后**两个方向都要跨服务调用**。
2. **`accounts` ↔ `tasks` 物理不可分**：`accounts.RecordBrowserSession.record_id` → `tasks.PublishRecord.id` 外键；账号登录会话状态机与发布记录状态机强绑定。→ **accounts 必须和 tasks 同服务**（印证旧稿「accounts 并入 publishing」）。
3. **`Asset` 表被 3 个模块跨模块外键引用**（旧稿完全漏掉）：`articles` 拥有 `Asset`，但 `accounts.Account.avatar_asset_id`、`tasks.TaskLog.screenshot_asset_id` 都直接 FK 它。拆库时这是隐藏地雷。

### 1.3 跨模块事务与状态机（探查②④）
- **T1 `create_task`**（`tasks/service.py:115-170`，门禁 `:666-681` `_validate_articles_approved`）：单事务里读 `articles.review_status` 做审核门禁，再笛卡尔积建 `PublishTask×PublishRecord`。拆开 = TOCTTOU 竞态。
- **`mark_pending_and_group` / `mark_pending_and_append_daily` 是三方汇聚点**：
  - `pipelines/executor.py:217`、`ai_generation/scheme_executor.py:377`、`pipelines/nodes/daily_group_stream.py` 都调它，
  - 都写 `articles.review_status='pending'` + 建/追加 `ArticleGroup`/`ArticleGroupItem`（含唯一约束乐观锁 + rollback 重试）。
  - → 这是「`review_status` 必须收归 content 单一写者、暴露成一个原子 API」的铁证。
- **四套状态机各有多写者**：① `Article` 三份正文 `content_json/html/plain_text`（4 处写：create/update/article_writer/ai_format）；② `review_status`（7 处写，自动/人工/Loop 三路竞争）；③ `PublishRecord.status`（worker 租约 vs 启动无租约复位的竞态）；④ `PublishTask.status`（子记录聚合的物化视图，`service.py:435-483`）。

### 1.4 运行时并发：阻碍水平扩展的具体代码点（探查③）
| 代码点 | 位置 | 单进程假设 | 多实例后失效 |
|---|---|---|---|
| 3 个 `ObservableGate` 信号量 | `scheme_executor.py:44` / `pipelines/executor.py:27` / `tasks/executor.py:86` | 进程内并发上限 | 上限 ×N |
| `_DISPATCH_POOL` + `ThreadPoolExecutor(max_workers=4)` | `scheme_executor.py:51,290` | 进程内线程池 | 并发 ×N |
| `daily_group` `itertools.count()+Lock` | `daily_group_stream.py:38` | 进程内自增 | sort_order 号段混乱 |
| 账号登录子线程 + `BrowserProfileLock` 续租 | `worker/executor.py:324,194-202` | 单 worker 独占 | 多 worker 互相覆盖租约（**单 worker 硬约束真因**） |
| 启动**无租约全量复位** running→failed | `pipelines/recovery.py:20-38`、`scheme_executor.py:305` | 进程刚起=无其他 writer | 第二实例启动误杀第一实例正在跑的 run（**「别跑多实例 web」真因**） |

> ⚠️ 探查③对 `sync_scheduler`/`taptap_health`/`resource_metrics` 三处是**推断未逐行验证**，落地前需复核。

### 1.5 数据模型（探查⑤）
- **42 张表 / 12 活跃模块**。`User` 被 15+ 表引用，`Article` 被 8 张引用。
- **隐式 JSON 跨模块引用**（无 FK、删除留孤儿，拆库最易漏）：`GenerationSchemeRun.article_ids`、`GenerationSchemeLine.allowed_prompt_template_ids`、`PipelineRun.node_results`/`snapshot`（内嵌 article_ids/template_ids/group_id）。
- **加密列仅在 `accounts`**：`Account.api_credentials`/`api_token_cache`（`EncryptedJSON`，`GEO_SECRET_KEY`）→ 密钥暴露面可收敛到发文域内部。

### 1.6 前端（探查④）
- **完全无 URL 路由**：`App.tsx` 用 `activeNav` state + `visitedTabs` + `display:none` 缓存，URL 永远不变。13 个 NavKey / 11 feature / ~113 个 API 函数。
- **完全未用 antd**：纯 Lucide + vanilla CSS 自制 Modal/Toast/Pagination。
- 不可平替组件：Tiptap（中）、PipelineEditor（高，含 QuestionTypePicker 复杂态）、noVNC（极低，只是 `window.open`）、SSE 进度流（低）、分块上传（低）。
- 路由化路障：`visitedTabs` 的「切走不卸载、保状态」语义、`ContentWorkspace` 脏检查、`TasksWorkspace` 的 SSE 生命周期、模态框多步 state。

### 1.7 Git & 部署（探查⑥）
- **已严格用 Conventional Commits + PR 号**（`fix(tasks): … (#143)`），2~3 个活跃贡献者（44lf 为主，Glen、trkjoy）。
- 已有 `docs/decomposition-plan-a` 分支 + 第二远程 `hlgit`——团队已在试水拆分。
- **仓库内无 CODEOWNERS / PR 模板**；分支保护在 GitHub 平台侧（"geo" 规则要求 backend+frontend 两个必需检查）。
- CI：GitHub（主，4 分片并行 pytest）+ GitLab（备，单 runner）。硬门禁：后端 ruff/mypy/pytest、前端 typecheck+build；ESLint `continue-on-error`，安全审计非阻塞；用 `dorny/paths-filter` 跳过纯文档。
- 部署：docker-compose，**单镜像跑 web+worker 两种进程**（同 `Dockerfile`，启动命令不同）+ nginx + mysql + minio + dailyhot-api(Node)。`app_data` 卷三方共享。
- **混合 monorepo**：前端 pnpm workspace(`@geo/web`) + 后端单 Python 包 + vendored Node 子服务。
- **隐藏痛点**：单一 alembic 迁移链——多分支并行各加迁移会频繁撞 head（拆分后会更糟）。

---

## 2. 战略判断：该上几个服务、用不用 Go？

这是旧稿没认真回答、却最该先回答的问题。基于上面的事实：

### 2.1 团队规模 vs 微服务税
2~3 人活跃团队 + 一个仍在高频改的产品（最近提交全是 feat/fix）。12 个 Go 微服务意味着同时引入：NATS、Redis 分布式锁、Saga/Outbox、服务发现、跨服务 trace、N 条 CI、N 个镜像、Go 重写 12 个域 + Tiptap 转换器移植——**这套分布式税会吃掉本就紧张的产能，且每个跨服务事务都新增失败模式**。结论：细粒度微服务对当前规模是**净负债**。

### 2.2 推荐：粗粒度「可独立部署单元」+ 模块化单体起步
把目标从「12 个微服务」降为 **4~5 个可独立部署单元**，沿真实耦合的「断裂面」切，而不是沿模块目录切：

| 部署单元 | 含模块 | 语言 | 为什么是一个单元 |
|---|---|---|---|
| **content-core**（内容核心） | articles + ai_generation + pipelines/flows + prompt_templates + image_library + auto_review + audit | 起步 **保留 Python**，按需热点改写 | `mark_pending_and_group` 三方汇聚、三份正文同步、review_status 单写者、JSON 隐式引用全在这圈内——**强行拆开就是制造分布式事务**。让它们同库同事务，是最省心的边界。 |
| **publishing**（发文域） | tasks + accounts + drivers + browser + worker | **保留 Python**（Playwright/Xvfb/noVNC 绑定 Linux） | accounts↔tasks 不可分；加密凭据收敛于此；单 worker 约束天然隔离。 |
| **identity**（基础身份） | system(User/Platform/WorkerHeartbeat) | 可 Go 可 Python | 全员依赖，先稳定契约；JWT 内嵌用户上下文减少回查。 |
| **hotlist**（已是独立子服务） | hot_lists 代理 + dailyhot-api(Node) | Node + 薄代理 | 现状已独立，零成本。 |
| **gateway/BFF**（可选） | api-gateway + mcp_catalog 聚合 | Go 或 Nginx+薄层 | 统一鉴权/路由/前端聚合，做绞杀者接缝。 |

> **Go 的定位**：不是「全量重写」，而是**按性能/团队边界按需抽离**。`identity`、`hotlist`、`performance` 这类近纯 CRUD/只读、低耦合的域，是 Go(go-zero) 练手与受益的首选；`content-core`/`publishing` 的复杂事务与 Tiptap 转换器**短期不值得 Go 重写**。把「Go 微服务」当成**终态可选项**而非起步要求。

### 2.3 这样切如何满足你的诉求①「生文/发文独立部署」
- **publishing** 一开始就独立部署、独立发版、独立扩缩——发文组完全自治（你的核心诉求）。
- **content-core** 内部「生文(ai_generation)」与「编排(pipelines)」逻辑独立、可独立迭代；当生文确需独立扩缩时，再沿 `mark_pending_and_group` 这一个 API 边界把它抽成独立单元（届时它通过 content-core 的原子 API 成组，不碰 articles 表）。**先模块化、保留抽离接缝，按真实需求再抽**。

---

## 3. 服务边界与表所有权（共享库软隔离阶段铁律）

### 3.1 表所有权
- 每张表**有且仅有一个 owner 单元**可写；跨单元只读走 owner 的 API/事件，**禁止跨单元 JOIN/UPDATE**。
- 共享库阶段用**独立 DB user + GRANT** 强制（每个单元一个库账号，只授权自己的表写权限）。这是「将来物理拆库零痛苦」的前提。
- 归属：content-core 拥有 articles/ai_generation/pipelines/prompt_templates/image_library/auto_review/audit 全部表；publishing 拥有 tasks+accounts 全部表；identity 拥有 system 三张表。

### 3.2 三个必须先解决的跨单元数据问题
1. **`Asset` 跨 3 模块 FK**：`Asset` 归 content-core。`accounts.avatar_asset_id`、`tasks.screenshot_asset_id` 改为**按 id 弱引用 + 应用层校验**（去掉物理 FK），或把发布截图/头像这类发文域自产资产**下沉到 publishing 自己的资产表**（更干净）。落地前做一次 `Asset` 引用盘点。
2. **`articles`「在途状态」反向依赖 `PublishRecord`**：不要让 content-core 反查 publishing 库。改为 **publishing 发 `publishing.record.status_changed` 事件 → content-core 维护一份本地「in-flight」只读投影**（CQRS read model），`approved_content_source`/`article_group_source` 的「已分发」判定读投影。
3. **隐式 JSON 引用**：迁移期对 `article_ids`/`allowed_prompt_template_ids`/`node_results` 做一次孤儿扫描脚本；长期把高价值的（如 scheme→article）抽成显式关联表。

---

## 4. 三大耦合难点的解法

### T1 `create_task` 审核门禁（publishing 读 content）
- **方案**：编排式 Saga，publishing 发起：① 调 content-core 批量 RPC 校验 `article_ids` 均 `approved`；② publishing 本地校验账号（同单元，无跨服务）；③ 本地事务建 `PublishTask×Record`。
- 因 accounts 与 tasks 同单元，T1 从「4 模块跨表」降为「**1 次跨单元只读校验**」。TOCTTOU 用「发布前 publishing 二次校验 + content 侧 approve 带版本号」兜底。

### `mark_pending_and_group` 收口（消灭 review_status 三路竞争）
- **单一写者**：`review_status` 与三份正文写入**收归 content-core 独占**，把 `mark_pending_and_group`/`mark_pending_and_append_daily` 整体留在 content-core 内，对外只暴露一个原子 API：`POST /internal/articles:mark-pending-and-group`。
- pipelines/ai_generation/auto_review 的 pending 写入全改调这一个 API → **状态机只有一个写者**，并发由 content-core 内部串行/CAS + 现有唯一约束乐观锁保证。
- 三份正文同步（`markdown_to_tiptap`/`_derive_html_and_text`）**永不跨单元拆**，保持原子。这也顺带回避了「Tiptap 转换器 Go 移植」风险——它留在 Python content-core 内即可。

### 状态机 + 并发原语分布式化（仅当某单元真要多实例时才做）
- **content-core / 生文 / 编排要多实例时**，按探查③逐点改：3 个 `ObservableGate`→Redis 分布式信号量；`itertools.count`→DB 序列/`SELECT … FOR UPDATE`；启动无租约全量复位→**心跳/租约判定**（`last_heartbeat_at` 超时才置 failed）→ 从此可多实例。
- **publishing worker 维持单实例**（profile 锁 + 无租约复位约束未变）；高可用走主备，不水平扩展。长期若要多 worker，先给 `BrowserProfileLock` 续租加 `with_for_update()` 行锁 + 租约。
- **关键纪律**：上面这些是「想多实例才付的成本」。模块化单体阶段单进程跑，**这些原语原样可用、零改动**——这正是不急着拆微服务的红利。

---

## 5. 前端：独立 URL 路由 + ant-design-pro 评估（诉求②）

### 5.1 分两步走，别一步到位上 UmiJS
- **第一步（2~3 周，强烈推荐先做）**：引入 **react-router v6**，保留现有 React19+Vite+Tiptap+Lucide 全部组件。把 13 个 NavKey 映射成独立路由：
  - 一级：`/agents` `/ai` `/content` `/tasks` `/image-library` `/media` `/system` `/hot-lists` `/mcp` `/admin/users` `/admin/audit-logs` `/admin/ai-models`。
  - 二级子标签用 path segment：`/content/pending` `/content/approved`、`/prompts/generation` 等（替代现在的 `contentReviewTab`/`promptsScope` state）。
  - 把 `visitedTabs` 的「保状态」语义迁移：列表筛选/搜索词进 URL query（可分享、可定位），编辑器脏检查改 router `beforeUnload`/`blocker` guard，SSE 在路由 `useEffect` cleanup 里 `es.close()`。
  - **这一步就完全满足你诉求②**：每个菜单页有唯一 URL，可直达、可分享、可加书签。
- **第二步（按需，非必须）**：评估是否上 ant-design-pro。

### 5.2 ant-design-pro 评估结论
- **收益**：ProLayout 侧边栏/面包屑、ProTable/ProForm 覆盖约 70% 的列表/表单类页面、`access` 权限模型直接映射 `admin`/`operator`、现成中后台皮肤。
- **代价**：UmiJS Max 是重型框架，需重学构建/约定式路由/数据流；Tiptap、PipelineEditor、noVNC、SSE 这 4 个自定义组件仍得保留（Pro 只能包外层）；从 vanilla CSS 迁 antd 设计语言有视觉返工；全量迁移估 5~8 周。
- **建议**：**不要为了「换框架」而换**。若团队主要诉求是「列表/表单更规整 + admin 模板」，可只引入 `antd` 组件库（按需），路由仍用 react-router，**不必迁 UmiJS Max 全家桶**。只有当你确需「约定式路由 + 权限插件 + 数据流 + 中后台模板」整套时，才值得上 UmiJS。可在第一步上线后用真实需求复核。

---

## 6. 团队 Git 分支策略（旧稿最大缺口 · 诉求③）

目标：2~3 人 + 未来按单元分工，**多人改代码尽量不撞车、各单元可独立发版**。

### 6.1 仓库形态：继续 monorepo（强烈推荐）
- 对 2~3 人团队，**多 repo 是净负担**（跨 repo PR、版本对齐、submodule 地狱）。保持现有混合 monorepo，按单元建顶层目录：
  ```
  services/content-core/   (现 server/app 大部分 + worker 的生文/编排部分)
  services/publishing/      (tasks+accounts+drivers+browser+worker)
  services/identity/        (system)
  services/hotlist/         (现 services/dailyhot-api)
  services/gateway/         (新)
  contracts/                (跨单元 API/事件契约，独立版本化)
  web/                      (前端，现状)
  ```
  > 过渡期可不动物理目录、先用「逻辑所有权 + CODEOWNERS」标边界，物理搬迁随抽离逐步做。

### 6.2 避免冲突的真正杠杆：CODEOWNERS + 目录所有权
- **当前仓库没有 CODEOWNERS——这是头号要补的东西。** 新建 `.github/CODEOWNERS`：
  ```
  /services/content-core/   @content-owner
  /services/publishing/     @publishing-owner
  /web/                     @frontend-owner
  /contracts/               @content-owner @publishing-owner   # 契约改动需双方 review
  /services/**/migrations/  @db-owner
  ```
- 原理：**人按目录分工 → 改的是不同文件 → 天然不撞车**。冲突只剩在「共享代码」与「契约」，而那两处恰恰**应该**强制多人 review。

### 6.3 分支模型：trunk-based + 短命特性分支（沿用现状强化）
- 保持 `main` 受保护 + PR + 必需检查（现状已是）。继续 Conventional Commits，**scope 对齐单元名**：`feat(content-core): …`、`fix(publishing): …`。
- 特性分支**短命**（≤2~3 天）、勤合并、`rebase` 而非 merge 保持线性；避免长命大分支（现 `feat/prompt-template-operator-edit` 已领先 9 commits，是要提醒的反例）。
- 不上 git-flow（release/develop 双长命分支对 CD + 小团队是过度设计）。

### 6.4 数据库迁移：拆链，消灭 alembic head 撞车
- **当前单一 alembic 链是多分支并行的隐藏冲突源**（两人各加迁移→撞 head→反复 rebase）。
- 拆分后**每个单元独立迁移目录/独立链**：content-core 继续 alembic；Go 单元用 golang-migrate/Atlas。共享库阶段约定「各单元只迁自己的表」，互不写对方表 = 互不撞 head。

### 6.5 契约协作：让跨单元改动有秩序
- `contracts/` 独立目录、独立版本号；同步契约用 OpenAPI（或 go-zero `.api`），异步用 CloudEvents + JSON Schema。
- 契约**只增不改**，破坏性变更走新版本字段/新 subject；CODEOWNERS 强制契约改动双 owner review；CI 加「契约变更触发下游消费方兼容性检查」。

### 6.6 CI 拆分：路径过滤 + 每单元独立流水线
- 已有 `dorny/paths-filter` 基础，扩展成**按单元路径触发**：改 `services/publishing/**` 只跑 publishing 的 lint/test，改 `web/**` 只跑前端——**互不阻塞、各自可独立发版**。
- 每单元独立构建镜像（现已是单 `Dockerfile` 双进程，拆分后每单元一个 Dockerfile）。

---

## 7. 渐进迁移路线（绞杀者 · 每步可独立上线可回滚）

> 总原则：Gateway 做接缝，逐前缀切流；任何一步不中断业务。**不追求「切完所有微服务」，追求「每步交付独立价值」。**

- **Phase 0｜地基（不动业务）**：补 `.github/CODEOWNERS`；拆 alembic 迁移纪律；前端引入 react-router 打通独立 URL（**最快见效、直接交付诉求②**）；（可选）立 Gateway 反代现单体。
- **Phase 1｜发文域独立（直接交付诉求①核心）**：把 `tasks+accounts+worker+drivers+browser` 整体打包成独立部署单元 `publishing`（仍 Python，不重写）。改造点仅三处：建任务的文章校验改调 content API；distribute 改消费事件；记录完成发事件。→ 发文组从此独立发版、独立扩缩。
- **Phase 2｜content-core 收口状态机**：把 `mark_pending_and_group`/三份正文/`review_status` 收成 content-core 独占 + 原子 API；建 in-flight 只读投影消费 publishing 事件。此时单体已被「掰成两个可独立部署单元 + 清晰契约」。
- **Phase 3｜按需抽离/Go 化**：低耦合域（identity / hotlist / performance）按团队/性能需要切 Go(go-zero) 练手；生文(ai_generation)确需独立扩缩时沿成组 API 抽离 + 并发原语换 Redis/租约。
- **Phase 4｜按需多实例 & 拆库**：哪个单元真遇到扩展瓶颈，才付「分布式原语 + 物理拆库」的成本。前端按需评估 ant-design-pro。

| 里程碑 | 内容 | 可见价值 |
|---|---|---|
| M0 | CODEOWNERS + 迁移拆链 + 前端独立路由 | 诉求②达成、多人协作不撞车 |
| M1 | publishing 独立部署 | 诉求①核心达成、发文组自治 |
| M2 | content-core 状态机收口 + 事件投影 | 内容/发文清晰解耦 |
| M3 | 低耦合域按需 Go 化 | 团队 Go 练手、按需独立 |
| M4 | 按需多实例 / 拆库 / ant-design-pro | 仅在真有瓶颈时付费 |

---

## 8. 风险与权衡

| 风险 | 说明 | 缓解 |
|---|---|---|
| **过度拆分吃产能** | 2~3 人上 12 微服务 = 分布式税压垮迭代 | 本稿核心：粗粒度 4~5 单元 + 模块化单体起步 + 按需抽离 |
| **Asset 跨 3 模块 FK** | 拆库时 accounts/tasks FK 到别库 | 先盘点引用；发文域自产资产下沉，其余按 id 弱引用 + 应用校验 |
| **articles↔tasks 双向** | 两方向都要跨单元 | 一向走 publish-payload 拉取 API，反向走事件 + in-flight 投影 |
| **隐式 JSON 引用孤儿** | 删除留孤儿、无约束 | 迁移期孤儿扫描脚本；高价值引用抽显式关联表 |
| **并发原语单进程假设** | 多实例后全失效（探查③清单） | 不急着多实例；真要多实例时按清单逐点换 Redis/租约 |
| **前端状态保活语义丢失** | 路由卸载 vs visitedTabs 保状态 | 筛选词进 URL query；编辑器 router guard；SSE cleanup |
| **探查③三处未验证** | sync_scheduler/taptap_health/resource_metrics 推断 | 落地前逐行复核 |

---

## 9. 落地下一步
1. 评审本稿，特别确认 **§2「粗粒度而非 12 微服务」** 与旧稿细粒度方案的取舍。
2. **立即可做、零风险**：补 `.github/CODEOWNERS` + alembic 迁移拆链纪律（§6.2/§6.4）。
3. 前端起 react-router 独立路由分支（§5.1 第一步），直接交付诉求②。
4. 起草 `contracts/`：content-core ↔ publishing 的 publish-payload API + `publishing.record.status_changed` 事件首版（§3.2/§4）。
5. 做一次 `Asset` 跨模块引用盘点 + 隐式 JSON 引用孤儿扫描脚本（§3.2）。
