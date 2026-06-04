# 问题池自动化与方案池重构需求与计划

## 背景

当前项目整体自动化程度偏低。后续要建设自动化生文 pipeline，第一步从“问题池”模块开刀。

现有问题池已经支持从飞书多维表同步提问词，但当前实现把问题池同时当作同步数据源、生成队列和消费状态记录。新的方向是把这些职责拆开：

- 问题池只作为飞书文档的本地镜像。
- 方案池负责定义生文方案。
- 方案运行记录负责承接一次实际生文任务。
- 后续 AI 生文以 `scheme_id` 为核心入口，不再直接消费问题池。

## 已确认需求

### 1. 问题池改为飞书镜像

- 问题池需要定时从飞书同步。
- 问题池状态必须与飞书文档保持一致。
- 问题池不再随着生文任务、发布任务或文章生成结果改变。
- 唯一对齐点是飞书文档。
- 现有飞书读取同步能力已经写好，这块作为固定基础继续复用。

同步语义调整：

- 飞书存在的记录，本地新增或更新。
- 飞书缺失的记录，本地软标记为缺失，不物理删除。
- 缺失记录如果后续又在飞书出现，应恢复 active。
- 旧的 `pending/consumed/article_id` 语义不再用于新方案流，可保留做兼容字段。

### 2. 新增方案池/方案表

新增方案作为后续生文任务的固定定义。

方案应至少表达：

- 方案 id。
- 所属问题池。
- 问题类型。
- 选中的具体问题。
- 该问题类型要生成的文章数。
- 该问题类型允许使用的提示词模板 id 列表。

示例：

- A 类型有问题 1、2、3。
- B 类型有问题 1、2、3、4、5。
- 方案中勾选 A 类型的问题 1、3，文章数为 3。
- 方案中勾选 B 类型的问题 1、2、3、4、5，文章数为 1。
- 执行时：
  - 用 A 类型选中的问题 1、3 生成 3 篇文章。
  - 用 B 类型选中的问题 1、2、3、4、5 生成 1 篇文章。

方案是长期可复用对象，不是一次性草稿。

### 3. 核心粒度是问题类型

本次重构的核心不是单条问题，而是问题类型。

以下配置都基于问题类型：

- 选哪些问题。
- 生成多少篇文章。
- 允许使用哪些提示词模板。
- 每篇文章实际随机采用哪个提示词模板。

`QuestionItem.category` 暂时作为问题类型字段使用，API 层可以命名为 `question_type`。

### 4. 提示词模板逻辑

新方案流不再使用 Skill。

Skill 是给 agent 的旧能力入口；后续 pipeline 暂不考虑 Skill，只使用提示词模板。

每个方案行要保存：

- `allowed_prompt_template_ids`：该问题类型允许使用的多个提示词模板 id。

方案运行时：

- 每篇文章独立从该问题类型允许列表中随机选择一个提示词模板。
- 实际选中的模板 id 需要记录下来。
- 需要异常处理：
  - 模板不存在。
  - 模板被删除。
  - 模板被停用。
  - 模板不可见。
  - 模板 scope 不是 `generation`。
- 已确认策略：如果某问题类型的模板列表在运行时无效，该类型对应任务失败并记录错误，其他类型继续执行。

### 5. 方案运行表

后续 AI 生文固定以方案 id 执行。

需要新增独立的方案运行记录表，记录一次实际生文任务，独立于方案定义。

运行记录需要保存：

- 执行状态。
- 生成文章 id 列表。
- 每篇文章的任务明细。
- 每篇文章实际采用的提示词模板 id。
- 问题文本快照。
- 错误信息。

每篇文章一条任务明细。

## 已确认设计决策

- 问题池行与飞书同步后保持本地外键身份。
- 飞书删除或隐藏的问题，本地软标记缺失。
- 方案引用 `QuestionItem` 外键，同时保存题目文本和问题类型快照。
- 方案执行时使用方案快照，不使用问题池最新文本。
- 飞书后续修改不会自动改变已保存方案。
- 定时同步使用应用内轻量后台 scheduler。
- 定时同步默认频率为 6 小时。
- 方案行的允许模板列表放在方案行上，不新增全局问题类型配置表。
- 提示词随机粒度是每篇文章随机一次。
- 旧的“直接勾选问题/自动 N 篇”流程由新方案流直接替换。
- 前端暂不纳入本轮实现，优先完成数据库、后端 API、同步和生文执行链路。

## 建议数据模型

### QuestionPool

保留现有问题池表，并补充池级同步状态：

- `last_synced_at`
- `last_sync_error`
- `auto_sync_enabled`

继续保留：

- `feishu_app_token`
- `feishu_table_id`
- `is_deleted`

### QuestionItem

保留现有问题项表，并补充飞书镜像状态：

- `source_active`
- `source_deleted_at`
- `last_seen_at`

继续保留：

- `pool_id`
- `record_id`
- `fields`
- `question_text`
- `category`
- `synced_at`

兼容保留但新流程不再使用：

- `status`
- `article_id`

### GenerationScheme

新增方案头表：

- `id`
- `user_id`
- `pool_id`
- `name`
- `is_enabled`
- `is_deleted`
- `created_at`
- `updated_at`

### GenerationSchemeLine

新增方案行表。每行对应一个问题类型：

- `id`
- `scheme_id`
- `question_type`
- `article_count`
- `allowed_prompt_template_ids`
- `created_at`
- `updated_at`

`allowed_prompt_template_ids` 可先用 JSON 数组保存。

### GenerationSchemeLineQuestion

新增方案行问题表：

- `id`
- `scheme_line_id`
- `question_item_id`
- `record_id`
- `question_text`
- `question_type`
- `created_at`

这里保存外键加快照：

