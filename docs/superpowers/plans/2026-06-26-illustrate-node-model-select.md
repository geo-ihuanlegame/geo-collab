# AI配图节点模型可配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 pipeline 的「AI配图」(ai_illustrate) 节点像「AI创作」(ai_compose) 一样,用下拉框选择配图所用的 LLM 模型(scope=ai_format),选择沿调用链透传到已存在的 `resolve_format_engine(db, selected)`。

**Architecture:** 不新建模块、不做 DB 迁移。统一模型注册表 `server/app/modules/ai_models/` 已上线(PR #115, migration `0046`),`resolve_format_engine(db, selected)` 已支持按模型选择并返回 `base_url`(Claude 中转地址),`/api/generation/format-engines` 下拉端点也已存在。本次只补「最后一段管线」:把节点配置里选的模型字符串,自底向上接到那个解析调用——目前该调用点硬写 `None`(`ai_format.py:998`),所以配图永远用默认格式模型。改动全部是**新增可选参数 + 一处实参替换**,默认值保持现有行为不变。

**Tech Stack:** FastAPI + SQLAlchemy(后端)、React 19 + Vite + TypeScript(前端)、LiteLLM(模型调用)、pytest(后端测试,`@pytest.mark.mysql` + `build_test_app`)。

## Global Constraints

- **不做 DB 迁移**:复用现有 `ai_models` 表与 `/format-engines` 端点。
- **密钥永不下发/入库**:模型选择存的是 litellm 模型串(如 `relay-model-x`),不是 key;key 由 `resolve_format_engine` 在后端按 `api_key_env` 从环境取。
- **空值语义**:配图模型下拉空值 = 系统默认。空串/None 传到 `resolve_format_engine(db, None)` 时由 `_match_row` 取该 scope 的 `is_default` 行,与现有行为一致。
- **向后兼容**:所有新增后端参数都是 `format_model_selected: str | None = None` / `format_model: str | None = None`,默认 None;不传 = 现状行为,存量节点/调用方零影响。
- **配图链路有两条,共用一份实现**:pipeline 节点 `run_ai_illustrate` 和 MCP/HTTP 端点 `ai_illustrate_article_mcp` 都走 `ai_illustrate_svc.illustrate_one`;两条都要能传模型。
- **后端测试运行方式**(见项目记忆):conda 的 activate 在工具 shell 里不生效,用 env python 全路径跑 pytest;需要 `GEO_TEST_DATABASE_URL`(库名含 `test`)。
- **worktree cwd 漂移**(见项目记忆):Bash 的 cwd 可能漂回主 checkout `/e/geo`;跑 `pytest` / `pnpm` / `git` 一律用 `-C <绝对路径>` 或绝对路径,跑完 `pwd` 确认。
- **前端无单测框架**:前端门禁 = `pnpm --filter @geo/web typecheck` + `build`,没有 `pnpm test`。
- **Vite 必须 5173 端口**(CORS 只放行 5173)——本计划不需要起前端服务,仅 typecheck/build。

---

## File Structure

后端(全部 Modify,无 Create):

- `server/app/modules/articles/ai_format.py` — 解析调用点。给 `run_ai_format` / `_run_ai_format_web_fallback` / `_ai_format_prepare` 加 `format_model_selected` 参数;把 `:998` 的 `resolve_format_engine(db, None)` 换成 `resolve_format_engine(db, format_model_selected)`。
- `server/app/modules/articles/ai_illustrate_svc.py` — `IllustrateOptions` 加 `format_model` 字段;`illustrate_one` 把它作为 `format_model_selected=` 传给 `run_ai_format`。
- `server/app/modules/pipelines/nodes/ai_illustrate.py` — 节点读 `cfg["format_engine"]`,塞进 `IllustrateOptions`。
- `server/app/modules/pipelines/router.py` — ai_illustrate 节点 `config_schema` 加一项 `format_engine` 字段(前端据此渲染下拉)。
- `server/app/modules/articles/router.py` — `AiIllustratePayload` 加 `format_engine`;`ai_illustrate_article_mcp` 透传给 `IllustrateOptions`。
- `server/mcp/tools/action.py` — MCP 工具 `ai_illustrate_article` 加 `format_engine` 参数 + 写进 POST body。

前端(全部 Modify,无 Create):

- `web/src/api/ai-generation.ts` — 加 `listFormatEngines()` 调 `/api/generation/format-engines`。
- `web/src/features/pipelines/PipelineEditor.tsx` — 加 `formatEngines` state + 加载 + `f.type === "format_engine"` 渲染分支(镜像现有 `ai_engine` 分支)。

测试(Modify 现有文件,复用其 helper):

- `server/tests/test_ai_illustrate_node.py` — 复用 `_make_article` / `_uid` / `_make_category` / `_capture_knobs`,加解析透传测试与节点透传测试。
- `server/tests/test_articles_ai_illustrate_endpoint.py` — 复用其 MCP-token 模式,加端点透传测试。

---

## Task 1: 后端 — 把 `format_model_selected` 从 `run_ai_format` 接到 `resolve_format_engine`

自底向上做第一步:解析调用点先支持选择,默认 None = 现状。这样后续上层接线时实链路始终可用。

**Files:**
- Modify: `server/app/modules/articles/ai_format.py`
  - `run_ai_format`(`server/app/modules/articles/ai_format.py:804`)
  - 其内部对 `_run_ai_format_web_fallback` 的调用(`:836`)与对 `_ai_format_prepare` 的调用(`:851`)
  - `_run_ai_format_web_fallback`(`:1145`)及其内部 `_ai_format_prepare` 调用(`:1169`)
  - `_ai_format_prepare`(`:950`)签名 + 解析调用点(`:998`)
- Test: `server/tests/test_ai_illustrate_node.py`

**Interfaces:**
- Consumes: `server.app.modules.ai_models.service.resolve_format_engine(db, selected) -> (model: str, api_key: str, base_url: str | None, timeout: int)`(已存在,无需改)。
- Produces:
  - `run_ai_format(article_id, *, ..., format_model_selected: str | None = None) -> int`
  - `_ai_format_prepare(article_id, *, ..., format_model_selected: str | None = None) -> _AiFormatPrep | None`(`_AiFormatPrep.model` / `.base_url` 反映解析结果)

- [ ] **Step 1: 写失败测试**

加到 `server/tests/test_ai_illustrate_node.py` 末尾(复用文件内已有的 `_make_article` / `_uid`):

```python
@pytest.mark.mysql
def test_ai_format_prepare_threads_selected_model(monkeypatch):
    """_ai_format_prepare 把 format_model_selected 透传给 resolve_format_engine，
    并把解析出的 model/base_url（含 Claude 中转地址）带进 prep。"""
    app = build_test_app(monkeypatch)
    try:
        aid = _make_article(app.client)
        uid = _uid(app)
        captured: dict = {}

        def _spy_resolve(db, selected=None):
            captured["selected"] = selected
            return ("relay-model-x", "sk-relay", "https://relay.example/v1", 120)

        # resolve_format_engine 在 _ai_format_prepare 内部按需 import，故 patch 源模块属性
        monkeypatch.setattr(
            "server.app.modules.ai_models.service.resolve_format_engine", _spy_resolve
        )

        from server.app.modules.articles.ai_format import _ai_format_prepare

        prep = _ai_format_prepare(
            aid,
            lock_started_at=None,
            include_images=False,
            preset_id=None,
            user_id=uid,
            candidate_categories=None,
            max_images=None,
            min_spacing=None,
            builtin_variant="conservative",
            format_model_selected="relay-model-x",
        )
        assert captured["selected"] == "relay-model-x"
        assert prep is not None
        assert prep.model == "relay-model-x"
        assert prep.base_url == "https://relay.example/v1"
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest server/tests/test_ai_illustrate_node.py::test_ai_format_prepare_threads_selected_model -q`
Expected: FAIL — `_ai_format_prepare()` 报 `TypeError: ... unexpected keyword argument 'format_model_selected'`。

- [ ] **Step 3: 实现 — `_ai_format_prepare` 加参数 + 换实参**

在 `_ai_format_prepare` 签名(`:950`)的关键字参数里加一行(放在 `web_fallback: bool = False,` 之前或之后均可):

```python
def _ai_format_prepare(
    article_id: int,
    *,
    lock_started_at: datetime | None,
    include_images: bool,
    preset_id: int | None,
    user_id: int | None,
    candidate_categories: list[dict[str, Any]] | None,
    max_images: int | None,
    min_spacing: int | None,
    builtin_variant: str,
    format_model_selected: str | None = None,
    web_fallback: bool = False,
) -> _AiFormatPrep | None:
```

把解析调用点(`:998`)的 `None` 换成新参数:

```python
        format_model, format_key, format_base_url, format_timeout = resolve_format_engine(
            db, format_model_selected
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest server/tests/test_ai_illustrate_node.py::test_ai_format_prepare_threads_selected_model -q`
Expected: PASS。

- [ ] **Step 5: 实现 — 上游 `run_ai_format` / `_run_ai_format_web_fallback` 透传**

`run_ai_format` 签名(`:804`)加参数:

```python
def run_ai_format(
    article_id: int,
    *,
    include_images: bool = False,
    lock_started_at: datetime | None = None,
    preset_id: int | None = None,
    user_id: int | None = None,
    candidate_categories: list[dict[str, Any]] | None = None,
    web_fallback: bool = False,
    max_images: int | None = None,
    min_spacing: int | None = None,
    builtin_variant: str = "conservative",
    format_model_selected: str | None = None,
    out_diagnostics: dict[str, Any] | None = None,
) -> int:
```

`run_ai_format` 内对 `_run_ai_format_web_fallback` 的调用(`:836`)加一行 `format_model_selected=format_model_selected,`:

```python
    if web_fallback:
        return _run_ai_format_web_fallback(
            article_id,
            include_images=include_images,
            lock_started_at=lock_started_at,
            preset_id=preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
            format_model_selected=format_model_selected,
            out_diagnostics=out_diagnostics,
        )
```

`run_ai_format` 内对 `_ai_format_prepare` 的调用(`:851`)加一行 `format_model_selected=format_model_selected,`:

```python
        prep = _ai_format_prepare(
            article_id,
            lock_started_at=lock_started_at,
            include_images=include_images,
            preset_id=preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
            format_model_selected=format_model_selected,
        )
```

`_run_ai_format_web_fallback` 签名(`:1145`)加同名关键字参数。完整 old→new:

```python
# old_string（现状，:1145 起）
def _run_ai_format_web_fallback(
    article_id: int,
    *,
    include_images: bool,
    lock_started_at: datetime | None,
    preset_id: int | None,
    user_id: int | None,
    candidate_categories: list[dict[str, Any]] | None,
    max_images: int | None,
    min_spacing: int | None,
    builtin_variant: str,
    out_diagnostics: dict[str, Any] | None = None,
) -> int:

# new_string（在 out_diagnostics 之前插入一行）
def _run_ai_format_web_fallback(
    article_id: int,
    *,
    include_images: bool,
    lock_started_at: datetime | None,
    preset_id: int | None,
    user_id: int | None,
    candidate_categories: list[dict[str, Any]] | None,
    max_images: int | None,
    min_spacing: int | None,
    builtin_variant: str,
    format_model_selected: str | None = None,
    out_diagnostics: dict[str, Any] | None = None,
) -> int:
```

并在其内部对 `_ai_format_prepare` 的调用(`:1169`,此处带 `web_fallback=True`)的关键字实参里加一行 `format_model_selected=format_model_selected,`。

> ⚠️ 执行提示:`run_ai_format` 与 `_run_ai_format_web_fallback` 两处签名都以 `out_diagnostics: dict[str, Any] | None = None,` 结尾,文本相同。用 Edit 时 `old_string` 要带上各自的函数名行(`def run_ai_format(` / `def _run_ai_format_web_fallback(`)及完整参数块以保证唯一匹配,不要只截 `out_diagnostics` 那一行。

- [ ] **Step 6: 跑该文件全部用例确认无回归**

Run: `python -m pytest server/tests/test_ai_illustrate_node.py server/tests/test_web_fallback.py -q`
Expected: PASS(全部,包括既有用例)。

- [ ] **Step 7: ruff + 提交**

```bash
ruff check server/app/modules/articles/ai_format.py server/tests/test_ai_illustrate_node.py
git add server/app/modules/articles/ai_format.py server/tests/test_ai_illustrate_node.py
git commit -m "feat(ai_format): thread format_model_selected into resolve_format_engine"
```

---

## Task 2: 后端 — AI配图节点与配图服务携带模型选择

**Files:**
- Modify: `server/app/modules/articles/ai_illustrate_svc.py`(`IllustrateOptions` `:33`;`illustrate_one` 内 `run_ai_format(...)` 调用 `:170`)
- Modify: `server/app/modules/pipelines/nodes/ai_illustrate.py`(`run_ai_illustrate` `:33` 起)
- Modify: `server/app/modules/pipelines/router.py`(ai_illustrate `config_schema` `:170`)
- Test: `server/tests/test_ai_illustrate_node.py`

**Interfaces:**
- Consumes: `run_ai_format(..., format_model_selected=...)`(Task 1 产出)。
- Produces:
  - `IllustrateOptions.format_model: str | None = None`
  - 节点 `config_schema` 新增字段 `{"key": "format_engine", "type": "format_engine", ...}`(前端 Task 4 据此渲染)。

- [ ] **Step 1: 写失败测试**

加到 `server/tests/test_ai_illustrate_node.py`(复用文件内 `_make_category` / `_make_article` / `_uid` / `_capture_knobs`):

```python
@pytest.mark.mysql
def test_ai_illustrate_passes_format_model(monkeypatch):
    """节点 cfg.format_engine → IllustrateOptions.format_model → run_ai_format(format_model_selected=...)。"""
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        aid = _make_article(app.client)
        uid = _uid(app)
        captured = _capture_knobs(monkeypatch)

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={
                    "main_category_id": main_id,
                    "format_engine": "relay-model-x",
                    "set_cover": False,
                },
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert captured["format_model_selected"] == "relay-model-x"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_format_model_default_none(monkeypatch):
    """不配 format_engine → format_model_selected 透传 None（走默认格式模型）。"""
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        aid = _make_article(app.client)
        uid = _uid(app)
        captured = _capture_knobs(monkeypatch)

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id, "set_cover": False},
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert captured["format_model_selected"] is None
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest server/tests/test_ai_illustrate_node.py::test_ai_illustrate_passes_format_model server/tests/test_ai_illustrate_node.py::test_ai_illustrate_format_model_default_none -q`
Expected: FAIL — `captured` 里没有 `format_model_selected` 键(节点未读 cfg、`illustrate_one` 未透传)。

- [ ] **Step 3: 实现 — `IllustrateOptions` 加字段**

`server/app/modules/articles/ai_illustrate_svc.py` 的 `IllustrateOptions`(`:33`)加一行:

```python
@dataclass
class IllustrateOptions:
    """配图旋钮，跟 pipeline ai_illustrate 节点的 cfg 字段一一对应.

    max_images / min_spacing 的 0 等同 None（视为未设置，回退到风格默认 12/1 或 3/5）；
    要"无上限"请用 None；想要硬上限则传正整数。
    """

    include_companion: bool = True
    web_fallback: bool = False
    aggressive_images: bool = True
    max_images: int | None = None
    min_spacing: int | None = None
    preset_id: int | None = None
    set_cover: bool = True
    # 配图模型（litellm 模型串，scope=ai_format）；None/"" = 走默认格式模型
    format_model: str | None = None
```

- [ ] **Step 4: 实现 — `illustrate_one` 透传**

`illustrate_one` 内对 `run_ai_format` 的调用(`:170`)加一行 `format_model_selected=options.format_model,`:

```python
    images_inserted = run_ai_format(
        article_id,
        include_images=True,
        lock_started_at=lock_started_at,
        preset_id=options.preset_id,
        user_id=user_id,
        candidate_categories=candidate_categories,
        web_fallback=options.web_fallback,
        max_images=max_images,
        min_spacing=min_spacing,
        builtin_variant=builtin_variant,
        format_model_selected=options.format_model,
        out_diagnostics=fmt_diag,
    )
```

- [ ] **Step 5: 实现 — 节点读 cfg**

`server/app/modules/pipelines/nodes/ai_illustrate.py` 的 `run_ai_illustrate`,在读取其它 cfg 旋钮处(`set_cover` 附近,`:46`)加:

```python
    # 配图模型：空串/None = 走默认格式模型（resolve_format_engine 取 scope 默认行）
    format_model = cfg.get("format_engine") or None
```

并在 `IllustrateOptions(...)` 构造(`:65`)里加一行 `format_model=format_model,`:

```python
            options=IllustrateOptions(
                include_companion=include_companion,
                web_fallback=web_fallback,
                aggressive_images=aggressive,
                max_images=max_images,
                min_spacing=min_spacing,
                preset_id=effective_preset,
                set_cover=set_cover,
                format_model=format_model,
            ),
```

- [ ] **Step 6: 实现 — 节点 config_schema 加下拉字段**

`server/app/modules/pipelines/router.py` 的 ai_illustrate `config_schema`(`config_schema` 起于 `:170`,`main_category_id` 字段条目在 `:171-175`)。在 `main_category_id` 条目结束 `},`(`:175`)与下面 `aggressive_images` 的注释之间插入新条目。为保证 Edit 唯一匹配,`old_string` 锚定 `main_category_id` 条目尾 + 紧随的风格注释:

```python
# old_string（:172-177 附近，按实际文件为准）
                        "label": "图片库 · 主推游戏",
                    },
                    # 配图风格：开=「积极配图」(每个明确出现的游戏都插，保留"不确定不插"准星)，
                    # 关=保守(图少文多)。默认开。见 ai_format._builtin_prompt_template 的 aggressive 变体。

# new_string（在 main_category_id 条目 `},` 之后插入 format_engine 条目）
                        "label": "图片库 · 主推游戏",
                    },
                    {
                        "key": "format_engine",
                        "type": "format_engine",
                        "label": "配图模型（留空=默认）",
                        "hint": "选「AI 模型管理」里 用途=格式·配图 的模型；留空走默认格式模型",
                    },
                    # 配图风格：开=「积极配图」(每个明确出现的游戏都插，保留"不确定不插"准星)，
                    # 关=保守(图少文多)。默认开。见 ai_format._builtin_prompt_template 的 aggressive 变体。
```

