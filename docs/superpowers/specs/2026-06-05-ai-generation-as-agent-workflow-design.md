# AI 生文拆解为智能体工作流 — 设计方案

- **日期**：2026-06-05
- **聚焦**：把 AI 生文（问题池 → 方案 → 运行）拆解为三个可编排、可定时运行的 pipeline 节点：`问题源 → AI创作 → 进入未审核库`，让"取问题 → 生成 → 进未审核库"能以智能体工作流形式自动定时运行。
- **落地工程**：`geo-collab` 主仓库。参考项目只读、仅作参照。
- **前置**：pipelines 引擎 + 定时调度 + 审核态 + `mark_pending_and_group` 均已在 main；AI 生文 scheme 模块（问题池/方案/运行）保持不动。

> 取舍（已与用户确认）：本设计**不复用 `GenerationScheme`（方案）实体**，而是把生成能力拆成 pipeline 节点重组。代价：不沿用方案的"问题快照"语义——每次运行从问题池取**当前**问题（对定时智能体更合适）；AI 生文现有"方案"页面与 scheme 运行**保持不动**（手动场景仍可用）。收益：智能体自包含、步骤透明、消除"问题池→方案"的多屏绕路。核心生成原语共享。

---

## 1. 背景与复用

现有可直接复用（无 HTTP 耦合）：
- 生成原语：`ai_generation/article_writer.py:generate_article_from_prompt(*, session_factory, user_id, template_content, question_text, model=None) -> int`。
- 模板随机选 + 校验：`ai_generation/scheme_executor.py:_pick_valid_template(db, allowed_ids, user_id, *, rng=None) -> tpl|None`（筛可见/未删/启用/scope=generation，随机返回）。可直接 import 复用。
- 问题渲染：`scheme_executor.py:_render_questions(questions) -> str`（编号问题列表）的逻辑。
- 问题数据：`ai_generation/models.py:QuestionItem`（`pool_id`、`category`=问题类型、`question_text`、`source_active`）；`QuestionPool`。
- 标 pending + 成组：`articles/service.py:mark_pending_and_group(session_factory, *, article_ids, user_id, base_name) -> int|None`。
- 节点框架 / 执行器 / 调度器 / AI 引擎列表 `GET /api/generation/ai-engines`、问题池/类型端点 `GET /api/generation/question-pools`、`.../{pool_id}/question-types`、提示词模板 `GET /api/prompt-templates?scope=generation`。

现有 `ai_generate` 节点是简化版（单模板 + 直给 question_text + count），**保留不动**；本次新增更适合 AI 生文流的 `ai_compose`。

---

## 2. 目标 / 非目标

### 目标
1. 三个新节点：`question_source`（问题源）、`ai_compose`（AI创作）、`to_review`（进入未审核库），可线性编排成"取问题→生成→进未审核库"。
2. 执行器与现有"运行结束自动成组"（Track A）协调：含 `to_review` 节点时跳过自动成组，由显式节点接管，避免重复成组。
3. 前端编辑器支持所需新配置字段类型（问题池、问题类型、AI 引擎、提示词模板多选）。
4. 与定时调度组合 → 每天自动取问题、生成、进未审核库；人工审核后可接（另一 spec 的）distribute 自动分发。

### 非目标（YAGNI）
- 不改 AI 生文现有"方案"页面 / scheme 运行 / 问题池同步。
- 不做问题快照（每次运行取当前问题）。
- v1 单 `question_source` 单问题类型（多类型 = 多智能体或后续迭代；线性流不分支）。
- 不删除现有 `ai_generate` 节点。
- 不新建表（问题/文章/分组都用现有模型）。

---

## 3. 三个新节点

### 3.1 `question_source`（问题源）—— `nodes/question_source.py`
- config：`{pool_id: int, question_type: str}`
- 运行（自建 session）：
  1. 校验池存在、归属 `ctx.user_id`（admin 放行，从 `User.role` 取）。
  2. 查 `QuestionItem` where `pool_id == cfg.pool_id` AND `category == cfg.question_type` AND `source_active == True`，按 id 升序。
  3. 渲染成编号问题文本（复用 `_render_questions` 逻辑）。
  4. 输出 `{"question_text": rendered, "question_item_ids": [...], "question_count": n}`。
  - 无匹配问题 → 输出 `question_text=""`（让下游 `ai_compose` 视为无内容跳过，见 3.2），不抛错（定时跑且池暂空时安静）。
