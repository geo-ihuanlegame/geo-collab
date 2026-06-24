# Geo 协作平台 — Go 微服务重构方案

> 状态:规划稿(brainstorm 产出) · 日期:2026-06-24
> 决策基线(已与负责人确认):
> 1. **发文/浏览器自动化保留 Python**,封装成独立可部署的 `publishing-service`,与 Go 服务通过契约/事件协作。
> 2. **数据库先共享一个 MySQL,按表所有权软隔离**,验证边界后再逐步物理拆库。
> 3. **绞杀者(Strangler)渐进迁移**,Go 服务逐个上线接管流量,Python 单体逐步退役,业务不中断。

---

## 1. 背景与目标

当前系统是一个 FastAPI + SQLAlchemy 的 Python 单体(`server/app/modules/` 下 12+ 领域模块),前端 React 19 + Vite + Tiptap。诉求:

1. **业务模块可独立部署**(生文、发文等各自独立),方便维护与扩缩容。
2. **团队各自负责独立模块**,模块之间通过**契约 + 异步事件**协作,可独立部署、独立发版。
3. **统一管理后台**,前端用 **ant-design-pro** 重构。
4. 后端倾向 **Go 微服务**。

本方案给出:服务边界划分、团队归属、Go 技术栈选型、契约与事件设计、三大难点事务解法、前端重构方案、以及一条可回滚的渐进迁移路线。

---

## 2. 现状评估摘要(代码探查结论)

### 2.1 依赖结构
- 模块间 import 依赖是**无环 DAG**:`articles` / `tasks` 是中心枢纽;`ai_generation` 是耦合最重的消费方(同时依赖 articles / prompt_templates / image_library / ai_models / ai_format 6 个模块)。
- `system`(User/Platform)被所有模块依赖,是事实上的 foundational 服务。

### 2.2 三个最难切的跨模块事务
| 编号 | 事务 | 跨越模块 | 本质 |
|---|---|---|---|
| **T1** | `create_task()` | tasks + articles + accounts + system | 一个 session 内校验文章已审核 + 账号有效,再笛卡尔积建 PublishTask×Record |
| **T2** | `distribute` 节点 → `create_task` | pipelines + articles + tasks + accounts | 编排节点在后台线程内同步建发布任务 |
| **T3/T6** | pipeline 线性执行 | pipelines + articles + ai_generation + tasks + image_library | 单后台线程顺序跑 ai_generate→to_review(改 review_status)→distribute,context dict 传递 |

### 2.3 三个最危险的共享状态
- **`Article` 三份正文**(`content_json` / `content_html` / `plain_text`)必须始终同步,任一漂移即发布失败或内容错乱。
- **`Article.review_status`** 状态机:被 articles(人审)、pipelines(to_review)、ai_generation、auto_review 四路写入,自动/人工/Loop 三路竞争。
- **`PublishRecord.status`** 发布状态机:5 路分叉 + 乐观锁(`lease_until`)+ `commit_uncertain` 未决保护。

### 2.4 运行时模型(关键约束)
- 生文(`scheme_executor`)与编排(`pipeline executor`)都跑在 **API server 进程内的后台线程 + ThreadPoolExecutor(max_workers=4)**,**没有独立 worker**,靠 `create_app()` 注入 `bg_session_factory`。
- 发文有**独立单实例 worker**(`server/worker/executor.py`),轮询 DB + `worker_id`/`worker_lease_until` 乐观锁抢占;账号登录在独立子线程。
- 浏览器自动化 = Playwright + **Xvfb/x11vnc/websockify/noVNC**(**Linux only**)+ 跨进程 `BrowserProfileLock`(DB 行锁)。Windows 本地跑不了发布。
- 一批"同进程内存共享"假设需要重做:`daily_group` 的 `itertools.count()+Lock` 计数器、`with_for_update()` 行锁去重、启动时**无租约全量复位** `running→failed`(导致"别跑多实例 web")。

### 2.5 前端
- 11 个 feature,~207 个 API 端点。
- **ant-design-pro 无法替代、必须保留的 3 个组件**:Tiptap 富文本编辑器、PipelineEditor 节点编辑器、noVNC iframe 远程桌面。
- 可平迁的交互:SSE 进度流、分块上传、图片库 lightbox、问题源多选表格。

---

## 3. 目标架构

### 3.1 服务划分与团队归属