- [ ] **Step 7: 运行测试确认通过**

Run: `python -m pytest server/tests/test_ai_illustrate_node.py server/tests/test_ai_illustrate_svc.py -q`
Expected: PASS(含既有用例)。

- [ ] **Step 8: ruff + 提交**

```bash
ruff check server/app/modules/articles/ai_illustrate_svc.py server/app/modules/pipelines/nodes/ai_illustrate.py server/app/modules/pipelines/router.py server/tests/test_ai_illustrate_node.py
git add server/app/modules/articles/ai_illustrate_svc.py server/app/modules/pipelines/nodes/ai_illustrate.py server/app/modules/pipelines/router.py server/tests/test_ai_illustrate_node.py
git commit -m "feat(ai_illustrate): expose 配图模型 select on node config"
```

---

## Task 3: 后端 — MCP/HTTP 配图端点 + MCP 工具携带模型选择

`/goal` Loop 经 MCP 工具 `ai_illustrate_article` → `POST /api/articles/{id}/ai-illustrate` → `illustrate_one`,与 pipeline 节点共用实现。补上这条路径,使两条配图入口能力一致。

**Files:**
- Modify: `server/app/modules/articles/router.py`(`AiIllustratePayload` `:1043`;`ai_illustrate_article_mcp` 内 `IllustrateOptions(...)` `:1094`)
- Modify: `server/mcp/tools/action.py`(`ai_illustrate_article` `:275`,body `:332`)
- Test: `server/tests/test_articles_ai_illustrate_endpoint.py`