- 缺 `pool_id`/`question_type` → `ValidationError`。

### 3.2 `ai_compose`（AI创作）—— `nodes/ai_compose.py`
- config：`{ai_engine: str|null, prompt_template_ids: list[int], count: int}`（count 默认 1，夹紧到 `settings.ai_generate_max_count`）。
- 输入：`question_text` 取自 `ctx.inputs`（上游问题源）→ 兜底 `cfg.question_text`。
- 运行：
  1. `question_text` 为空 → 输出 `{"article_ids": [], "skipped": "无问题可生成"}`，不生成、不报错（定时跑无问题时安静）。
  2. `prompt_template_ids` 为空 → `ValidationError`。
  3. 循环 `count` 次：`tpl = _pick_valid_template(db, prompt_template_ids, ctx.user_id)`；全无效 → 记错误、该篇跳过（不整体失败，收集到 errors）；否则 `generate_article_from_prompt(session_factory=ctx.session_factory, user_id=ctx.user_id, template_content=tpl.content, question_text=question_text, model=ai_engine)` → article_id。
  4. 输出 `{"article_ids": [...], "errors": [...]}`。
- 并发：可用 `ThreadPoolExecutor(max_workers=4)`（与 scheme/现有 ai_generate 一致）；实现时保持每线程自建 session。

### 3.3 `to_review`（进入未审核库）—— `nodes/to_review.py`
- config：`{group_name: str|null}`（可选组名）。
- 输入：`article_ids` 取自 `ctx.inputs`（上游 ai_compose）→ 兜底 `cfg`。
- 运行：
  - `article_ids` 为空 → 输出 `{"skipped": "无文章"}`，不建组。
  - 否则 `base_name = cfg.group_name or "{run时间} · {pipeline名}"`（pipeline 名/时间由节点取——见实现注），调 `mark_pending_and_group(ctx.session_factory, article_ids=..., user_id=ctx.user_id, base_name=base_name)` → 标 pending + 成组（进「图文工作台·未审核」）。
  - 输出 `{"group_id": gid, "article_ids": [...]}`。
- 注：节点内拿 pipeline 名需要 pipeline_id——`NodeRunContext` 当前不带 pipeline_id。**实现时给 `NodeRunContext` 增一个 `pipeline_id` 字段**（执行器构造 ctx 时传入），供 to_review 组默认命名；或默认名用 `f"未审核 · run"`（不依赖 pipeline 名）。择简：默认名不依赖 pipeline 名，避免改 ctx。

### 3.4 注册
`nodes/__init__.py` 增 import：`question_source`、`ai_compose`、`to_review`（连同既有节点）。

---

## 4. 执行器协调（避免重复成组）

`pipelines/executor.py:run_pipeline` 的 Track A 自动成组段（现 `if article_ids:` → `mark_pending_and_group`，约 line 168-185）：
- 计算 `has_to_review = any(s["node_type"] == "to_review" for s in node_specs)`（node_specs 在加载节点时已建）。
- 把 Track A 门条件改为 `if article_ids and not has_to_review:` —— 含显式 `to_review` 节点时由节点接管成组，执行器不再自动成组（避免一批文章被成两次组）。
- 其余（run 状态聚合、ignore_exception、依赖阻断）不变。

---

## 5. 前端

### 5.1 node-types（`/api/pipelines/node-types` 增三项 config_schema）
```json
{"type":"question_source","label":"问题源",
 "config_schema":[
   {"key":"pool_id","type":"question_pool","label":"问题池"},
   {"key":"question_type","type":"question_type","label":"问题类型"}]}
{"type":"ai_compose","label":"AI创作",
 "config_schema":[
   {"key":"ai_engine","type":"ai_engine","label":"AI 模型"},
   {"key":"prompt_template_ids","type":"prompt_templates","label":"提示词模板(可多选,运行时随机)"},
   {"key":"count","type":"number","label":"生成数量"}]}
{"type":"to_review","label":"进入未审核库",
 "config_schema":[{"key":"group_name","type":"text","label":"分组名(可空)"}]}
```