按"领域内聚 + 团队可独立维护"划分。**核心域**强一致、需谨慎;**支撑域**低耦合、易独立。

```
                         ┌────────────────────────┐
                         │   ant-design-pro 后台    │  (统一管理后台,单前端)
                         └───────────┬────────────┘
                                     │ HTTPS
                         ┌───────────▼────────────┐
                         │      API Gateway        │  Go · 统一入口/鉴权/路由
                         │  (JWT 校验 · MCP token)  │
                         └───────────┬────────────┘
              ┌──────────────────────┼──────────────────────────┐
              │                      │                          │
   ┌──────────▼─────────┐ ┌──────────▼─────────┐  ┌─────────────▼────────────┐
   │  核心域 (Go)        │ │  发文域 (Python)    │  │  支撑域 (Go)              │
   ├────────────────────┤ ├────────────────────┤  ├──────────────────────────┤
   │ identity-service   │ │ publishing-service │  │ prompt-template-service  │
   │ content-service    │ │  ├ accounts/账号    │  │ image-service (MinIO)    │
   │ generation-service │ │  ├ drivers/驱动     │  │ ai-model-service         │
   │ orchestration-svc  │ │  ├ browser/noVNC    │  │ audit-service            │
   │                    │ │  └ publish worker   │  │ performance-service      │
   │                    │ │  (保留 Playwright)   │  │ auto-review-service      │
   │                    │ │                    │  │ hotlist-service (代理)   │
   └─────────┬──────────┘ └─────────┬──────────┘  └─────────────┬────────────┘
             │                      │                           │
             └──────────────────────┼───────────────────────────┘
                                     │
              ┌──────────────────────▼──────────────────────┐
              │     Event Bus (NATS JetStream)               │  异步契约总线
              └──────────────────────┬──────────────────────┘
                                     │
              ┌──────────────────────▼──────────────────────┐
              │  llm-gateway (Python, 封装 LiteLLM)          │  共享 AI 出口
              └──────────────────────────────────────────────┘
              ┌──────────────────────────────────────────────┐
              │  MySQL (共享库, 表所有权软隔离) · Redis · MinIO │
              └──────────────────────────────────────────────┘
```

### 3.2 服务清单与团队建议

| 服务 | 语言 | 拥有的核心表 | 团队 | 拆分难度 |
|---|---|---|---|---|
| **identity-service** | Go | User, Platform, WorkerHeartbeat | 平台基础组 | 中(被全员依赖,先做) |
| **content-service** | Go | Article, ArticleGroup, ArticleGroupItem, Asset, ArticleBodyAsset, Tag | 内容组 | 高(枢纽 + 三份正文 + review 状态机) |
| **generation-service** | Go | QuestionPool, QuestionItem, GenerationScheme*, GenerationSchemeRun* | 生文组 | 高(依赖最重) |
| **orchestration-service** | Go | Pipeline, PipelineNode, PipelineRun, PipelineVersion | 编排组 | 高(跨服务编排) |
| **publishing-service** | **Python(保留)** | PublishTask, PublishRecord, TaskLog, Account, BrowserSession, *ProfileLock, AccountLoginSession | 发文组 | 中(整体迁移,不重写) |
| **prompt-template-service** | Go | PromptTemplate | 内容组/共享 | 低 |
| **image-service** | Go | StockCategory, StockImage | 内容组 | 低 |
| **ai-model-service** | Go | AiModel | 生文组 | 低 |
| **audit-service** | Go | AuditLog | 平台基础组 | 低 |
| **performance-service** | Go | (聚合,读 records/articles) | 数据组 | 低 |
| **auto-review-service** | Go | AutoReviewDecision | 生文组 | 低 |
| **hotlist-service** | Go | (无表,代理 DailyHotApi) | 平台基础组 | 低 |
| **llm-gateway** | **Python** | (无表) | 生文组/共享 | — |
| **api-gateway** | Go | (无表) | 平台基础组 | — |

> **关键设计:把 `accounts` 并入 `publishing-service`**。账号凭据解密(`GEO_SECRET_KEY`)、登录会话状态机、`BrowserProfileLock` 跨进程锁、noVNC 接管全部与浏览器发布强绑定且 Linux/Playwright bound,强行拆开只会制造分布式锁噩梦。让"发文组"完整拥有"账号 + 驱动 + 发布",是最自然的团队边界。