**Interfaces:**
- Consumes: `IllustrateOptions.format_model`(Task 2 产出)。
- Produces: `AiIllustratePayload.format_engine: str | None = None`;MCP 工具新增同名参数。

- [ ] **Step 1: 写失败测试**

加到 `server/tests/test_articles_ai_illustrate_endpoint.py`(复用其 MCP-token 模式):

```python
@pytest.mark.mysql
def test_ai_illustrate_endpoint_passes_format_engine(monkeypatch):
    """payload.format_engine → IllustrateOptions.format_model。"""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        from server.app.modules.articles.ai_illustrate_svc import IllustrateResult

        called: dict = {}

        def fake_illustrate_one(*, article_id, main_category_id, user_id, options, session_factory):
            called["format_model"] = options.format_model
            return IllustrateResult(
                article_id=article_id,
                images_inserted=0,
                cover_status="skipped",
                cover_error=None,
                format_error=None,
            )

        monkeypatch.setattr(
            "server.app.modules.articles.router.illustrate_one", fake_illustrate_one
        )

        r = test_app.client.post(
            "/api/articles/9/ai-illustrate",
            json={"main_category_id": 1, "format_engine": "relay-model-x"},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        assert called["format_model"] == "relay-model-x"
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest server/tests/test_articles_ai_illustrate_endpoint.py::test_ai_illustrate_endpoint_passes_format_engine -q`
Expected: FAIL — `called["format_model"]` 为 `None`(payload 丢弃了未知字段 `format_engine`,端点未透传)。

