# 计划：DB 化的 AI 模型注册表 + 管理界面（去 hardcode）

## Context（为什么做这件事）

现在项目里 AI 模型的来源**全在环境变量**，没有任何 DB 表：

- 写作模型：`GEO_AI_MODEL`（默认 `claude-3-5-sonnet-20241022`）+ `GEO_AI_ENGINES`（JSON 数组，下拉可选列表）。
- 格式/配图模型：`GEO_AI_FORMAT_MODEL`（默认 `deepseek/deepseek-v4-flash`）——**固定写死，全站没有任何选择入口**。

所有写作调用统一经过 [config.py:149](server/app/core/config.py#L149) 的 `resolve_engine(selected) -> (model, api_key, base_url)` 喂给 `litellm.completion`。

**痛点**：想新增「DeepSeek pro、Claude opus、sonnet」这类模型，今天要改 `GEO_AI_ENGINES` JSON 再重启——正是用户讨厌的 env hardcode；而格式模型连选都不能选。

**目标产出**：一张 DB 表 `ai_models` 作为模型清单的唯一真相源 + admin 管理页（增删改、设默认，**无需重启、不碰代码**）。写作 + 格式两套都纳入统一注册表。**API Key 仍只存环境变量**，DB 只存元数据（含一个"环境变量名"引用），密钥永不进 DB、永不下发前端。`GEO_AI_ENGINES` 退化为首次播种 + 回落，不删除。

已确认决策：① DB 表 + 管理界面（推荐）；② 写作 + 格式两套都可配；③ Key 存 env，DB 只存元数据。

---

## 关键设计：模型 = DB 元数据，Key = env

`ai_models` 行存：`label / model(litellm串) / scope / base_url / api_key_env(环境变量名) / is_enabled / is_default / sort_order`。

**解析优先级**（写作与格式同形，**两者都返回 base_url**）：
1. **DB 行**：`selected` 非空 → 按 `model` 匹配本 scope 的 enabled 行；`selected` 空 → 取本 scope 的 `is_default` enabled 行；无匹配则进第 4 步回落。
2. **Key**：`os.environ[api_key_env]`（设了且存在）→ scope 全局 key（写作 `GEO_AI_API_KEY`；格式 `GEO_AI_FORMAT_API_KEY` 再回落 `GEO_AI_API_KEY`）→ `""`。
3. **model 串**：`row.model or scope 默认`（`settings.ai_model` / `settings.ai_format_model`）；**`base_url` 取 `row.base_url`（None=litellm 默认官方端点）**。
4. **env 回落（向后兼容）**：无任何 DB 行命中 → 写作委托现有 `config.resolve_engine`（仍读 `GEO_AI_ENGINES` 内联 key）；格式回落 `settings.ai_format_model`/key（base_url=None）。**即新装/未播种时行为与今天完全一致**。

### 中转站（OpenAI 兼容网关）怎么配

`base_url` 是 `ai_models` 表的一列、**管理页每行可改**——这就是中转站的入口。一条中转站模型行 = `model`（中转站约定的模型串）+ `base_url`（中转站地址）+ `api_key_env`（指向存中转站 key 的环境变量名，如 `GEO_RELAY_KEY`）。**中转站的 key 仍只存环境变量**（你的决策③），DB 行只记元数据，运行时按 `api_key_env` 从 env 取、没设则回落 scope 全局 key。写作链已传 `api_base`；**本计划同步把格式链也接上 `api_base`**，于是写作 + 格式两套模型都能走中转站。

**两种中转协议都覆盖（靠 model 前缀区分，无需新增字段）**——本项目走 LiteLLM（不用 Anthropic/OpenAI SDK），故 `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` 这类 SDK 原生 env 变量**不会被直接消费**，而是映射进 DB 行：

| 中转类型 | `model` | `base_url` | `api_key_env` 指向 |
|---|---|---|---|
| OpenAI 兼容中转 | `openai/gpt-4o` | 中转 `/v1` 地址 | 该中转 key |
| **Anthropic 原生中转**（CRS / claude-relay-service，token `crs_` 前缀，走 `/v1/messages`） | `anthropic/claude-opus-4-8`（`anthropic/` 前缀让 LiteLLM 走 Messages 协议） | `ANTHROPIC_BASE_URL` 的值（如 `http://host:8080/api`） | 存 `crs_...` 的 env 变量 |

**待实测坑**：LiteLLM anthropic provider 默认用 `x-api-key` 头，部分 CRS 只认 `Authorization: Bearer`——端到端冒烟时确认鉴权头匹配（`Invalid Auth` 先查这个，见 [gotcha-litellm-provider-baseurl](gotcha-litellm-provider-baseurl.md)）。

**思考 / 联网（走中转）= 零代码或不支持，本期不做**：中转的"思考版"通常是另一个模型串（如 `claude-opus-4-x-thinking`）——model 串自由文本，加一行选它即可，无需任何代码改动。Claude 服务端 `web_search` 经通用中转多不支持，本项目生文也不需要（配图联网走百度 `GEO_BAIDU_API_KEY`，无关）。故 `ai_models` 表**不加** thinking/web 字段。

**解析器放新模块 service，不放 config.py**（config 是最底层、必须 DB-unaware，否则 import 环）。两个调用点本就握有 DB session，传 `db` 进解析器天然可行。`config.resolve_engine` 原样保留作回落，签名不动，现有 `test_ai_engine_resolution.py` 不受影响。

---

## 后端改动

### 1. 新模块 `server/app/modules/ai_models/`（models / schemas / service / router）

**`models.py` — `AiModel` 表**
- 列：`id / label(String100) / model(String200,默认"") / scope(String20: "generation"|"ai_format") / base_url(String300,null) / api_key_env(String80,null) / is_enabled(Bool) / is_default(Bool) / is_default_key(String20,null) / sort_order(Int) / created_at / updated_at`。
- **每 scope 至多一个 default 的硬约束**：用 `is_default_key` 部分唯一技巧——`is_default=True` 时写 `is_default_key=scope`，否则 `NULL`；加 `UniqueConstraint("scope","is_default_key")`。MySQL 唯一索引允许多个 NULL、拒绝重复非 NULL，于是非默认行随便建、每 scope 默认行至多一个。service 在每次写入时同步 `is_default_key`。
- 索引 `ix_ai_models_scope_enabled(scope,is_enabled)`。

**`schemas.py`**：`AiModelCreate / AiModelUpdate(全可选,PATCH) / AiModelRead`。scope 校验须 ∈ `("generation","ai_format")`，否则抛 `ValidationError`（不抛裸 ValueError）。`api_key_env` 只是环境变量**名**、回传安全；`api_key` 本身永不出现在任何 schema。

**`service.py`**：
- CRUD：`list_models(db,*,scope,enabled_only) / get_model / create_model / update_model / delete_model`；`update/delete` 缺失抛 `ConflictError`。`_set_default` 同步 `is_default`+`is_default_key`，提升新默认时同事务清旧默认（唯一约束 `IntegrityError` 兜底 → 转 `ConflictError`）。
- 解析器：`resolve_writing_engine(db, selected) -> (model, api_key, base_url)`、`resolve_format_engine(db, selected=None) -> (model, api_key, base_url, timeout_seconds)`，按上文优先级（**格式解析器也返回 base_url**，供中转站）。
- 播种：`seed_ai_models_if_empty(db)`——表非空即 no-op（幂等）；空表时从 `settings.ai_engines` 建写作行（第 0 个设默认）+ 一条格式默认行（`settings.ai_format_model`）。**只复制 label/model/base_url，绝不写 api_key**。

**`router.py`**：admin-only CRUD，逐路由 `Depends(require_admin)`（仿 `audit/router.py`）。
```
GET /api/ai-models?scope=&enabled_only=  | POST /api/ai-models
GET/PATCH/DELETE /api/ai-models/{id}
```
写操作发 `add_audit_entry`（action `ai_model.create/update/delete`）。

### 2. 接两个调用点

- **写作** [article_writer.py:146](server/app/modules/ai_generation/article_writer.py#L146)：把 `resolve_engine(model)` 换成短生命周期 session 上的 `resolve_writing_engine(_db, model)`（`session_factory()` 开、`finally` 关；ThreadPoolExecutor 下每 worker 独立 session）。`generate_article_from_prompt` 签名不变，`model` 仍来自 `ai_compose` 的 `cfg["ai_engine"]` / `ai_generate` 的 `cfg["model"]` / scheme 的 `ai_engine`。
- **格式** [ai_format.py:910](server/app/modules/articles/ai_format.py#L910) `_ai_format_prepare`：此函数**已自开 `SessionLocal()`**（`finally` 关），把读 `settings.ai_format_model/api_key/timeout`（~line 998）改为 `resolve_format_engine(db, None)`。⚠️ **本分支基于 origin/main，`get_settings.cache_clear()` 仍在 `_ai_format_prepare`——保持不动**（Task 1c 在独立未合并分支 `feat/settings-refresh-endpoint` 删它，与本 PR 无关）；本计划只换模型源，resolver 对 cache_clear 在/不在都健壮。**为支持中转站要小改 run 层**：① `_AiFormatPrep`（class @871）加 `base_url: str | None` 字段；② `_call_litellm_completion`（[ai_format.py:529](server/app/modules/articles/ai_format.py#L529)）加 `api_base: str | None = None` 形参并传给 `completion(api_base=api_base or None)`；③ `_ai_format_prepare` 的**两个调用点（816 非兜底 / 1108 web_fallback）都经它**，改这一处即全覆盖，`prep.base_url` 透传到 LLM 段。

### 3. main.py 装配
- 挂载 `/api/ai-models`（`dependencies=[Depends(get_current_user)]`，admin 由路由内 `require_admin` 把关），仿 [main.py:256](server/app/main.py#L256) audit 块。
- 路由挂好后调 `seed_ai_models_if_empty`，try/except 仅记日志（非致命，仿 `start_auto_sync`）。

### 4. 下拉接口（保持前端零改动即可工作）
- **重指** `GET /api/generation/ai-engines`（[scheme_router.py:91](server/app/modules/ai_generation/scheme_router.py#L91)）改读 DB `scope=generation` enabled 行，仍返回 `AiEngineRead{label,model}`——SchemeEditor / PipelineEditor 现有下拉自动显示新增写作模型。**DB 为空时回落 `settings.ai_engines`**，下拉永不空。
- **新增** `GET /api/generation/format-engines`（`get_current_user` 即可）返回 `scope=ai_format` enabled 行，供格式下拉（含将来 ai_illustrate 节点）。

### 5. 迁移 `server/alembic/versions/0046_ai_models.py`
- `revision="0046"`，`down_revision="0045"`（现 head 已核实为 0045）。
- 仅建表 + 索引 + 唯一约束（`get_table_names()` 守卫幂等，仿 0034）。**不在迁移里塞数据**——播种需读运行时 env，放启动 seeder 更幂等、可降级。

---

## 前端改动

- **新 admin 功能** `web/src/features/ai-models/`：`AiModelsWorkspace.tsx`（列表：label/model/scope/base_url/api_key_env/enabled/default/操作=复制·编辑·删除）+ `AiModelFormModal.tsx`（字段含 scope 下拉「写作 generation / 格式·配图 ai_format」、api_key_env 文本框带提示"环境变量名，如 GEO_AI_API_KEY，密钥本身仍存环境变量"、enabled/default 开关）。样式仿 `AuditLogsWorkspace`。
- **API 客户端** `web/src/api/ai-models.ts`：`listAiModels/createAiModel/updateAiModel/deleteAiModel`。
- **类型** `web/src/types.ts`：新增 `AiModel/AiModelPayload`；**保留** `AiEngine={label,model}`（:60）不动——现有下拉继续吃 `listAiEngines()`，自动受益。`NavKey` 联合加 `"ai-models"`。
- **App.tsx**（admin 门控，仿 audit-logs 那两块**硬编码**块，不进 `navItems` 数组）：lazy import workspace；`user.role==="admin"` 块内加导航按钮；加 `visitedTabs.has("ai-models")` 的面板。
- **复制（克隆）动作**：列表操作列加「复制」图标 → 打开 `AiModelFormModal` 但**预填该行全部字段**（含 base_url + api_key_env），用户通常只改 名称 + 模型串 即另存为新模型——复用同一把 env key、url 不重填（demo 第三屏的交互）。纯前端：复制即"带初值的新增"，后端仍走 `POST /api/ai-models`，**无需新接口**。满足"加 Haiku 改个名就行"。

---

## 格式模型选择范围

- **本期核心**：仅靠 `scope=ai_format` 的 `is_default` 行 + `resolve_format_engine(db,None)`，管理页切换默认即换 DeepSeek flash/pro，调用点零改。
- **可选延伸（先不做，留钩子）**：`ai_illustrate` 节点加 `format_engine` 配置（透传到 `run_ai_format`→`_ai_format_prepare`→`resolve_format_engine(db,selected)`）；或 `GenerationScheme` 加 `format_ai_engine` 列做按方案覆盖。

---

## 验证

> UI 视觉/交互参考 `demo.pen`（三屏：① 模型管理列表 ② 编辑弹窗 ③ 复制预填弹窗），实现时对齐其布局、字段与"复制即带初值新增"的交互。

1. **后端测试**（`server/tests/`，`@pytest.mark.mysql` + `build_test_app`，`finally` 里 `cleanup()`）：
   - `test_ai_models_resolver.py`：写作/格式解析优先级（默认行命中、selected 命中、disabled 行忽略→回落、`api_key_env` 命中 env、缺失回落全局 key、无行委托 `config.resolve_engine` 仍认 `GEO_AI_ENGINES` 内联 key、**写作与格式行带 `base_url` 时解析器原样返回、无行时 base_url=None**）。
   - `test_ai_models_api.py`：admin CRUD 全通 + 非 admin 每个动词 403 + PATCH 只改 set 字段。
   - `test_ai_models_default_uniqueness.py`：同 scope 第二个 default 翻掉第一个、跨 scope 各一个默认并存。
   - `test_ai_models_seed.py`：空表播种、重复调用幂等、有行不播。
   - 运行：`GEO_TEST_DATABASE_URL=... pytest server/tests/test_ai_models_*.py -q`（参考记忆 [run-tests-env](run-tests-env.md)：工具 shell 里 conda 不生效，用 env python 全路径）。
2. **迁移**：`alembic upgrade head` 起表，`alembic downgrade -1` 可回滚。
3. **端到端手测**（本地全栈，参考 [env-local-dev-windows](env-local-dev-windows.md)）：起后端 8000 + 前端 5173；admin 登录 →「AI 模型」tab 新增一条 Claude opus 写作行 + 一条 DeepSeek pro 格式行并设默认 → 方案编辑器/Pipeline `ai_compose` 下拉立刻出现该写作模型 → 跑一篇生文确认走新模型；切换格式默认后跑一次 ai_format 确认换 pro。
4. **中转站验证**：env 里设 `GEO_RELAY_KEY=<中转站 key>`；管理页加一行 `base_url=<中转站/v1>`、`api_key_env=GEO_RELAY_KEY`、model=中转站约定串，分别建一条写作 + 一条格式行 → 跑生文 + ai_format，确认请求打到中转站地址、key 取自该环境变量（抓 litellm 日志或中转站后台）。
5. **门禁**：`ruff check server/ && ruff format --check server/ && mypy server/app`；前端 `pnpm --filter @geo/web typecheck && build`。

## 与在途 resource-hardening 计划的协调（docs/plans/2026-06-16-resource-hardening.md）

**无硬冲突**;只是 `ai_format.py` / `main.py` 的同文件协调 + 与 Task 1c 的交互,方向一致。

- **ai_format.py（共同热点,但已就绪)**:hardening **Phase 0 已合并(PR #111,`04eb146`)**,Task 1a/1b 的三段式/五段式重构在主线上,`_ai_format_prepare`(@910)/`_AiFormatPrep`(@871)/`_call_litellm_completion`(@529) 即重构后结构(2026-06-17 实测)——本计划的格式模型 DB 解析 + base_url 接线正建在其上,且 resolve 落在 **SEG1 短 session** 内、LLM 段不持连接,**与 hardening「慢 IO 不持连接」完全同向**。✅ 已落地:本工作基于 **origin/main(`8d22cb0`=#111+#112)** 拉分支(2026-06-17),`_ai_format_prepare` 等已存在,无旧单 session 结构之虑;cache_clear 仍在、保持不动(见下条)。
- **Task 1c 在独立未合并分支(`feat/settings-refresh-endpoint`/`6a8fa93`)、origin/main 尚无**:本 PR 基于 origin/main——`get_settings.cache_clear()` **仍在** ai_format.py,**保持不动**(它的去留归 Task 1c)。resolver 设计对此无依赖:DB 行实时读、`os.environ[api_key_env]` 直读 env(不过 lru_cache),仅"scope 全局回落 key"经 settings;cache_clear 在则运维改 env 即时生效、删则靠 Task 1c 的 refresh 端点——两种都兼容。
- **main.py create_app() / config.py = 加性合并**:hardening Task 3(`start_resource_sampler`)、Task 5(anyio 断言)也改 create_app/config.py;本计划挂 `/api/ai-models` + `seed_ai_models_if_empty` 与之**加性叠加**、无重叠行,合并即可。
- **无迁移号冲突**:hardening 不含 Alembic 迁移,本计划 `0046` 安全(head=0045)。
- **Task G 连接看门狗(30s 阈值)会监到本计划的 resolver session**:resolver 是亚毫秒短借,不触发——反而是对它的正向验证。

## 性能账

- **LiteLLM 不引入额外开销**:项目用库模式(进程内 `litellm.completion`，无代理跳数），翻译层是毫秒级，相对几秒~几十秒的生成（`max_tokens=12000`）可忽略。真正的吞吐瓶颈是 `ThreadPoolExecutor(max_workers=4)`（同步 `completion` 阻塞占线程）+ 中转/模型延迟 + DB 池，不是 LiteLLM。
- **DB 化新增开销 = 每次生成一条索引 SELECT（亚毫秒）**，对比生成耗时不计。关键不是查询耗时而是 **session 短开短关、绝不跨 litellm 调用持有**（已写入 §2 接调用点）——直接呼应 [bug-db-pool-exhaustion-crash](bug-db-pool-exhaustion-crash.md)：慢调用期间持连接会占死池。
- 生产别开 `litellm.set_verbose`；若将来要热路径零 DB 读，可进程内缓存模型清单 + 写时 invalidate——但每次读可忽略，**先不做**。

## 风险 / gotcha

- 已废弃的 `pipeline.py`（410）忽略 `ai_engine`——不复活其 `resolve_engine` import 即可；活跃写作路径全经 `generate_article_from_prompt`，改一处全覆盖。
- `is_default` 用 `is_default_key` 部分唯一兜底 + service 同事务清旧默认；`IntegrityError`→`ConflictError`。
- `GEO_AI_ENGINES` 内联 key 留在 env、靠 `config.resolve_engine` 回落解析，**不迁进 DB**（密钥不入库）。
- MySQL only；admin 改 AI 模型行**即时生效靠 DB 行实时读、不缓存**（无需 cache_clear）；env key 轮换靠 Task 1c 新增的 `POST /api/system/refresh-settings`（原 `cache_clear()` 已删，`6a8fa93`）。
- 测试 schema 须先 import `ai_models.models` 再 `create_all`——经 main.py 挂 router 间接 import 即可，落表时核实表存在。