> **为什么保留 `llm-gateway` 用 Python**:LiteLLM 是 Python 生态;`ai_format`(标题识别/智能配图选词)、方案运行生文都走它。Go 服务通过内网 HTTP(OpenAI 兼容协议)调它即可,无需在 Go 里重建多模型路由 + 引擎回落逻辑。MCP loop 的"主对话生文"路径本就不调 LiteLLM,不受影响。

---

## 4. 技术栈选型

| 关注点 | 选型 | 理由 |
|---|---|---|
| **Go 微服务框架** | **go-zero**(主推) | 自带 goctl 代码生成(`.api`→handler/logic/svc/types)、RPC、服务发现、限流熔断;团队已有 `go-zero-engineer` agent 可直接落地。备选 Kratos(更 DDD,但脚手架成本高)。 |
| **服务间同步调用** | go-zero RPC(zRPC/gRPC) | 核心域内部低延迟 RPC;对外统一走 Gateway REST。 |
| **异步事件总线** | **NATS JetStream** | Go 原生、轻量、内置持久化/重放/消费组,契合本系统体量。备选 Kafka(若未来需要高吞吐 CDC/数仓回流再上)。 |
| **API 网关** | go-zero gateway / 自建 + Kong/APISIX 可选 | 统一 JWT cookie 校验、MCP token 旁路、路由、灰度。 |
| **跨服务事务** | **Saga(编排式)+ Outbox 模式** | 见 §7。不引入重型 2PC;必要时评估 temporal.io。 |
| **配置中心** | 环境变量 + 可选 Nacos/Consul | 沿用现有 `GEO_*` 前缀约定,平滑过渡。 |
| **服务注册发现** | go-zero 内置(etcd)/ K8s Service | 视部署形态(见 §10)。 |
| **可观测性** | OpenTelemetry + Prometheus + Grafana + Loki | 跨服务 trace 必备(单体调试经验不可迁移)。 |
| **数据库** | MySQL 8.0(共享→拆) + Redis(分布式锁/计数/队列) | Redis 替代进程内 `Lock`/`itertools.count`/`ObservableGate`。 |
| **对象存储** | MinIO(沿用) | image-service 直连。 |
| **前端** | ant-design-pro(UmiJS/Max)+ 保留 Tiptap/PipelineEditor/noVNC | 见 §8。 |

---

## 5. 服务边界与数据所有权

### 5.1 表所有权(共享库软隔离阶段的铁律)

- 每张表**有且仅有一个 owner 服务**可写;其它服务**只能通过 owner 的 API/事件**读写,**禁止跨服务直接 JOIN 或 UPDATE**。
- 在共享库阶段用**库账号权限**强制:每个服务用独立 DB user,只 GRANT 自己拥有的表的写权限(关键跨服务读可先开只读视图,逐步换成 API)。
- 这条纪律是后续"物理拆库零痛苦"的前提。违反一次,拆库就多一处数据双写。

### 5.2 跨服务读的处理
当前大量"懒加载 JOIN"(如发布时读 Article 正文、读 Account 凭据)在拆分后变成跨服务调用。策略:
- **发布读文章**:`publishing-service` 通过 `content-service` 的 `GET /internal/articles/{id}/publish-payload` 一次性取齐(正文段落 + 资产本地路径 + 封面),即现有 `PublishPayload` 的构建移到 content 侧产出,publishing 只消费 —— 这与现有"驱动拿到的是已构建好的 PublishPayload、不碰 ORM"设计天然契合。
- **冗余只读副本**:对高频只读、可容忍秒级延迟的字段(如文章标题、平台名),消费方通过事件维护本地只读投影(CQRS read model),减少同步 RPC。

---

## 6. 契约与异步事件设计

### 6.1 契约规范
- **同步契约**:每个 Go 服务用 go-zero `.api` 文件定义 REST/RPC,作为唯一事实源,`goctl` 生成骨架。对外 API 沿用现有 `/api/*` 路径(Gateway 透传),保证前端零改动迁移。
- **异步契约**:事件用 **CloudEvents 信封 + JSON Schema(或 protobuf)** 定义,集中放 `contracts/events/` 仓库(独立版本化,各服务依赖发布版本)。事件**只增不改**,字段演进用新版本 subject。

### 6.2 核心事件目录(草案)

