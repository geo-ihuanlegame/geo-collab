# geo-collab 可视化流程编排引擎 — 设计方案

- **日期**：2026-06-04
- **聚焦模块**：可视化流程编排引擎（原始需求 Section 三）+ 第一个真实节点 = AI 生文（Section 四的节点部分）
- **落地工程**：`geo-collab` 主仓库（**唯一改动目标**）
  - 后端：`server/app/`（FastAPI + SQLAlchemy + Alembic，MySQL only）
  - 前端：`web/`（React 19 + Vite + TypeScript + Tiptap）
- **参考项目**：`content-library-public`（Java）/ `pc-admin-conetnt-library-public`（Vue）**仅作架构参照，不可改动**。

> 取代 `2026-06-04-pipline-visual-orchestration-design.md`（那份针对参考项目、已作废）。
> 本 spec 只覆盖"编排引擎 + AI 生文节点"。智能体管理 tab、内容审核库、内容分发节点等其余模块各自另起 spec，不在本次范围。

---

## 1. 背景

geo-collab 当前**没有任何工作流/编排能力**。其 AI 流程是"问题池 → 方案池 → 方案运行"（`ai_generation` 模块）。本次参照两个参考项目的 pipLine 架构（线性顺序流水线 + 节点 + 草稿/版本），在 geo-collab 里**从零新建**一个 `pipelines` 模块，并把现有 AI 生文能力封装为第一个可编排节点。

参考项目关键架构（已调研，仅作参照）：节点按 `item_index` 线性执行、节点配置存 JSON、插件式节点分发、执行有运行日志。geo-collab 版本沿用"线性 + 节点注册表 + JSON 配置"，复用 geo-collab 自身的 SQLAlchemy / FastAPI / 后台线程 / React 模式。

### 复用的 geo-collab 现有能力（不重写）
- 生文入口：`server/app/modules/ai_generation/article_writer.py:generate_article_from_prompt(*, session_factory, user_id, template_content, question_text, model=None) -> int`（返回 article_id）。
- 文章落库：`server/app/modules/articles/service.py:create_article(db, user_id, ArticleCreate)`（已被 article_writer 内部调用，节点无需直接调）。
- 提示词模板：`prompt_templates` 模块（scope=`generation`）。
- 后台线程注入模式：`create_app()` 把 `bg_session_factory = SessionLocal` 注入路由模块（见 `main.py`），方案运行据此 spawn `Thread` + `ThreadPoolExecutor`。
- 运行状态机参照 `GenerationSchemeRun`（pending/running/done/partial_failed/failed）。

---

## 2. 目标与非目标

### 目标
1. **线性编排引擎**：可视化（线性节点列表，非画布库）增删/排序节点，节点属性面板可编辑参数。
2. **连线依赖 + 数据传递规则**：节点可配置从上游节点输出字段映射到本节点输入，并可配置跳过条件；执行器据此注入/跳过。
3. **草稿暂存**：编辑态可保存草稿而不影响线上运行版本；发布才生效。
4. **版本回溯**：每次发布生成快照；可查看历史、回溯（载入草稿后用户确认再发布，不直接覆盖线上）。
5. **唯一标识符**：`Pipeline.id`。
6. **两个内置节点类型**让引擎端到端可跑可测：`input`（源）、`ai_generate`（AI 生文）。
7. **导航入口**：新增「工作流编排」tab。

### 非目标（YAGNI，明确排除）
- 不引入 React Flow / 任何图库；不做自由画布 DAG、分支、循环、多上游合流。
- 不做智能体管理 tab、内容审核库、内容分发节点（各自另起 spec）。
- 不改 `ai_generation` 现有方案流（只调用其 `generate_article_from_prompt`，不修改）。
- 节点不做权限/共享（沿用 owner = user_id，admin 可见全部，与现有模块一致）。

---

## 3. 数据模型（SQLAlchemy，新 Alembic 迁移接当前 head `0036`）

新表均放 `server/app/modules/pipelines/models.py`，遵循现有 `Mapped`/`mapped_column` + `utcnow` + 原生 `JSON` 列约定。