> 前置依赖:本测试断言 `options.format_model is None` 才能"为 None 而失败",依赖 Task 2 已给 `IllustrateOptions` 加好 `format_model`(默认 None)字段;务必按 Task 1→2→3 顺序执行,不要跨任务先跑 Task 3。

- [ ] **Step 3: 实现 — `AiIllustratePayload` 加字段**

`server/app/modules/articles/router.py` 的 `AiIllustratePayload`(`:1043`)加一行:

```python
class AiIllustratePayload(BaseModel):
    """走 ai_illustrate 节点同款逻辑（AI 决策 + 自动封面）."""

    main_category_id: int
    include_companion: bool = True
    web_fallback: bool = False
    aggressive_images: bool = True
    max_images: int | None = Field(default=None, ge=1, le=50)
    min_spacing: int | None = Field(default=None, ge=1, le=20)
    preset_id: int | None = None
    set_cover: bool = True
    # 配图模型（litellm 模型串，scope=ai_format）；None/"" = 默认格式模型
    format_engine: str | None = None
```

- [ ] **Step 4: 实现 — 端点透传**

`ai_illustrate_article_mcp` 内 `IllustrateOptions(...)` 构造(`:1094`)加一行 `format_model=payload.format_engine,`:

```python
            options=IllustrateOptions(
                include_companion=payload.include_companion,
                web_fallback=payload.web_fallback,
                aggressive_images=payload.aggressive_images,
                max_images=payload.max_images,
                min_spacing=payload.min_spacing,
                preset_id=payload.preset_id,
                set_cover=payload.set_cover,
                format_model=payload.format_engine,
            ),
```