| 事件 subject | 生产者 | 主要消费者 | 载荷要点 |
|---|---|---|---|
| `content.article.created` | content | generation/performance | article_id, user_id, source |
| `content.article.review_status_changed` | content | orchestration/auto-review/audit | article_id, from, to, actor |
| `generation.scheme_run.requested` | generation | generation(worker) | run_id |
| `generation.scheme_run.completed` | generation | content/orchestration | run_id, article_ids, status |
| `orchestration.distribute.requested` | orchestration | **publishing** | article_ids 或 group_id, platform_codes, requested_by |
| `publishing.task.created` | publishing | orchestration | task_id, status |
| `publishing.record.completed` | publishing | performance/auto-review/content(metrics) | record_id, article_id, account_id, status, publish_url |
| `publishing.record.metrics_reported` | publishing | performance/content | record_id, metrics |
| `identity.user.*` / `account.status_changed` | identity/publishing | 相关方 | — |

### 6.3 可靠投递:Outbox 模式
- 任何"改本地表 + 发事件"的操作,事件先写本地 `outbox` 表(同一事务),由独立 relay 进程读 outbox → 发 NATS → 标记已发。杜绝"DB 提交了但事件丢了"。
- 消费端**幂等**:用事件 id + 业务唯一键去重(复用现有 `client_request_id` 幂等经验)。

---

## 7. 三大难点事务的解法

### T1 — `create_task`(发文建任务)
拆分后跨 content + publishing(accounts 已并入 publishing)。
- **方案**:同步**编排式 Saga**,由 publishing 发起:
  1. publishing 调 `content-service` 校验 `article_ids` 均 `approved`(一次批量 RPC);
  2. publishing 本地校验账号有效(同服务,无跨服务);
  3. 本地事务建 `PublishTask×Record`,失败本地回滚(无跨服务补偿,简单)。
- 因为 accounts 与 tasks 同服务,T1 从"4 模块跨表"降为"1 次跨服务只读校验",**难度大幅下降**。

### T2 — `distribute` 节点(编排建任务)
- **方案**:**异步事件 + 状态回写**,把同步建任务改成最终一致:
  1. orchestration 的 distribute 节点发 `orchestration.distribute.requested`(带 article_ids 优先 / group_id 兜底,保留现有"先判 article_ids 再判 group_id"优先级);
  2. publishing 消费 → 执行 T1 → 发 `publishing.task.created`;
  3. orchestration 异步收到回执更新节点结果;超时/账号全停用 → 节点标 `skipped`(与现有"安静跳过"语义一致)。
- 代价:pipeline run 的 distribute 节点由"同步完成"变"提交后异步完成",前端运行日志需展示"分发已提交,等待发文服务回执"。

### T3/T6 — pipeline 线性执行改 `review_status` + 三份正文
- **单一写者原则**:`review_status` 和三份正文的写入**收归 content-service 独占**。
  - orchestration 的 `to_review` 节点 → 调 `content-service` 的 `POST /internal/articles:mark-pending-and-group`(把现有 `mark_pending_and_group` 整体搬进 content,作为原子 API)。
  - auto-review、ai_generation 的 pending 写入同样走 content API。
  - **这一步直接消灭了"自动/人工/Loop 三路竞争"** —— 状态机只有一个写者,并发由 content 内部串行/CAS 保证。
- **三份正文同步**(`markdown_to_tiptap`/`markdown_to_html`)整体留在 content-service 内部,**永不跨服务拆**,保持原子。
  - ⚠️ 风险:转换器是非平凡 Python(Tiptap schema)。Go 移植成本高 —— 见 §11 备选(content 转换子模块可短期保留 Python sidecar)。

---

## 8. 前端 ant-design-pro 重构方案

### 8.1 框架
- 用 **ant-design-pro(UmiJS Max)**:ProLayout 侧边栏 + 顶栏、ProTable 统一列表、ProForm 统一表单、`access` 权限模型直接映射现有 `admin`/`operator`。
- 现有 `App.tsx` 的 tab 懒挂载 + ErrorBoundary 模式天然对应 Pro 的路由级 code-splitting,迁移顺滑。