### 3.1 `pipelines`
```
id          int PK
user_id     int FK users.id, index
name        str(200)
description str/Text nullable
draft_snapshot  JSON nullable   -- 未发布草稿全量 {schemaVersion, nodes:[...]}
has_draft   bool default False
created_at  datetime default utcnow
updated_at  datetime default utcnow (onupdate)
```

### 3.2 `pipeline_nodes`（已发布的线上节点）
```
id          int PK
pipeline_id int FK pipelines.id, index
node_type   str(64)        -- 'input' | 'ai_generate'
name        str(200)
node_index  int            -- 线性顺序，0 起
config      JSON default dict   -- 节点参数（按 node_type 不同）
flow_meta   JSON nullable       -- {schemaVersion, dependsOnIndex, inputMapping:[{from,to}], condition:{field,op,value}}
created_at  datetime default utcnow
```

### 3.3 `pipeline_versions`
```
id          int PK
pipeline_id int FK pipelines.id, index
version_no  int            -- pipeline 内递增
snapshot    JSON           -- {schemaVersion, nodes:[...]} 全量
remark      str(255) nullable
created_by  int FK users.id
created_at  datetime default utcnow
```
索引：`(pipeline_id, version_no)`。

### 3.4 `pipeline_runs`（镜像 GenerationSchemeRun）
```
id          int PK
pipeline_id int FK pipelines.id, index
user_id     int FK users.id, index
status      str(20) default 'pending'  -- CheckConstraint pending/running/done/partial_failed/failed
node_results JSON default dict          -- {node_index: {output...}} 调试/追溯用
article_ids JSON default list           -- 本次产出的文章 id
error_message Text nullable
created_at  datetime default utcnow
completed_at datetime nullable
```

> 快照 / flow_meta / config 的 JSON 顶层均带 `"schemaVersion": 1`。

---

## 4. 节点抽象与注册表

`server/app/modules/pipelines/nodes/__init__.py` 暴露注册表：

```python
# NodeContext: 累积上下文 dict（节点间数据传递）
# NodeResult: {"output": dict, "article_ids": list[int]}
NodeHandler = Callable[[NodeRunContext], NodeResult]
_REGISTRY: dict[str, NodeHandler] = {}
def register(node_type: str, handler: NodeHandler) -> None: ...
def get_handler(node_type: str) -> NodeHandler: ...   # 未知类型抛 ValidationError
```

`NodeRunContext` 携带：`session_factory`、`user_id`、`config`（本节点）、`inputs`（经 inputMapping 注入的字段）、`upstream`（上游累积 context，供节点直接读）。

### 4.1 `input` 节点（`nodes/input_node.py`）
- config：`{"question_text": str}`
- 行为：输出 `{"question_text": config.question_text}`。无外部调用。

### 4.2 `ai_generate` 节点（`nodes/ai_generate_node.py`）
- config：`{"prompt_template_id": int, "count": int, "model": str | null}`
- 行为：
  1. `question_text` 取自 `inputs.get("question_text")`（经 flow_meta inputMapping 从上游注入），缺失则取 config 兜底或抛 `ValidationError`。
  2. 校验 `prompt_template_id` 存在、scope=`generation`、未删除（否则 `ValidationError`）。
  3. 循环 `count` 次调用 `generate_article_from_prompt(session_factory=..., user_id=..., template_content=tpl.content, question_text=question_text, model=config.model)`，收集 article_ids。
  4. 输出 `{"article_ids": [...]}`。单篇失败按 run 的聚合策略计入 partial_failed（不中断后续）。

> 节点 handler 是**纯函数式**（输入 NodeRunContext，输出 NodeResult），每次自建/关闭 session（session 非线程安全），与 scheme_executor 一致。

---

## 5. 执行引擎（`pipeline_executor.py`）

镜像 `scheme_executor.py` 的后台线程 + session-per-step 模式。**线性**遍历（非并发图）：