> 注意:此处需与现有构造的其余字段保持一致;若现有代码字段顺序/换行不同,仅**新增** `format_model=payload.format_engine,` 一行,不要重排其它行。

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest server/tests/test_articles_ai_illustrate_endpoint.py -q`
Expected: PASS(含既有两条用例)。

- [ ] **Step 6: 实现 — MCP 工具加参数**

`server/mcp/tools/action.py` 的 `ai_illustrate_article`(`:275`)签名加 `format_engine`:

```python
async def ai_illustrate_article(
    article_id: int,
    main_category_id: int,
    include_companion: bool = True,
    aggressive_images: bool = True,
    set_cover: bool = True,
    web_fallback: bool = False,
    format_engine: str | None = None,
) -> dict[str, Any]:
```

在 docstring 的 `Args:` 段补一行说明(放在 `web_fallback` 说明之后):

```
        format_engine: 配图所用 LLM 模型串（scope=ai_format，需在「AI 模型管理」里存在并启用）。
            None = 用默认格式模型。用于给配图换中转/不同模型，与 Web UI「AI配图」节点的下拉等价。
```

把 body(`:332`)加一行:

```python
    body: dict[str, Any] = {
        "main_category_id": main_category_id,
        "include_companion": include_companion,
        "aggressive_images": aggressive_images,
        "set_cover": set_cover,
        "web_fallback": web_fallback,
        "format_engine": format_engine,
    }