### 8.2 组件迁移策略
| 组件 | 策略 | 成本 |
|---|---|---|
| Tiptap 富文本编辑器 | **保留原组件**,Pro 只包外层 Drawer/Modal | 低 |
| PipelineEditor 节点编辑器 | **保留原组件**,Pro 包外层 | 低 |
| noVNC iframe | **保留原组件**(零改动,iframe 内容无关) | 零 |
| SSE 任务进度流 | 自建 `useEventSource` hook + ProTable 虚拟列表 | 中 |
| 分块上传(3MB×4) | 复用现有 `chunked-upload.ts`,套 Pro Upload UI | 低 |
| 图片库 lightbox/搜索 | ProTable grid + Image.PreviewGroup + ProForm 搜索 | 中 |
| 问题源多选表格 | ProTable rowSelection(types×record_ids 矩阵) | 中 |
| 列表/表单类(articles/accounts/tasks 等) | ProTable + ProForm 覆盖约 70% | 中 |

### 8.3 鉴权
- JWT cookie 走 Gateway,前端 `getInitialState` 拉用户态,`access` 控制菜单/路由。
- MCP token 仅"MCP 接入"页用,单独注入 header。

### 8.4 重要约束
- 前端**先对接 Gateway 聚合的现有 `/api/*`**(路径不变),与后端微服务化解耦 —— 前端重构可与后端迁移**并行推进、互不阻塞**(对应你选的"前端+网关可先行")。

---

## 9. 渐进式迁移路线(绞杀者)

总原则:**任何一步都可独立上线、可回滚,业务不中断**。Gateway 是绞杀者的"接缝",通过路由开关把单个端点/前缀从 Python 单体切到新 Go 服务。

### Phase 0 — 地基(不动业务逻辑)
- 引入 **API Gateway**,把现有 `/api/*` 全量反代到现 Python 单体(透明,零行为变化)。
- 立 **NATS JetStream + Outbox 表 + contracts 仓库** 骨架。
- 立可观测性栈(OTel/Prometheus/Grafana/Loki)。
- 前端团队同步启动 **ant-design-pro** 骨架,对接 Gateway。
- ✅ 验收:流量经 Gateway,行为与现状一致。

### Phase 1 — 切支撑域(低风险练手)
顺序:`hotlist-service` → `ai-model-service` → `prompt-template-service` → `image-service` → `audit-service`。
- 每个服务:用 go-zero 实现 → 双跑校验 → Gateway 把对应 `/api/*` 前缀切到 Go → 观察 → 下线 Python 对应路由。
- 这些表低耦合、近乎纯 CRUD/只读,可直接 database-per-service(对应你选的"先共享、可对低耦合域率先独立")。
- ✅ 价值:团队跑通"Go 服务 + 契约 + 事件 + 网关切流 + 回滚"全流程。

### Phase 2 — identity-service
- 抽 User/Platform/鉴权。JWT 签发/校验移到 Gateway + identity。
- 全员依赖,务必灰度;保留 Python 侧校验做兜底直到稳定。

### Phase 3 — content-service(枢纽,最关键)
- 搬 Article/Group/Asset + 三份正文同步 + `review_status` 单一写者 API + `mark_pending_and_group`。
- 提供 `/internal/articles/{id}/publish-payload` 给发文消费。
- ⚠️ 数据双写校验期最长;充分灰度。Tiptap 转换器视情况 Go 移植或 Python sidecar(§11)。

### Phase 4 — publishing-service(Python 整体抽出,不重写)
- 把现 `tasks` + `accounts` + `worker` + `drivers` + `browser` **整体打包**成独立部署单元(仍是 Python/FastAPI + 独立 worker 进程)。
- 改造点:① 建任务的文章/账号校验改调 content-service;② distribute 改成消费 `orchestration.distribute.requested` 事件;③ metrics/记录完成发事件。
- worker **仍单实例**(profile 锁 + 无租约复位约束未变);高可用走主备而非水平扩展。
- ✅ 发文域从此可独立发版、独立扩缩(发文组完全自治)。

### Phase 5 — generation-service + orchestration-service
- 把后台线程执行模型改成 **NATS 消费 + Go worker**:
  - `_RUN_GATE`(ObservableGate)→ Redis 信号量/分布式锁;
  - `daily_group` 进程内计数器 → Redis `INCR`(或 DB `RETURNING`);
  - `with_for_update` 去重 → DB 唯一约束(活跃 run 唯一)或 Redis `SET NX`;
  - 启动**无租约全量复位** → **租约/心跳**(`last_heartbeat_at` 超时才置 failed)→ **从此可多实例**。
- orchestration 的 distribute/to_review 改成事件/content API(见 §7)。
- llm-gateway(Python)上线,generation 经它调 LiteLLM。
- ✅ 生文/编排可多实例水平扩展(解除"单 web 实例"魔咒)。