```
create_run(db, *, pipeline, user_id) -> PipelineRun
    # 读已发布 pipeline_nodes（按 node_index 升序）快照进 run 上下文；status=pending
run_pipeline(run_id, session_factory)   # 后台线程入口
    run.status = running
    context = {}                      # 累积上下文
    for node in nodes(by node_index):
        meta = parse(node.flow_meta)
        upstream = context (或按 dependsOnIndex 取指定节点输出)
        if evaluator.should_skip(meta, upstream): 记录跳过, continue
        inputs = evaluator.apply_input_mapping(meta, upstream)
        result = get_handler(node.node_type)(NodeRunContext(...))
        context[node.node_index] = result.output
        run.article_ids += result.article_ids
        异常: 记 error；按聚合策略决定 partial_failed / failed
    run.status = aggregate(...); completed_at = utcnow()
```

- **数据传递求值器** `flow_meta.py`：`apply_input_mapping(meta, upstream) -> dict`、`should_skip(meta, ctx) -> bool`。纯逻辑，op ∈ `eq`/`neq`/`contains`。**可无 DB 单测**。
- 聚合：全成功 done；部分失败 partial_failed；全失败 failed。
- 路由 POST `/runs` 立即返回 202 + run_id，spawn `Thread(target=run_pipeline, args=(run_id, bg_session_factory))`（与 scheme 一致）。

---

## 6. 草稿 / 版本 / 快照

- **快照编解码** `snapshot.py`（纯逻辑，可无 DB 单测）：`nodes_to_snapshot(list[PipelineNode]) -> dict`、`snapshot_to_node_dicts(dict) -> list[dict]`。结构 `{schemaVersion:1, nodes:[{node_type,name,node_index,config,flow_meta}]}`。
- **保存草稿**：写 `pipelines.draft_snapshot` + `has_draft=True`，不动 `pipeline_nodes`。
- **发布**：事务内删除该 pipeline 的 `pipeline_nodes` → 按 draft_snapshot 重建 → 写一条 `pipeline_versions`（version_no = 现有 max+1）→ 清空 draft。
- **丢弃草稿**：清 draft_snapshot + has_draft=False。
- **回溯**：把指定 version.snapshot 写入 draft_snapshot + has_draft=True（**不覆盖线上**），由前端确认后再发布。

---

## 7. API（FastAPI，挂 `/api/pipelines`，`Depends(get_current_user)`）