```

- [ ] **Step 7: ruff + 提交**

```bash
ruff check server/app/modules/articles/router.py server/mcp/tools/action.py server/tests/test_articles_ai_illustrate_endpoint.py
git add server/app/modules/articles/router.py server/mcp/tools/action.py server/tests/test_articles_ai_illustrate_endpoint.py
git commit -m "feat(mcp): pass format_engine through ai-illustrate endpoint + tool"
```

---

## Task 4: 前端 — `listFormatEngines()` + `format_engine` 下拉字段类型

**Files:**
- Modify: `web/src/api/ai-generation.ts`(`listAiEngines` `:95` 附近)
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`(import `:5`、state `:277`、loader `:298`、render 分支 `:635`)

**Interfaces:**
- Consumes: 后端 `GET /api/generation/format-engines`(已存在)→ `AiEngine[]`(`{ label: string; model: string }`,`web/src/types.ts:60`)。
- Produces: 前端识别 `config_schema` 字段类型 `"format_engine"`(Task 2 产出)并渲染下拉。

- [ ] **Step 1: 加 API 客户端函数**

`web/src/api/ai-generation.ts`,在 `listAiEngines`(`:95`)之后加:

```typescript
export function listFormatEngines(): Promise<AiEngine[]> {
  return api<AiEngine[]>("/api/generation/format-engines");
}
```