### Phase 6 — 收尾
- `performance` / `auto-review` 切 Go。
- MCP server:FastMCP **保留作为 Claude Code 网关**,内部 HTTP 调各 Go 服务(改动最小,主对话生文路径不变)。
- Python 单体退役为空壳;评估各服务**物理拆库**(把软隔离的表迁到独立库实例)。
- 前端 ant-design-pro 全量上线,旧 Vite 前端下线。

---

## 10. 跨切面关注点

- **部署形态**:推荐 K8s(Go 服务无状态易扩),但 `publishing-service` 因 Xvfb/noVNC + 单实例 + profile 卷,单独用有状态 Deployment(replicas=1)或专用节点 + 卷级加密(LUKS/云盘)。沿用现有 docker-compose 经验平滑过渡。
- **密钥**:`GEO_SECRET_KEY`/`GEO_SECRET_KEYS` 仅 publishing-service 需要(凭据解密);其它 Go 服务不接触敏感凭据,缩小密钥暴露面。web/worker 同密钥的约束被收敛到发文域内部。
- **CI/CD**:每服务独立流水线(独立 repo 或 monorepo 分包)。Go:`go test`/`golangci-lint`;Python publishing 沿用现有 ruff/mypy/pytest 门禁。契约仓库变更触发下游兼容性检查。
- **配置**:沿用 `GEO_*` 前缀,逐服务拆分各自需要的子集。
- **数据迁移**:Alembic(Python 侧)继续管发文域;Go 侧用 golang-migrate / Atlas。共享库阶段统一一套迁移协调,避免互踩。

---

## 11. 风险与权衡

| 风险 | 说明 | 缓解 |
|---|---|---|
| **Tiptap 转换器 Go 移植** | `markdown_to_tiptap`/三份正文同步是非平凡 Python,Go 重写易引入内容 bug | content-service 初期保留 **Python 转换 sidecar**(content 调它),稳定后再评估 Go 移植或长期保留 |
| **后台线程模型重做** | 生文/编排的并发原语(Gate/计数器/行锁/无租约复位)全部依赖单进程内存,跨服务后必须换分布式版 | Phase 5 集中改造,Redis + 租约/心跳;充分压测 |
| **分布式事务复杂度** | T1/T2/T6 从本地事务变 Saga/事件,最终一致引入新失败模式 | 单一写者(review_status 收归 content)+ Outbox 幂等 + 完善 trace;难点事务单独写设计稿评审 |
| **发文仍单实例** | publishing worker 水平扩展受限(profile 锁 + 无租约复位) | 接受现状,主备高可用;长期可给 profile 锁加租约后再评估多实例 |
| **跨服务调用延迟/级联失败** | 原懒加载 JOIN 变 RPC | 关键只读做 CQRS 投影;超时/熔断/降级(go-zero 内置) |
| **团队 Go 经验** | 若团队 Go 储备不足 | 用 go-zero(goctl 降门槛)+ Phase 1 低风险服务练手 + `go-zero-engineer` agent 辅助 |
| **迁移周期长** | 绞杀者天然慢 | 每 Phase 独立交付价值(前端统一后台 + 发文自治可早期见效);允许长期 Go/Python 共存 |

---

## 12. 里程碑(粗粒度)

| 里程碑 | 内容 | 可见价值 |
|---|---|---|
| M0 | Gateway + 事件总线 + 可观测性 + ant-design-pro 骨架 | 接缝就位,前端重构启动 |
| M1 | 支撑域 5 服务切 Go | 团队跑通全流程,低风险验证 |
| M2 | identity + content 上线 | 内容枢纽 Go 化,状态机收口 |
| M3 | **publishing-service 独立** | **发文组完全自治、独立发版** |
| M4 | generation + orchestration 多实例 | **生文/编排独立 + 解除单实例约束** |
| M5 | 收尾、拆库、旧前端下线 | 单体退役,目标架构达成 |

---

## 附:落地下一步
1. 评审本方案,确认服务边界与团队归属。
2. 起草 `contracts/events/` 事件 schema 首版(先覆盖 §6.2 核心事件)。
3. 起草 Gateway 路由切换清单(端点→服务映射表)。
4. 对 T1/T2/T6 三个难点事务各写一份独立设计稿(`docs/specs/*-saga-design.md`)。
5. content-service 的 Tiptap 转换:做一次 Go 移植 spike,评估"移植 vs Python sidecar"。