service 层抛 `ClientError/ConflictError/ValidationError`（不抛裸 ValueError）。

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/pipelines` | 列出当前用户的 pipeline |
| POST | `/api/pipelines` | 新建（name/description） |
| GET | `/api/pipelines/{id}` | 详情（含已发布 nodes + has_draft） |
| PATCH | `/api/pipelines/{id}` | 改 name/description |
| DELETE | `/api/pipelines/{id}` | 删除 |
| POST | `/api/pipelines/{id}/draft` | 保存草稿（body: snapshot） |
| POST | `/api/pipelines/{id}/publish` | 发布草稿（body: remark?），返回 version_no |
| POST | `/api/pipelines/{id}/draft/discard` | 丢弃草稿 |
| GET | `/api/pipelines/{id}/versions` | 版本列表（不含 snapshot 体） |
| GET | `/api/pipelines/versions/{version_id}` | 版本详情（含 snapshot） |
| POST | `/api/pipelines/versions/{version_id}/rollback` | 回溯到草稿 |
| POST | `/api/pipelines/{id}/runs` | 触发运行（202 + run_id） |
| GET | `/api/pipelines/{id}/runs` | 运行列表 |
| GET | `/api/pipelines/runs/{run_id}` | 运行详情（status/article_ids/node_results/error） |
| GET | `/api/pipelines/node-types` | 可用节点类型 + 其 config 字段 schema（供前端属性面板渲染） |

owner 校验：非 admin 只能操作自己的 pipeline（参照 `_get_owned_pool`，不属于则 404）。

`create_app()` 挂载 `pipelines_router`（prefix `/api/pipelines`，dependencies=get_current_user），并注入 `pipelines.router.bg_session_factory = SessionLocal` 与 `pipelines.pipeline_executor` 所需 factory（参照 scheme_router 注入）。

---

## 8. 前端（`web/`）

- **导航**：`web/src/types.ts` 的 `NavKey` 加 `"pipelines"`，`navItems` 加 `{ key:"pipelines", label:"工作流编排", icon:<Workflow/Network 图标> }`；`App.tsx` 加渲染块（lazy + visitedTabs 模式，参照现有 tab）。
  > 放置位置：本模块只负责加入「工作流编排」tab。需求 Section 一「智能体管理 置于首位」属另一模块，本 spec 不处理 tab 顺序。
- **API 客户端**：`web/src/api/pipelines.ts`，复用 `api<T>()` wrapper；类型加到 `web/src/types.ts`。
- **Feature 目录** `web/src/features/pipelines/`：
  - `PipelinesWorkspace.tsx`：左侧 pipeline 列表 + 右侧编辑器，参照 `AiGenerationWorkspace`。
  - `PipelineEditor.tsx`：**线性节点列表**（上下排列 + 上/下移排序 + 增/删），选中节点打开属性面板。
  - `NodePropertyPanel.tsx`：按 `node-types` 返回的 config schema 渲染表单 + 「数据传递」分栏（上游节点下拉、字段映射表、跳过条件）。
  - `VersionHistory.tsx`：版本列表 + 回溯确认。
  - 顶部操作：保存草稿 / 发布 / 丢弃草稿 / 版本历史 / 运行；`has_draft` 状态标识；运行后轮询 run 详情展示状态与 article_ids。

---

## 9. 验证策略（按 geo-collab 实情）

- **纯逻辑单测（无 DB，无 mysql 标记）**：`flow_meta.py`（apply_input_mapping / should_skip）、`snapshot.py`（往返编解码）、节点注册表（注册/取用/未知类型抛错）。直接 `pytest server/tests/test_pipeline_logic.py -q`。
- **API + 执行（`@pytest.mark.mysql`，用 `build_test_app`）**：建 pipeline → 存草稿 → 发布（验证 nodes 重建 + 版本号）→ 触发运行（**测试内同步调用 `run_pipeline(run_id, app.session_factory)`**，参照 `test_scheme_runs.py` 的 `_run_now`）→ 断言 run.status 与 article_ids。`ai_generate` 节点的 LLM 调用在测试中 monkeypatch `generate_article_from_prompt` 返回假 article_id，避免真实 LLM。
- **前端**：`pnpm --filter @geo/web typecheck` + `pnpm --filter @geo/web build`（硬门禁）；eslint 非阻塞。无前端单测框架，UI 走手动冒烟。
- **命令**：后端测试需 `GEO_TEST_DATABASE_URL`（库名含 test）。Python 工具在 dev 容器跑（宿主无 conda/Java）。

---

## 10. 关键决策（已与用户确认）

1. 落地工程 = **geo-collab 主仓库**；参考项目不可改。
2. **不引图库**，线性编排。
3. 草稿暂存 + 版本回溯 + 连线依赖/数据传递（单上游）；**回溯不覆盖线上**，载入草稿后用户确认再发布。
4. 本次节点范围 = **引擎 + `input` + `ai_generate`**（直接复用现有生文）。

## 11. 风险与缓解

- **后台线程 + session**：所有节点 handler 自建/关闭 session，禁止跨线程传 session（与 scheme_executor 一致）。
- **LLM 真调用**：测试 monkeypatch 生文函数；生产缺 AI Key 在调用时报错（geo-collab 启动不校验 Key）。
- **CORS / 端口**：前端必须 5173（CORS 写死）。
- **迁移 head 漂移**：写迁移前 `ls server/alembic/versions/` 取实际最新 head（现为 0036），不要写死。
- **JSON 列**：MySQL 原生 JSON，沿用现有 `mapped_column(JSON, default=...)`。
- **节点扩展**：node-types 经注册表 + config schema 驱动前端，未来加分发节点（Section 五）只需注册新 handler，不改引擎。

## 12. 验收标准

1. 可新建 pipeline、增删/排序节点、编辑节点参数与数据传递配置，保存草稿后线上运行版本不变。
2. 发布后 `pipeline_nodes` 与草稿一致并生成递增版本号；版本列表可见、回溯将版本载入草稿。
3. 配置 `input(question_text) → ai_generate(template,count)` 并用 inputMapping 把 question_text 传给 ai_generate，运行后产出 count 篇文章，run.status=done、article_ids 非空。
4. 数据传递 condition 不满足时节点被跳过并记录于 node_results。
5. 纯逻辑单测、mysql 集成测试、前端 typecheck+build 全绿。