- 外键用于联动和追溯来源。
- 快照用于保证方案执行稳定。

### GenerationSchemeRun

新增方案运行头表：

- `id`
- `scheme_id`
- `user_id`
- `status`
- `article_ids`
- `error_message`
- `created_at`
- `completed_at`

建议状态：

- `pending`
- `running`
- `done`
- `partial_failed`
- `failed`

### GenerationSchemeRunTask

新增每篇文章的运行明细：

- `id`
- `run_id`
- `scheme_line_id`
- `question_type`
- `question_text`
- `question_item_ids`
- `allowed_prompt_template_ids`
- `actual_prompt_template_id`
- `status`
- `article_id`
- `error_message`
- `created_at`
- `completed_at`

建议状态：

- `pending`
- `running`
- `done`
- `failed`

## API 计划

### 问题池 API

保留现有接口：

- `GET /api/generation/question-pools`
- `POST /api/generation/question-pools`
- `POST /api/generation/question-pools/{pool_id}/sync`
- `GET /api/generation/question-pools/{pool_id}/items`

新增按问题类型聚合读取接口：

- `GET /api/generation/question-pools/{pool_id}/question-types`

用途：

- 给方案录入页展示每个问题类型下有哪些 active 问题。

### 方案 API

新增：

- `GET /api/generation/schemes`
- `POST /api/generation/schemes`
- `GET /api/generation/schemes/{scheme_id}`
- `PUT /api/generation/schemes/{scheme_id}`
- `DELETE /api/generation/schemes/{scheme_id}`

创建/更新方案时校验：

- 方案所属问题池存在且有权限。
- 选中问题都属于该问题池。
- 选中问题都是 `source_active=true`。
- 选中问题的问题类型与方案行 `question_type` 一致。
- `article_count > 0`。
- `allowed_prompt_template_ids` 非空。
- 所有允许模板都可见、未删除、已启用、`scope=generation`。

### 方案运行 API

新增：

- `POST /api/generation/schemes/{scheme_id}/runs`
- `GET /api/generation/scheme-runs/{run_id}`

执行逻辑：

- 根据方案行展开 run tasks。
- 每个方案行按 `article_count` 生成 N 条任务。
- 每条任务使用该方案行的问题快照。
- 每条任务独立随机选择一个实际提示词模板。
- 每条任务生成一篇文章。
- 任务成功后写入 `article_id`。
- run 根据任务结果汇总为 `done`、`partial_failed` 或 `failed`。

### 旧 Session API

旧 `/api/generation/sessions` 中的问题池勾选/自动生成路径不再作为主流程使用。

建议处理：

- 如果请求包含 `question_item_ids` 或 `auto_count`，返回明确错误，提示使用 scheme run。
- 非问题池旧逻辑可按实际是否还需要决定保留或废弃。

## 生文逻辑

每篇文章的用户提示词：

- 读取实际随机选中的 `PromptTemplate.content`。
- 将题目快照渲染成编号问题列表。
- 如果模板包含 `{{问题}}`，替换该占位符。
- 如果模板不包含 `{{问题}}`，追加：

```text
## 用户问题

1. xxx
2. yyy
```

系统提示词：

- 使用通用写作系统提示。
- 不再拼 Skill 内容。

文章生成后：

- 仍使用现有 Markdown 到 Tiptap/HTML 转换。
- 仍使用 `create_article()` 入库。
- 不修改 `QuestionItem.status`。
- 不写 `QuestionItem.article_id`。

## 定时同步计划

新增应用内后台线程：

- FastAPI 启动后创建 daemon thread。
- 默认每 6 小时运行一次。
- 每轮扫描绑定飞书配置且未删除、启用自动同步的问题池。
- 对每个池调用同步函数。
- 单池失败只记录错误，不影响其他池。

新增配置：

```bash
GEO_QUESTION_POOL_AUTO_SYNC_ENABLED=true
GEO_QUESTION_POOL_SYNC_INTERVAL_SECONDS=21600
```

测试中应默认禁用或可控触发单轮同步，避免真实 sleep 和真实飞书请求。

## 测试计划

### 飞书同步

- 新增飞书记录后本地新增。
- 飞书记录更新后本地更新。
- 飞书记录缺失后本地软标记 `source_active=false`。
- 缺失记录再次出现后恢复 active。
- 飞书同步失败记录错误，不影响服务。

### 方案校验

- 跨池问题失败。
- 问题类型不一致失败。
- 非 active 问题失败。
- 空题目失败。
- `article_count <= 0` 失败。
- 模板不存在失败。
- 模板停用失败。
- 模板删除失败。
- 模板 scope 非 `generation` 失败。

### 方案执行

- 按问题类型展开文章数。
- A 类型选两个问题、文章数 3，生成 3 条任务。
- 每篇文章记录 `actual_prompt_template_id`。
- 执行使用方案快照，不受问题池后续同步影响。
- 执行不改动 `QuestionItem.status/article_id`。

### 异常场景

- 某类型模板无效时，该类型任务失败，其他类型继续。
- LLM 失败时只影响运行任务，不污染方案定义。
- 全部任务失败时 run 为 `failed`。
- 部分任务成功时 run 为 `partial_failed`。
- 全部成功时 run 为 `done`。

### 定时同步

- 测试单轮 scheduler。
- 不跑真实 sleep。
- 不调用真实飞书。

## 实施顺序

1. 新增模型和 Alembic 迁移。
2. 调整问题池同步为飞书镜像语义。
3. 增加定时同步配置和后台同步循环。
4. 新增方案 schemas/service/router。
5. 新增方案运行 executor。
6. 旧 `/sessions` 问题池直连路径改为明确错误或迁移提示。
7. 补充后端测试。
8. 后续再单独调整前端方案录入与执行界面。