### 5.2 编辑器新增 4 种配置字段类型（`PipelineEditor` config 渲染，沿用 article_group/accounts 模式 + 已重样的 .agentField 风格）
- `question_pool`：下拉，选项来自 `GET /api/generation/question-pools`（取 `id/name`）。值存 pool_id(number)。
- `question_type`：下拉，**依赖同节点已选 pool_id**——读 `sel.config.pool_id`，按需拉 `GET /api/generation/question-pools/{pool_id}/question-types`；pool 未选时禁用/提示先选池。值存 question_type(string)。
- `ai_engine`：下拉，选项来自 `GET /api/generation/ai-engines`；含"系统默认"(空值)。值存 string|null。
- `prompt_templates`：多选，选项来自 `GET /api/prompt-templates?scope=generation`（取 `id/name`，仅 enabled）。值存 number[]。
- 前端 api 客户端补对应封装；类型 `NodeTypeDef.config_schema[].type` 已是 string，无需改类型。

---

## 6. 测试（@pytest.mark.mysql，monkeypatch LLM）

新增 `server/tests/test_ai_generation_nodes.py`：
1. **question_source 取类型**：建池 + 若干 `QuestionItem`（不同 category，部分 source_active=False）；run question_source(pool,type) → 只取该类型 active 的，question_text 含其文本、question_item_ids 正确；无匹配 → question_text=""、不报错。
2. **ai_compose 生成**：monkeypatch `ai_compose` 内 `generate_article_from_prompt` 造真实文章；给 question_text + 两个有效模板 + count=3 → 产 3 篇、article_ids 非空；模板全无效 → errors 有值、article_ids 空、不整体失败；question_text 空 → skipped、不生成。
3. **to_review**：给 article_ids（默认 approved 文章）→ 标 pending、建组、组内含这些文章；空 article_ids → skipped 不建组。
4. **执行器协调**：含 to_review 的 pipeline run 后，文章只成**一个**组（执行器 Track A 未重复成组）；不含 to_review 的生成型 pipeline 仍由 Track A 自动成组（回归现有行为）。
5. **端到端**：`question_source → ai_compose → to_review` publish + run（同步）→ run done、产出文章 pending 且成组、出现在「未审核」。

前端：`pnpm --filter @geo/web typecheck && build`。

---

## 7. 关键决策（已与用户确认）
1. 三节点拆解 `问题源 → AI创作 → 进入未审核库`（不复用 scheme 实体；AI 生文方案页保持不动）。
2. 每次运行取**当前**问题（不快照）。
3. 含 `to_review` 时执行器跳过自动成组（显式节点接管）。
4. v1 单源单类型；保留现有 `ai_generate` 节点。

## 8. 风险与缓解
- **后台线程 session**：节点/生成各自建 session、本线程 commit/close（与现有一致）。
- **NodeRunContext 不带 pipeline_id**：to_review 默认组名**不依赖 pipeline 名**（避免改 ctx 接口）；若以后要更友好命名再扩 ctx。
- **question_type 依赖 pool 的级联下拉**：编辑器读同节点 `sel.config.pool_id` 动态拉类型；pool 未选时禁用。属前端小复杂度，已在 §5.2 明确。
- **空内容语义**：question_source 无问题 → 空 question_text → ai_compose skipped → to_review skipped；整条链安静 done，不报错（定时跑空池的关键），测试覆盖。
- **与现有 ai_generate / Track A 兼容**：ai_generate 保留；Track A 仅在"无 to_review 节点"时生效，回归测试覆盖。
- **AI Key 缺失**：生成时 LiteLLM 报错被 ai_compose 收进 errors（best-effort），不崩整条 run。

## 9. 验收标准
1. 三节点可在智能体管理→工作流编排里拼成 `问题源 → AI创作 → 进入未审核库`，前端可选问题池/类型/模型/模板。
2. 运行后：按类型取当前问题 → 用允许模板随机生成 count 篇 → 文章进「未审核」且成组。
3. 含 to_review 时不重复成组；不含时维持现有自动成组。
4. 空池/无问题/无有效模板时安静 done、不报错。
5. 挂定时调度可"每天自动取问题生成进未审核库"。
6. 不改 AI 生文方案页/scheme 运行、不新建表；纯逻辑/集成/前端门禁全绿。