(`AiEngine` 类型该文件已 import 用于 `listAiEngines`,无需新增 import。)

- [ ] **Step 2: PipelineEditor 引入 + 加载列表**

修改 import(`:5`):

```typescript
import { listAiEngines, listFormatEngines, listQuestionPools, listQuestionTypes } from "../../api/ai-generation";
```

在 `engines` state(`:277`)之后加一行 state:

```typescript
  const [formatEngines, setFormatEngines] = useState<AiEngine[]>([]);
```

在加载 `listAiEngines().then(setEngines)`(`:298`)之后加一行:

```typescript
    listFormatEngines().then(setFormatEngines).catch(() => {});
```

- [ ] **Step 3: 加渲染分支(镜像 `ai_engine`)**

在 `f.type === "ai_engine"` 分支(`:635`-`:643`)之后、`f.type === "prompt_templates"` 分支(`:644`)之前,插入:

```tsx
                    : f.type === "format_engine"
                    ? <select value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => updateNode(selected!,
                          { config: { ...sel.config, [f.key]: e.target.value || null } })}>
                        <option value="">系统默认</option>
                        {formatEngines.map((en) => (
                          <option key={en.model} value={en.model}>{en.label || en.model}</option>
                        ))}
                      </select>
```

- [ ] **Step 4: typecheck 确认通过**

Run: `pnpm --filter @geo/web typecheck`(从 worktree 根目录)
Expected: 无 TS 报错。

- [ ] **Step 5: build 确认通过**

Run: `pnpm --filter @geo/web build`(从 worktree 根目录)
Expected: 构建成功。

> 注:本仓库前端 pnpm 过滤名是 `@geo/web`。**worktree 下 Bash cwd 可能漂回主 checkout**(见 Global Constraints),`-C web` 相对路径有踩错树风险;**优先用过滤式 `pnpm --filter @geo/web typecheck` / `... build`**(从 worktree 根目录跑),或用绝对路径 `pnpm -C E:\geo\.claude\worktrees\orktree\web typecheck`。跑前先 `pwd` 确认在 worktree 内。

- [ ] **Step 6: 提交**

```bash
git add web/src/api/ai-generation.ts web/src/features/pipelines/PipelineEditor.tsx
git commit -m "feat(web): 配图模型下拉 for AI配图 node"
```

---

## Task 5: 全量回归 + 收尾

- [ ] **Step 1: 跑相关后端测试**

Run:
```bash
python -m pytest server/tests/test_ai_illustrate_node.py server/tests/test_ai_illustrate_svc.py server/tests/test_articles_ai_illustrate_endpoint.py server/tests/test_pipeline_ai_illustrate.py server/tests/test_web_fallback.py server/tests/test_ai_models_resolver.py -q
```
Expected: 全 PASS。

- [ ] **Step 2: 后端 lint/format/type 门禁**

Run:
```bash
ruff check server/
ruff format --check server/
mypy server/app
```
Expected: 通过(mypy 宽松配置)。

- [ ] **Step 3: 前端门禁**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

---

## 运营说明(非代码任务,供文档/交接)

- 下拉的候选来自 `GET /api/generation/format-engines` = `ai_models` 表里 `scope=ai_format` 且 `is_enabled=True` 的行。现网首次播种只建了一条「默认格式模型」。要让配图能选 **Claude 中转**,管理员需在前端「AI 模型管理」页(`AiModelsWorkspace`)为 ai_format 用途新增一行:`model=<中转要的模型串>`、`base_url=<中转地址>`、`api_key_env=<中转 key 的环境变量名>`,启用即出现在下拉里。
- Claude 中转链路代码侧已通(`resolve_format_engine` 返回 `base_url` → `_call_litellm_completion(api_base=...)`)。配置侧需注意 litellm 的 provider 前缀要与中转匹配(OpenAI 兼容中转常需 `openai/` 前缀),否则 Invalid Auth——这是配置问题,非本次代码改动范围。

---

## Self-Review

**1. Spec coverage:**
- 需求「AI配图节点加模型下拉」→ Task 2(节点 config_schema + 选项透传)+ Task 4(前端下拉)✅
- 需求「沿用统一模型配置」→ 复用既有 `ai_models` + `resolve_format_engine` + `/format-engines`,不新建模块 ✅
- 需求「Claude 中转仍可用」→ `base_url` 全程透传,Task 1 测试断言 `prep.base_url`;运营说明给出配置步骤 ✅
- 两条配图入口(节点 + MCP/goal loop)→ Task 2 + Task 3 ✅

**2. Placeholder scan:** 无 TBD/TODO;每个代码步骤给出完整代码块;测试代码完整可运行。✅

**3. Type consistency:**
- `format_model_selected`(`run_ai_format`/`_run_ai_format_web_fallback`/`_ai_format_prepare`)统一命名 ✅
- `IllustrateOptions.format_model`(svc 层字段)→ `illustrate_one` 映射为 `format_model_selected=options.format_model` ✅
- `AiIllustratePayload.format_engine`(API 入参)→ `IllustrateOptions(format_model=payload.format_engine)` ✅
- 节点 cfg 键 `format_engine`(与前端字段 `type:"format_engine"` 一致)→ `IllustrateOptions(format_model=...)` ✅
- 命名映射链清晰:外层(节点 cfg / API / 前端)用 `format_engine`,svc 内层用 `format_model`,ai_format 解析参数用 `format_model_selected`——每层边界都有显式映射,无同名漂移。
